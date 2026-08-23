#!/usr/bin/env python3
"""Toggle Claude Code CLI between OmniRoute (local gateway) and native Anthropic.

Usage:
    python toggle_gateway.py on [base_url] [auth_token] [model]
    python toggle_gateway.py off
    python toggle_gateway.py status

Edits the "env" block of ~/.claude/settings.json. Home directory is resolved
at runtime (Path.home()) so this works unchanged on any machine/user account.

IMPORTANT: ANTHROPIC_BASE_URL is read once when the Claude Code process starts.
Editing settings.json here does not affect an already-running session -- the
user must exit and relaunch `claude` for the change to take effect.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
DEFAULT_BASE_URL = "http://localhost:20128"
DEFAULT_MODEL = "omni-default"
BACKUP_KEY = "_omniroute_backup"
ENV_KEYS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
    "ANTHROPIC_CUSTOM_MODEL_OPTION", "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
)


def load_settings():
    if SETTINGS_PATH.exists():
        text = SETTINGS_PATH.read_text(encoding="utf-8")
        return json.loads(text) if text.strip() else {}
    return {}


def save_settings(data):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def gateway_reachable(base_url, timeout=1.5):
    try:
        urllib.request.urlopen(base_url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        # Server responded (even with an error status) -> it's up.
        return True
    except Exception:
        return False


def cmd_status(settings):
    env = settings.get("env", {})
    active = env.get("ANTHROPIC_BASE_URL") == DEFAULT_BASE_URL
    if active:
        print(f"OmniRoute: ON  (ANTHROPIC_BASE_URL={env.get('ANTHROPIC_BASE_URL')}, "
              f"ANTHROPIC_MODEL={env.get('ANTHROPIC_MODEL', '<default>')})")
    else:
        print("OmniRoute: OFF (native Anthropic)")
    print(f"settings file: {SETTINGS_PATH}")


def cmd_on(settings, base_url, auth_token, model):
    env = settings.setdefault("env", {})

    if BACKUP_KEY not in settings:
        settings[BACKUP_KEY] = {k: env[k] for k in ENV_KEYS if k in env}

    env["ANTHROPIC_BASE_URL"] = base_url
    if auth_token:
        env["ANTHROPIC_AUTH_TOKEN"] = auth_token
    if model:
        env["ANTHROPIC_MODEL"] = model
        # Claude Code rejects any model string it doesn't recognize (alias,
        # claude-* prefix, or a registered custom option) before ever
        # sending the request. ANTHROPIC_CUSTOM_MODEL_OPTION registers our
        # OmniRoute combo name and explicitly skips that validation.
        env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = model
        env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = "OmniRoute (free/cheap gateway)"
        env["ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"] = "Routed through local OmniRoute gateway"

    save_settings(settings)

    print(f"OmniRoute enabled -> {base_url} (model={model})")
    print(f"Written to {SETTINGS_PATH}")
    if not gateway_reachable(base_url):
        print(f"WARNING: could not reach {base_url}. Make sure the OmniRoute "
              f"server is running (e.g. `omniroute` in another terminal) "
              f"before you restart Claude Code.")
    print("Exit and relaunch `claude` for this to take effect (env vars are read once at startup).")


def cmd_off(settings):
    env = settings.setdefault("env", {})
    backup = settings.pop(BACKUP_KEY, {})

    for key in ENV_KEYS:
        if key in backup:
            env[key] = backup[key]
        else:
            env.pop(key, None)

    if not env:
        settings.pop("env", None)

    save_settings(settings)

    print(f"Native Anthropic restored in {SETTINGS_PATH}")
    print("Exit and relaunch `claude` for this to take effect (env vars are read once at startup).")


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("on", "off", "status"):
        print(__doc__)
        sys.exit(1)

    action = args[0]
    settings = load_settings()

    if action == "status":
        cmd_status(settings)
    elif action == "on":
        base_url = args[1] if len(args) > 1 else DEFAULT_BASE_URL
        auth_token = args[2] if len(args) > 2 else None
        model = args[3] if len(args) > 3 else DEFAULT_MODEL
        cmd_on(settings, base_url, auth_token, model)
    elif action == "off":
        cmd_off(settings)


if __name__ == "__main__":
    main()
