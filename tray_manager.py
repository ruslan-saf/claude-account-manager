"""System tray manager for running multiple Claude Desktop account windows
side by side, plus a quick launcher for a gateway-routed terminal session.

Account 1 (main) uses Claude Desktop's default profile (no --user-data-dir).
Account 2 (second) uses an isolated Electron profile at ~/.claude-second, so
it keeps its own login session independent of the main window.

Home directory is resolved at runtime (Path.home()) so this works unchanged
on either of the user's two machines.
"""
import subprocess
import threading
import time
from pathlib import Path

import pystray
import win32con
import win32gui
import win32process
from PIL import Image, ImageDraw, ImageFont

import local_llama

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent
SECOND_PROFILE_DIR = HOME / ".claude-second"
SECOND_PROFILE_MARKER = ".claude-second"
GATEWAY_BAT = SCRIPT_DIR / "open_gateway.bat"
NATIVE_BAT = SCRIPT_DIR / "open_native.bat"
LOCAL_CLI_BAT = SCRIPT_DIR / "open_local_cli.bat"
START_LOCAL_SERVER_BAT = SCRIPT_DIR / "start_llama_server.bat"
STATUS_LOCAL_SERVER_BAT = SCRIPT_DIR / "status_llama_server.bat"
HELP_TXT = SCRIPT_DIR / "HELP.txt"

CREATE_NO_WINDOW = 0x08000000


def resolve_claude_exe() -> str:
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "(Get-AppxPackage *claude*).InstallLocation",
        ],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    install_dir = result.stdout.strip()
    if not install_dir:
        raise RuntimeError("Could not resolve Claude Desktop install location via Get-AppxPackage")
    return str(Path(install_dir) / "app" / "Claude.exe")


CLAUDE_EXE = resolve_claude_exe()


def launch(profile_dir: Path | None = None):
    args = [CLAUDE_EXE]
    if profile_dir is not None:
        args.append(f"--user-data-dir={profile_dir}")
    subprocess.Popen(args)


def _matching_pids(marker: str | None) -> list[int]:
    """PIDs of Claude.exe processes whose command line matches (or, if marker
    is None, does NOT contain) the given profile marker."""
    if marker:
        where_clause = f"$_.CommandLine -like '*{marker}*'"
    else:
        where_clause = f"$_.CommandLine -notlike '*{SECOND_PROFILE_MARKER}*'"
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='Claude.exe'\" | "
        f"Where-Object {{{where_clause}}} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    return [int(line) for line in result.stdout.split() if line.strip().isdigit()]


def close_matching(marker: str | None):
    for pid in _matching_pids(marker):
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, creationflags=CREATE_NO_WINDOW,
        )


def minimize_matching(marker: str | None):
    pids = set(_matching_pids(marker))
    if not pids:
        return

    def _callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if win32gui.GetWindowText(hwnd) == "":
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in pids:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        return True

    win32gui.EnumWindows(_callback, None)


def open_main(icon, item):
    launch(None)


def close_main(icon, item):
    close_matching(None)


def minimize_main(icon, item):
    minimize_matching(None)


def open_second(icon, item):
    launch(SECOND_PROFILE_DIR)


def close_second(icon, item):
    close_matching(SECOND_PROFILE_MARKER)


def minimize_second(icon, item):
    minimize_matching(SECOND_PROFILE_MARKER)


def open_gateway_terminal(icon, item):
    subprocess.Popen(["cmd", "/c", "start", "Claude (gateway)", str(GATEWAY_BAT)])


def open_native_terminal(icon, item):
    subprocess.Popen(["cmd", "/c", "start", "Claude (native)", str(NATIVE_BAT)])


def _make_select_local_model(model_id):
    def _select(icon, item):
        local_llama.cmd_select(model_id)
    return _select


def _make_is_local_model_selected(model_id):
    def _checked(item):
        models_now = local_llama.discover_models()
        return local_llama.selected_model_id(models_now) == model_id
    return _checked


