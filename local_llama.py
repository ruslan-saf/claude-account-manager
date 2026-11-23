"""Manage a local llama.cpp server for Claude Account Manager.

Three things this enables:
  1. Pick which downloaded GGUF model to serve -- the list is built by
     scanning the models folder (C:/llama.cpp/models, <publisher>/<model>/
     *.gguf) every time, so a model you put there or delete shows up (or
     disappears) automatically, no registry to maintain by hand.
  2. Start the llama-server process on its own, so any OpenAI-compatible
     harness (e.g. Hermes Agent) can connect to it directly.
  3. Point Claude Code CLI at it (via the OmniRoute gateway, since Claude
     Code speaks the Anthropic Messages API and llama-server only speaks
     OpenAI-compatible chat completions -- OmniRoute translates between
     the two, the same way it already does for the free/cheap-model lane).

Every model is served under the SAME fixed alias "local-model-llama-cpp", regardless
of which GGUF is actually loaded. This is deliberate: Hermes Agent's
config.yaml (see the multi-account-gateway skill / README_1.md) already
hardcodes "local-model-llama-cpp" as the model name, and the OmniRoute combo set up
for CLI routing ("local-model") also hardcodes it. Keeping the alias fixed
means switching the underlying model here never requires touching Hermes'
config or OmniRoute's combo again -- both already point at "local-model-llama-cpp"
and will keep working no matter which model is loaded. `status` always
prints which real model is behind that alias so this doesn't get confusing.

Only one model can be loaded at a time (single GPU, one port). Starting a
different model automatically stops whichever one is currently running.

The llama-server build and the models folder sit outside the user profile
(C:/llama.cpp), so this works unchanged on either of the user's machines;
both can be moved with the LLAMACPP_SERVER_EXE / LLAMACPP_MODELS_DIR env vars.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent
# Override with the LLAMACPP_SERVER_EXE env var if llama-server lives
# somewhere other than the default build location below.
LLAMA_SERVER_EXE = Path(os.environ.get("LLAMACPP_SERVER_EXE", "C:/llama.cpp/llama-server.exe"))
# GGUF models live next to the llama.cpp build; override with LLAMACPP_MODELS_DIR.
MODELS_ROOT = Path(os.environ.get("LLAMACPP_MODELS_DIR", "C:/llama.cpp/models"))
STATE_PATH = SCRIPT_DIR / "local_llama_state.json"

HOST = "127.0.0.1"
PORT = 8080
BASE_URL = f"http://{HOST}:{PORT}"
ALIAS = "local-model-llama-cpp"  # fixed on purpose -- see module docstring

CREATE_NO_WINDOW = 0x08000000

GENERIC_FLAGS = [
    "--ctx-size", "65536",
    "--n-gpu-layers", "999",
    "--flash-attn", "on",
    "--cache-type-k", "q4_0",
    "--cache-type-v", "q4_0",
    "--jinja",
    "--no-context-shift",
]

# 65536 isn't just Hermes' minimum -- Claude Code's own system prompt (tool
# list + every skill description + agent-type list) has been observed here
# at 37K-58K tokens by itself, before any conversation history. A smaller
# default (32768 was tried first) fails outright on the very first message
# with "request (N tokens) exceeds the available context size" -- so every
# discovered model gets 65536 + quantized KV cache (same trick as the tuned
# config below) rather than risking that on models Claude Code CLI is
# actually meant to talk to. n-gpu-layers 999 (full offload) plus 65536 q4
# KV hasn't been VRAM-checked for every possible model size -- if a much
# larger GGUF gets dropped into the folder and fails to load, lower
# --n-gpu-layers for it the way the tuned entry below does for Qwen3.
#
# Only this exact file additionally disables reasoning-format text leaking
# into `content` in a way Hermes needs (see README_1.md in this repo / the
# claude-multi-account-gateway skill) and offloads 4/64 layers to CPU --
# tuned specifically for Hermes' 16GB VRAM budget alongside other GPU load.
TUNED_FLAGS = {
    "Qwen3.8-27B-Q3_K_M.gguf": [
        "--ctx-size", "65536",
        "--n-gpu-layers", "60",
        "--flash-attn", "on",
        "--cache-type-k", "q4_0",
        "--cache-type-v", "q4_0",
        "--jinja",
        "--reasoning-format", "none",
        "--no-context-shift",
    ],
    # Vision + agentic lane, and the one to reach for when a harness drives a
    # browser or the desktop through screenshots. Qwen3-VL-8B is multimodal,
    # so it needs the projector alongside it -- _find_mmproj attaches that
    # automatically, no flag here.
    #
    # Kept as the fast option: it is the only entry here that fits entirely in
    # VRAM with the projector loaded (10.8 GB) and generates at ~96 tokens/s.
    # Use it for short, well-scoped work. For anything long enough that the
    # model has to reason about tool feedback, use the 30B entry below -- this
    # one stops drawing conclusions from errors well before the context fills.
    #
    # Two lessons from the model this lane started with (gpt-oss-20b, since
    # removed) are why it is a Qwen3-VL and not something else. A text-only
    # model is useless to a harness that drives a browser through screenshots:
    # every one came back as "image input is not supported", 62 times in one
    # session. And gpt-oss speaks Harmony, a channel-tagged format the
    # abliterated build did not emit reliably under load -- 44
    # "common_chat_peg_parse: unparsed peg-native output" warnings in the same
    # session, each surfacing to the harness as an APIError after three
    # retries. Qwen3-VL emits ordinary ChatML tool calls, which survive
    # abliteration intact.
    #
    # 65536 matches the context_length Hermes has configured for this
    # endpoint, and leaves VRAM headroom for image processing, which
    # allocates on top of the KV cache. Weights 5.45 GB, projector 1.08 GB,
    # KV about 2.7 GB.
    #
    # Sampling follows Qwen's published recommendations for the Instruct line,
    # with one addition made after watching it fail. repeat-penalty stays at
    # 1.0 (Qwen degrades under the sequence-level damping that suits other
    # models), but leaving every penalty off let the model fall into a
    # generation loop while writing a long tool argument: it emitted the same
    # commented-out line over and over inside a browser_exec code payload
    # until the JSON was unusable, and Hermes logged "Unrepairable tool_call
    # arguments for browser_exec -- replaced with empty object". Qwen's own
    # guidance for exactly that symptom is presence-penalty rather than
    # repeat-penalty, so 1.5 is set here. It discourages reusing a token at
    # all without penalizing the repeated structure that valid code and JSON
    # legitimately contain.
    "Huihui-Qwen3-VL-8B-Instruct-abliterated-Q5_K_M.gguf": [
        "--ctx-size", "65536",
        "--n-gpu-layers", "999",
        "--flash-attn", "on",
        "--cache-type-k", "q4_0",
        "--cache-type-v", "q4_0",
        "--jinja",
        "--no-context-shift",
        "--temp", "0.7",
        "--top-p", "0.8",
        "--top-k", "20",
        "--repeat-penalty", "1.0",
        "--presence-penalty", "1.5",
    ],
    # Uncensored agentic lane, bigger sibling of the 8B entry above and the
    # one to use when a task runs long enough that following instructions
    # matters more than raw speed. Mixture-of-Experts: ~30B total parameters
    # but only ~3B active per token.
    #
    # It replaced Qwen3-VL-8B after watching the 8B fail the same way twice in
    # one Hermes session. It did not malform anything -- it simply ignored
    # tool feedback: it repeated a computer_use call that had already failed
    # with "Permission denied" until Hermes cut it off ("the same tool call
    # failed 5 times with identical arguments"), then immediately did the same
    # with skill_manage, eight identical retries against an error that was
    # never going to change. Both turns ended in guardrail_halt. That is not a
    # formatting problem any sampler setting fixes; it is the model not
    # drawing conclusions from what it reads.
    #
    # Q4_K_M rather than a smaller quant on purpose. IQ4_XS (15.2 GB) and
    # Q3_K_M (13.7 GB) would both fit with less offload, but quantization
    # below Q4 degrades instruction-following first, and instruction-following
    # is the exact thing being bought here.
    #
    # At 17.3 GB the weights do not fit in 16 GB, so --n-cpu-moe keeps the
    # expert tensors of the first N layers in system RAM while attention, the
    # projector and the remaining experts stay on the GPU. Only 8 of 128
    # experts fire per token, so the CPU share of the work is far smaller than
    # the parameter share suggests.
    #
    # N=24 was measured against N=18 on this machine rather than guessed:
    #   18 -> 15.65 GB VRAM (726 MB spare), 32.4 t/s text, 41.8 t/s vision
    #   24 -> 13.92 GB VRAM (2.4 GB spare), 30.5 t/s text, 37.6 t/s vision
    # Roughly 6-10% of the speed buys 1.7 GB of headroom. Worth it: image
    # processing allocates on top of the KV cache, and 726 MB is not enough
    # margin for a large screenshot plus whatever else is using the GPU.
    # Lower N for speed if the GPU is otherwise idle, raise it if a big image
    # ever triggers an allocation failure.
    "Huihui-Qwen3-VL-30B-A3B-Instruct-abliterated-Q4_K_M.gguf": [
        "--ctx-size", "65536",
        "--n-gpu-layers", "999",
        "--n-cpu-moe", "24",
        "--flash-attn", "on",
        "--cache-type-k", "q4_0",
        "--cache-type-v", "q4_0",
        "--jinja",
        "--no-context-shift",
        "--temp", "0.7",
        "--top-p", "0.8",
        "--top-k", "20",
        "--repeat-penalty", "1.0",
        "--presence-penalty", "1.5",
    ],
    # Successor of the Qwen3-VL-30B entry above and the default vision +
    # agentic lane since 2026-11-13. Same shape (MoE, 35B total / ~3B active,
    # native vision encoder, ChatML tool calls), one generation newer (Qwen3.6,
    # April 2026): Qwen reports clear gains over Qwen3.5-35B-A3B on agentic
    # benchmarks (Terminal-Bench 2.0 51.5 vs 40.5, MCPMark 37.0 vs 27.0), and
    # Qwen3.5-35B-A3B itself already scored 54.5 on OSWorld-Verified.
    # Source GGUF: mradermacher/Huihui-Qwen3.6-35B-A3B-abliterated-GGUF; the
    # projector was saved as mmproj-f16.gguf so _find_mmproj picks it up.
    #
    # Q4_K_M for the same reason as the 30B entry: instruction-following is
    # what this lane is for, and it degrades first below Q4.
    #
    # Qwen3.6 thinks by default. Thinking is switched off through the chat
    # template (enable_thinking=false) because Hermes caps max_tokens at 4096
    # for this endpoint and hides reasoning anyway; a long think block would
    # eat the budget before the tool call is written. This keeps the lane
    # behaving like the Instruct model it replaces. Drop the two
    # --chat-template-kwargs items to turn thinking back on.
    #
    # Speed flags, measured with bench_llama.py (raw numbers in bench_results/;
    # RTX 4080 SUPER 16 GB, i7-14700KF 8P+12E, desktop using ~1.5-2 GB VRAM):
    #   llama.cpp build 12613 instead of 10502 (LLAMACPP_SERVER_EXE points at
    #       it): 29.0 -> 34.8-37.1 t/s decode with identical flags.
    #   --threads 8  the 8 P-cores only; the default also schedules E-cores,
    #       which hold the P-cores back on the CPU-side expert matmuls.
    #   --n-cpu-moe 20  three more MoE layers on the GPU than the old 23.
    # RAM matters because the remaining experts run on the CPU: after XMP was
    # enabled (DDR4 2667 -> 3600, 2026-11-23) and with Logi Options+ closed
    # (its agent kept one core busy), this setting measured
    # (bench_results/20261123-232128):
    #   decode, four unrelated prompts   70.1 t/s (55.8 before XMP)
    #   1920x1080 screenshot             prompt 570 t/s, answer 71.8 t/s
    #   15.5k-token prompt               869 t/s
    #   VRAM peak                        14.0 GB
    # The alternative --cpu-moe --moe-cache-mib 8000 (build >= 12613 only;
    # experts in RAM, the 8 GB most used of them cached in VRAM) is faster on
    # text (80.1 t/s on the same four prompts, 12.8 GB VRAM) but slower and
    # noisier on screenshots (prompt 479 t/s, answer 61.3 t/s). Estimated time
    # for one computer-use step (screenshot + 100-300 token answer, history
    # reused from the prompt cache): 5.1-7.8 s here vs 6.0-9.2 s with the cache,
    # so this lane, which is mostly screenshots, uses --n-cpu-moe 20. Switch to
    # the cache flags for text-only work. q8_0 KV was within noise of q4_0.
    #
    # Coordinates come back on Qwen's normalized 0-1000 grid (a button centred
    # at 960,550 on 1920x1080 was reported as 498,508), the same convention as
    # Qwen3-VL, so harness-side scaling does not change.
    "Huihui-Qwen3.6-35B-A3B-abliterated.Q4_K_M.gguf": [
        "--ctx-size", "65536",
        "--n-gpu-layers", "999",
        "--n-cpu-moe", "20",
        "--threads", "8",
        "--flash-attn", "on",
        "--cache-type-k", "q4_0",
        "--cache-type-v", "q4_0",
        "--jinja",
        "--no-context-shift",
        "--chat-template-kwargs", '{"enable_thinking": false}',
        "--temp", "0.7",
        "--top-p", "0.8",
        "--top-k", "20",
        "--repeat-penalty", "1.0",
        "--presence-penalty", "1.5",
    ],
}


def _slug(rel_path: Path) -> str:
    text = str(rel_path).replace("\\", "/")
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    stem = rel_path.stem.lower()
    stem = "".join(c if c.isalnum() else "-" for c in stem).strip("-")[:40]
    return f"{stem}-{digest}"


def _find_mmproj(model_path: Path) -> Path | None:
    """Returns the vision projector that belongs to a model, or None for a
    text-only model.

    A multimodal GGUF is useless without its projector: llama-server loads
    the language weights, then rejects every image with "image input is not
    supported - hint: if this is unexpected, you may need to provide the
    mmproj". The tool calling a harness does around that failure still works,
    so the model looks like it is running fine while being blind -- which is
    exactly how this went unnoticed until an agent was asked to drive a
    browser through screenshots.

    Projectors ship beside the model in the same folder, named mmproj-*.gguf.
    Where several precisions exist, F16 is preferred over BF16 over F32: F32
    is twice the VRAM for no visible quality gain at this scale.
    """
    candidates = sorted(model_path.parent.glob("mmproj-*.gguf"))
    if not candidates:
        return None
    for tag in ("f16", "bf16"):
        for c in candidates:
            if tag in c.stem.lower():
                return c
    return candidates[0]


def discover_models() -> dict:
    """Scans MODELS_ROOT for *.gguf files. Skips mmproj-* helper files
    (vision projectors, not standalone chat models) -- those are attached to
    the model they belong to by _find_mmproj instead."""
    models = {}
    if not MODELS_ROOT.exists():
        return models
    for path in sorted(MODELS_ROOT.rglob("*.gguf")):
        if path.name.lower().startswith("mmproj-"):
            continue
        rel = path.relative_to(MODELS_ROOT)
        model_id = _slug(rel)
        size_gb = path.stat().st_size / (1024 ** 3)
        flags = TUNED_FLAGS.get(path.name, GENERIC_FLAGS)
        tuned = path.name in TUNED_FLAGS
        mmproj = _find_mmproj(path)
        vision = " [vision]" if mmproj else ""
        models[model_id] = {
            "label": f"{path.stem} ({size_gb:.1f} GB){' [tuned]' if tuned else ''}{vision}",
            "gguf": path,
            "mmproj": mmproj,
            "flags": flags,
        }
    return models


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict):
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def selected_model_id(models: dict) -> str | None:
    """Returns the stored selection if that file still exists, else the
    first discovered model, else None if nothing is installed."""
    stored = load_state().get("selected_model")
    if stored in models:
        return stored
    return next(iter(models), None)


def cmd_list():
    models = discover_models()
    if not models:
        print(f"No .gguf models found under {MODELS_ROOT}")
        return
    selected = selected_model_id(models)
    for model_id, cfg in models.items():
        marker = "*" if model_id == selected else " "
        print(f"{marker} {model_id:24s} {cfg['label']}")


def cmd_select(model_id: str):
    models = discover_models()
    if model_id not in models:
        print(f"Unknown model '{model_id}'. Run `list` to see current choices.")
        sys.exit(1)
    state = load_state()
    state["selected_model"] = model_id
    save_state(state)
    print(f"Selected: {models[model_id]['label']}")


def _server_pids() -> list[int]:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
         "Select-Object -ExpandProperty ProcessId"],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    return [int(line) for line in result.stdout.split() if line.strip().isdigit()]


def _query_loaded_alias() -> str | None:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/v1/models", timeout=2) as resp:
            data = json.loads(resp.read())
        return data["data"][0]["id"]
    except Exception:
        return None


def _health_ok() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=2) as resp:
            return json.loads(resp.read()).get("status") == "ok"
    except Exception:
        return False


def cmd_status():
    models = discover_models()
    model_id = selected_model_id(models)
    if model_id is None:
        print(f"No .gguf models found under {MODELS_ROOT}")
    else:
        print(f"Selected model: {models[model_id]['label']} ({model_id})")

    pids = _server_pids()
    if not pids:
        print("llama-server: NOT RUNNING")
        return
    print(f"llama-server: RUNNING (pid {', '.join(map(str, pids))})")
    if _health_ok():
        print(f"  health: ok, listening on {BASE_URL}")
    else:
        print("  health: not ready yet (still loading?)")
    loaded = _query_loaded_alias()
    if loaded:
        print(f"  serving under alias: {loaded}")


def cmd_stop_server():
    pids = _server_pids()
    if not pids:
        print("llama-server is not running.")
        return
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True, creationflags=CREATE_NO_WINDOW)
    print(f"Stopped llama-server (pid {', '.join(map(str, pids))}).")


def _build_command(model_id: str, models: dict) -> list[str]:
    cfg = models[model_id]
    if not cfg["gguf"].exists():
        print(f"ERROR: model file not found: {cfg['gguf']}")
        sys.exit(1)
    if not LLAMA_SERVER_EXE.exists():
        print(f"ERROR: llama-server not found: {LLAMA_SERVER_EXE}")
        print("Set LLAMACPP_SERVER_EXE to its actual path if it's installed elsewhere.")
        sys.exit(1)
    cmd = [
        str(LLAMA_SERVER_EXE),
        "--model", str(cfg["gguf"]),
        "--host", HOST, "--port", str(PORT),
        "--alias", ALIAS,
        *cfg["flags"],
    ]
    if cfg.get("mmproj"):
        cmd += ["--mmproj", str(cfg["mmproj"])]
    return cmd


def cmd_start_server_fg(model_id: str | None = None):
    """Runs llama-server in the foreground of the current console. Blocks
    until the process exits (Ctrl+C, or the window is closed)."""
    models = discover_models()
    model_id = model_id or selected_model_id(models)
    if model_id is None:
        print(f"No .gguf models found under {MODELS_ROOT}")
        sys.exit(1)
    if _server_pids():
        print("llama-server is already running -- stop it first (or use the "
              "tray menu's Start Server, which does this for you).")
        sys.exit(1)
    cmd = _build_command(model_id, models)
    print(f"Starting: {models[model_id]['label']}")
    print(f"Command: {' '.join(cmd)}\n")
    sys.exit(subprocess.run(cmd).returncode)


def cmd_start_server_bg(model_id: str | None = None, timeout: float = 120.0):
    """Ensures the selected model is loaded and healthy, starting or
    restarting llama-server in a new visible window if needed. Used before
    opening the local-model Claude Code CLI session."""
    models = discover_models()
    model_id = model_id or selected_model_id(models)
    if model_id is None:
        print(f"No .gguf models found under {MODELS_ROOT}")
        sys.exit(1)

    loaded = _query_loaded_alias() if _server_pids() else None
    # Same alias is always reported regardless of which model is loaded, so
    # "already running" is good enough -- we don't restart just to swap the
    # underlying GGUF for the same alias. If the user picked a different
    # model since the server last started, they should use Stop+Start.
    if loaded == ALIAS:
        print("llama-server already running and healthy.")
        return
    if _server_pids():
        print("A stale/unhealthy llama-server is running -- stopping it first.")
        cmd_stop_server()
        time.sleep(1)

    cfg = models[model_id]
    if not cfg["gguf"].exists():
        print(f"ERROR: model file not found: {cfg['gguf']}")
        sys.exit(1)

    print(f"Launching llama-server ({cfg['label']}) in a new window...")
    subprocess.Popen(
        ["cmd", "/c", "start", "llama.cpp server (local)",
         sys.executable, str(SCRIPT_DIR / "local_llama.py"), "start-server-fg", model_id],
    )

    print("Waiting for it to become healthy", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _health_ok():
            print(" done.")
            return
        print(".", end="", flush=True)
        time.sleep(2)
    print()
    print(f"ERROR: llama-server did not become healthy within {timeout:.0f}s. "
          f"Check the server window for errors (VRAM contention is the usual cause).")
    sys.exit(1)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    action = args[0]
    if action == "list":
        cmd_list()
    elif action == "select":
        if len(args) < 2:
            print("Usage: local_llama.py select <model_id>")
            sys.exit(1)
        cmd_select(args[1])
    elif action == "status":
        cmd_status()
    elif action == "start-server-fg":
        cmd_start_server_fg(args[1] if len(args) > 1 else None)
    elif action == "start-server-bg":
        cmd_start_server_bg(args[1] if len(args) > 1 else None)
    elif action == "stop-server":
        cmd_stop_server()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
