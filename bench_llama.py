"""Speed benchmark for the local llama-server lane.

Runs one llama-server configuration after another against the same model and
measures what an agent harness actually waits for: decode speed on a short
answer, prompt processing on a long prompt, a screenshot round trip, and VRAM.
Results go to bench_results/<timestamp>.csv and .md so every number in
local_llama.py's comments can be traced back to a run.

Usage:
  python bench_llama.py                 # all configs in CONFIGS
  python bench_llama.py base new-base   # only the named configs
  python bench_llama.py --list

It refuses to start while another program holds more than MAX_FOREIGN_VRAM_MIB
of video memory (ComfyUI video generation, for example), because a shared GPU
makes every number meaningless. Stop the local llama-server first as well.

This starts llama-server only for the length of each measurement and always
terminates it; it is not a way to run the server permanently (use the tray).
"""
import base64
import csv
import io
import json
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import local_llama

SCRIPT_DIR = Path(__file__).resolve().parent
OUT_DIR = SCRIPT_DIR / "bench_results"
URL = f"http://{local_llama.HOST}:{local_llama.PORT}"
OLD_EXE = "C:/llama.cpp/llama-server.exe"
NEW_EXE = "C:/llama.cpp-b12613/llama-server.exe"
MAX_FOREIGN_VRAM_MIB = 2500   # the desktop alone uses about 1.5 GB here
DECODE_REPS = 3               # repeated short answers; median is reported
LONG_PROMPT_TOKENS = 8000     # sizes the filler; the tokenizer actually yields ~15.5k tokens

MODEL = "Huihui-Qwen3.6-35B-A3B-abliterated.Q4_K_M.gguf"

COMMON = ["--ctx-size", "65536", "--n-gpu-layers", "999", "--flash-attn", "on",
          "--jinja", "--no-context-shift",
          "--chat-template-kwargs", '{"enable_thinking": false}',
          "--temp", "0.7", "--top-p", "0.8", "--top-k", "20",
          "--repeat-penalty", "1.0", "--presence-penalty", "1.5"]
KV_Q4 = ["--cache-type-k", "q4_0", "--cache-type-v", "q4_0"]
KV_Q8 = ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]

# name -> (executable, extra flags). "base" is exactly what local_llama.py
# runs today; every other entry changes one thing against it where possible.
CONFIGS = {
    "base":          (OLD_EXE, KV_Q4 + ["--n-cpu-moe", "23"]),
    "new-base":      (NEW_EXE, KV_Q4 + ["--n-cpu-moe", "23"]),
    "new-kv8":       (NEW_EXE, KV_Q8 + ["--n-cpu-moe", "23"]),
    "new-t8":        (NEW_EXE, KV_Q4 + ["--n-cpu-moe", "23", "--threads", "8"]),
    "new-t16":       (NEW_EXE, KV_Q4 + ["--n-cpu-moe", "23", "--threads", "16"]),
    "new-ncmoe20":   (NEW_EXE, KV_Q4 + ["--n-cpu-moe", "20"]),
    "new-cache8g":   (NEW_EXE, KV_Q4 + ["--cpu-moe", "--moe-cache-mib", "8000"]),
    "new-cache10g":  (NEW_EXE, KV_Q4 + ["--cpu-moe", "--moe-cache-mib", "10000"]),
    "new-ncmoe23-cache2g": (NEW_EXE, KV_Q4 + ["--n-cpu-moe", "23", "--moe-cache-mib", "2000"]),
    # round 2: combinations of the round-1 winners (threads 8 = the 8 P-cores)
    "new-ncmoe20-t8":   (NEW_EXE, KV_Q4 + ["--n-cpu-moe", "20", "--threads", "8"]),
    "new-cache8g-t8":   (NEW_EXE, KV_Q4 + ["--cpu-moe", "--moe-cache-mib", "8000", "--threads", "8"]),
    "new-cache6g-t8":   (NEW_EXE, KV_Q4 + ["--cpu-moe", "--moe-cache-mib", "6000", "--threads", "8"]),
    "new-cache8g-t8-kv8": (NEW_EXE, KV_Q8 + ["--cpu-moe", "--moe-cache-mib", "8000", "--threads", "8"]),
}