def _local_models_submenu_items():
    """Single dynamic-menu callable, re-evaluated whenever the menu is
    rebuilt, so models added/removed under local_llama.MODELS_ROOT show
    up or disappear without restarting the tray app. The rebuild itself is
    what _watch_models() below exists to trigger -- see the note there."""
    models = local_llama.discover_models()
    if not models:
        yield pystray.MenuItem(
            f"(no .gguf found under {local_llama.MODELS_ROOT})", None, enabled=False
        )
    for model_id, cfg in models.items():
        yield pystray.MenuItem(
            cfg["label"],
            _make_select_local_model(model_id),
            checked=_make_is_local_model_selected(model_id),
            radio=True,
        )

    yield pystray.Menu.SEPARATOR
    yield pystray.MenuItem("Start Server", start_local_server)
    yield pystray.MenuItem("Stop Server", stop_local_server)
    yield pystray.MenuItem("Server Status", show_local_server_status)
    yield pystray.Menu.SEPARATOR
    yield pystray.MenuItem("Open Claude Code CLI (Local Model) [experimental]", open_local_cli_terminal)


def start_local_server(icon, item):
    subprocess.Popen(["cmd", "/c", "start", "llama.cpp server (local)", str(START_LOCAL_SERVER_BAT)])


def stop_local_server(icon, item):
    subprocess.run(
        ["python", str(SCRIPT_DIR / "local_llama.py"), "stop-server"],
        capture_output=True, creationflags=CREATE_NO_WINDOW,
    )


def show_local_server_status(icon, item):
    subprocess.Popen(["cmd", "/c", "start", "llama.cpp server status", str(STATUS_LOCAL_SERVER_BAT)])


def open_local_cli_terminal(icon, item):
    subprocess.Popen(["cmd", "/c", "start", "Claude (local model)", str(LOCAL_CLI_BAT)])


def open_help(icon, item):
    subprocess.Popen(["notepad.exe", str(HELP_TXT)])


def quit_app(icon, item):
    icon.stop()


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in ("segoeuib.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_icon() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 2, size - 2), fill=(204, 102, 51, 255))
    font = _load_font(38)
    text = "C"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), text, fill=(255, 255, 255, 255), font=font)
    return img


menu = pystray.Menu(
    pystray.MenuItem(
        "Account 1 (Main)",
        pystray.Menu(
            pystray.MenuItem("Open", open_main),
            pystray.MenuItem("Minimize", minimize_main),
            pystray.MenuItem("Close", close_main),
        ),
    ),
    pystray.MenuItem(
        "Account 2 (Second)",
        pystray.Menu(
            pystray.MenuItem("Open", open_second),
            pystray.MenuItem("Minimize", minimize_second),
            pystray.MenuItem("Close", close_second),
        ),
    ),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem("Open Gateway Terminal", open_gateway_terminal),
    pystray.MenuItem("Open Native Terminal", open_native_terminal),
    pystray.MenuItem("Local Models (llama.cpp)", pystray.Menu(_local_models_submenu_items)),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem("Help", open_help),
    pystray.MenuItem("Quit", quit_app),
)

MODEL_POLL_SECONDS = 5


def _watch_models(icon):
    """Rebuilds the tray menu when the set of installed GGUF models changes.

    This is needed because pystray's Win32 backend builds the native menu
    once and only rebuilds it on update_menu() or after a menu item is
    activated -- it does NOT re-evaluate a dynamic submenu when that submenu
    is opened, despite the submenu being defined by a callable. Without this
    watcher, a model downloaded while the tray app is running stays invisible
    in the menu until the app is restarted, which is exactly what happened
    with gpt-oss-20b: the file was on disk and `local_llama.py list` showed
    it, but the tray menu kept showing the three models that existed at
    launch.

    Polling is cheap enough to not be worth optimizing -- discover_models()
    is an rglob over a folder holding a handful of multi-gigabyte files.
    """
    def snapshot():
        # Compare the rendered labels, not just the model ids. An id is a slug
        # of the file name, so it is already final the moment an empty file is
        # created -- watching ids alone rebuilt the menu once at the start of a
        # download, froze "(0.0 GB)" into it, and never refreshed. The label
        # carries the size and the [tuned]/[vision] markers, so it keeps
        # changing until the file is actually complete.
        return {mid: cfg["label"] for mid, cfg in local_llama.discover_models().items()}

    known = snapshot()
    while True:
        time.sleep(MODEL_POLL_SECONDS)
        try:
            current = snapshot()
        except OSError:
            continue  # folder briefly unavailable (e.g. mid-download rename)
        if current != known:
            known = current
            icon.update_menu()


def _setup(icon):
    icon.visible = True
    threading.Thread(target=_watch_models, args=(icon,), daemon=True).start()


if __name__ == "__main__":
    icon = pystray.Icon("claude_account_manager", make_icon(), "Claude Account Manager", menu)
    icon.run(setup=_setup)
