"""Manage a local llama.cpp server for Claude Account Manager.

Three things this enables:
  1. Pick which downloaded GGUF model to serve -- the list is built by
     scanning the LM Studio models folder every time, so a model you
     download or delete there shows up (or disappears) automatically,
     no registry to maintain by hand.
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

Home directory and the LM Studio models folder are resolved at runtime
(Path.home()) so this works unchanged on either of the user's machines.
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
MODELS_ROOT = HOME / ".lmstudio" / "models"
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
}


def _slug(rel_path: Path) -> str:
    text = str(rel_path).replace("\\", "/")
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    stem = rel_path.stem.lower()
    stem = "".join(c if c.isalnum() else "-" for c in stem).strip("-")[:40]
    return f"{stem}-{digest}"


def discover_models() -> dict:
    """Scans MODELS_ROOT for *.gguf files. Skips mmproj-* helper files
    (vision projectors, not standalone chat models)."""
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
        models[model_id] = {
            "label": f"{path.stem} ({size_gb:.1f} GB){' [tuned]' if tuned else ''}",
            "gguf": path,
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
    return [
        str(LLAMA_SERVER_EXE),
        "--model", str(cfg["gguf"]),
        "--host", HOST, "--port", str(PORT),
        "--alias", ALIAS,
        *cfg["flags"],
    ]


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