# Unrelated tasks in different languages and formats, so an expert cache
# cannot stay warm on one topic the way it does on the repeated PID question.
MIXED_PROMPTS = [
    "Напиши на русском письмо коллеге о переносе семинара на четверг, 150 слов.",
    "Return a JSON array of 15 objects {name, capital, population_millions} for European countries.",
    "Write a Python function that parses an ISO 8601 date range and returns the number of weekdays. Include docstring and tests.",
    "Explain the mechanism of Fenton oxidation of an azo dye in about 200 words.",
]


def vram_mib() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout
    return int(out.strip().splitlines()[0])


def get(path):
    with urllib.request.urlopen(URL + path, timeout=5) as r:
        return json.loads(r.read())


def chat(messages, max_tokens, temperature=0.7):
    body = {"model": local_llama.ALIAS, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    req = urllib.request.Request(URL + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())


def screenshot_b64() -> str:
    img = Image.new("RGB", (1920, 1080), "white")
    dr = ImageDraw.Draw(img)
    font = ImageFont.truetype("arial.ttf", 48)
    dr.rectangle([800, 500, 1120, 600], outline="black", width=4, fill="#2e7d32")
    dr.text((850, 520), "Сохранить", fill="white", font=font)
    dr.text((100, 100), "Invoice #48213  Total: 7 350 KZT", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def long_prompt() -> str:
    # Varied filler so the prompt cache cannot shortcut it; about 10 tokens
    # per line for this tokenizer.
    lines = [f"Record {i}: sensor {i % 37} read {i * 7 % 1000} units at step {i}."
             for i in range(LONG_PROMPT_TOKENS // 12)]
    return "\n".join(lines) + "\n\nHow many records are listed above? Answer with one number."


def run_config(name, exe, flags, model_path, mmproj, log_dir):
    cmd = [exe, "--model", str(model_path), "--mmproj", str(mmproj),
           "--host", local_llama.HOST, "--port", str(local_llama.PORT),
           "--alias", local_llama.ALIAS, *COMMON, *flags]
    row = {"config": name, "exe": Path(exe).parent.name, "flags": " ".join(flags)}
    with open(log_dir / f"{name}.log", "w", encoding="utf-8") as log:
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        try:
            t0 = time.time()
            while True:
                if p.poll() is not None:
                    row["error"] = f"server exited with code {p.returncode} (see {name}.log)"
                    return row
                try:
                    if get("/health").get("status") == "ok":
                        break
                except Exception:
                    pass
                if time.time() - t0 > 300:
                    row["error"] = "timeout waiting for health"
                    return row
                time.sleep(2)
            row["load_s"] = time.time() - t0
            row["vram_loaded_mib"] = vram_mib()

            chat([{"role": "user", "content": "Say OK."}], 8)  # warm-up
            tps = []
            for i in range(DECODE_REPS):
                r = chat([{"role": "user", "content":
                           f"Variant {i}: explain in about 200 words how a PID controller works."}], 300)
                tps.append(r["timings"]["predicted_per_second"])
            row["decode_tps_median"] = statistics.median(tps)
            row["decode_tps_runs"] = " / ".join(f"{x:.1f}" for x in tps)

            mixed = [chat([{"role": "user", "content": q}], 300)["timings"]["predicted_per_second"]
                     for q in MIXED_PROMPTS]
            row["mixed_tps_median"] = statistics.median(mixed)
            row["mixed_tps_runs"] = " / ".join(f"{x:.1f}" for x in mixed)

            r = chat([{"role": "user", "content": long_prompt()}], 16, temperature=0.0)
            row["long_prompt_n"] = r["timings"]["prompt_n"]
            row["long_prompt_tps"] = r["timings"]["prompt_per_second"]

            r = chat([{"role": "user", "content": [
                {"type": "text", "text": "Screenshot 1920x1080. Give the invoice number, the total, "
                                         "and the centre of the green button as JSON."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + screenshot_b64()}}]}],
                150, temperature=0.2)
            row["vision_prompt_n"] = r["timings"]["prompt_n"]
            row["vision_prompt_tps"] = r["timings"]["prompt_per_second"]
            row["vision_decode_tps"] = r["timings"]["predicted_per_second"]
            row["vision_answer"] = r["choices"][0]["message"]["content"].strip().replace("\n", " ")[:200]
            row["vram_peak_mib"] = vram_mib()
            return row
        except Exception as exc:  # keep going with the next config
            row["error"] = repr(exc)
            return row
        finally:
            p.terminate()
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()
            time.sleep(3)


def main():
    args = sys.argv[1:]
    if args == ["--list"]:
        for k, (exe, fl) in CONFIGS.items():
            print(f"{k:22s} {Path(exe).parent.name:16s} {' '.join(fl)}")
        return
    names = args or list(CONFIGS)
    unknown = [n for n in names if n not in CONFIGS]
    if unknown:
        sys.exit(f"Unknown config(s): {unknown}. Use --list.")

    if local_llama._server_pids():
        sys.exit("llama-server is running. Stop it first (tray menu or `local_llama.py stop-server`).")
    used = vram_mib()
    if used > MAX_FOREIGN_VRAM_MIB:
        sys.exit(f"GPU busy: {used} MiB already in use (limit {MAX_FOREIGN_VRAM_MIB}). "
                 f"Finish the other GPU job (e.g. ComfyUI) first.")

    model = next((m for m in local_llama.discover_models().values() if m["gguf"].name == MODEL), None)
    if model is None or model["mmproj"] is None:
        sys.exit(f"{MODEL} or its mmproj not found under {local_llama.MODELS_ROOT}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = OUT_DIR / stamp
    log_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in names:
        exe, flags = CONFIGS[name]
        print(f"[{datetime.now():%H:%M:%S}] {name} ...", flush=True)
        row = run_config(name, exe, flags, model["gguf"], model["mmproj"], log_dir)
        row["baseline_vram_mib"] = used
        rows.append(row)
        print("   ", {k: (round(v, 1) if isinstance(v, float) else v) for k, v in row.items()
                      if k not in ("flags", "vision_answer")}, flush=True)

    fields = ["config", "exe", "flags", "load_s", "vram_loaded_mib", "vram_peak_mib",
              "decode_tps_median", "decode_tps_runs", "mixed_tps_median", "mixed_tps_runs", "long_prompt_n", "long_prompt_tps",
              "vision_prompt_n", "vision_prompt_tps", "vision_decode_tps", "vision_answer",
              "baseline_vram_mib", "error"]
    with open(log_dir / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    base = next((r.get("decode_tps_median") for r in rows if r["config"] == "base"), None)
    md = ["| config | VRAM peak GB | decode t/s (median) | vs base | mixed t/s (median) | prompt t/s | vision prompt t/s | vision decode t/s |",
          "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            md.append(f"| {r['config']} | – | error: {r['error']} | | | | | |")
            continue
        rel = f"{r['decode_tps_median'] / base:.2f}x" if base else "–"
        md.append(f"| {r['config']} | {r['vram_peak_mib'] / 1024:.1f} | {r['decode_tps_median']:.1f} "
                  f"({r['decode_tps_runs']}) | {rel} | {r['mixed_tps_median']:.1f} ({r['mixed_tps_runs']}) | "
                  f"{r['long_prompt_tps']:.0f} | "
                  f"{r['vision_prompt_tps']:.0f} | {r['vision_decode_tps']:.1f} |")
    (log_dir / "results.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\nSaved to {log_dir}")


if __name__ == "__main__":
    main()
