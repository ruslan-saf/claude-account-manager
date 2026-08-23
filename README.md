# Running two Claude Pro accounts (plus a free-model lane) side by side on Windows

Two Claude Pro subscriptions cost less than one Claude Max 5x subscription and, for a workload
that rarely brushes against the Max ceiling anyway, cover more real usage. The problem was never
the money — it was that Claude Desktop only lets you be logged into one account at a time. This
is the setup that ended up working: two fully independent Desktop windows, each on its own
account, plus a terminal lane routed through free/cheap models for the routine technical work,
all switchable from one system-tray menu.

## Requirements

- Windows 11 (the process-matching and window-minimize tricks in Part 4 use Win32 APIs).
- Python 3.10+, with the `py` launcher available (`py -3` on PATH) — used both by the app itself
  and by the autostart `.vbs` to find `pythonw.exe` dynamically.
- Claude Desktop (Microsoft Store build) for Part 1; a Claude Pro/Max account.
- [OmniRoute](https://github.com/diegosouzapw/OmniRoute) (`npm install -g omniroute`) for Part 2/3
  and for the local-model CLI route in Part 5.
- Optional, for Part 5 only: a [llama.cpp](https://github.com/ggml-org/llama.cpp) build
  (`llama-server.exe`) and at least one GGUF model, e.g. downloaded via
  [LM Studio](https://lmstudio.ai/).

## Quick start

```powershell
git clone https://github.com/<your-fork>/claude-account-manager.git
cd claude-account-manager
pip install -r requirements.txt
python tray_manager.py
```

A tray icon appears with the menu described in Part 4 below. To also autostart it at login, edit
`Claude_Account_Manager.vbs` if you cloned the repo somewhere other than
`~/Documents/GitHub/claude-account-manager`, then drop a shortcut to it in
`shell:startup`.

If your `llama-server.exe` isn't at `C:/llama.cpp/llama-server.exe` (the default `local_llama.py`
looks for), set `LLAMACPP_SERVER_EXE` to its actual path before launching the tray app, or as a
permanent user environment variable.

## The boundary that shaped this design

Since early 2026, Anthropic has actively blocked third-party tools from using a Claude Pro/Max
subscription's OAuth token outside the official Claude Code / claude.ai client — full
enforcement landed in April 2026. This targets a specific pattern: a relay-style proxy that
holds one or more subscription tokens centrally and re-serves them to other tools. It does
**not** target simply owning more than one personal account.

That distinction is why this setup looks the way it does. A gateway tool that logs into your
Claude account and re-serves that session — the kind of thing that promises to "pool" two Pro
subscriptions into one bigger one — sits squarely in the blocked category, was tested here, and
was deliberately left out of the final design. Everything below is either the *official client
logging in normally, one account at a time*, or a *gateway that only ever talks to models that
were never Anthropic's to begin with* (DeepSeek, OpenRouter, Gemini, all with their own API
keys). Neither of those touches a subscription token in a way Anthropic's policy cares about.

## Part 1 — Two Desktop windows, two accounts

Claude Desktop is an Electron app, so it understands the standard `--user-data-dir` flag, which
relocates where it stores the login session and local cache. Point two launches at two
different folders and you get two independent, simultaneously open windows:

```powershell
$exe = Join-Path (Get-AppxPackage *claude*).InstallLocation "app\Claude.exe"

# Account 1: just launch normally
& $exe

# Account 2: isolated profile
& $exe --user-data-dir="$env:USERPROFILE\.claude-second"
```

`Get-AppxPackage` is doing the work of finding the install path, because the Microsoft Store
build lives under a versioned, unpredictable directory.

The one snag: logging into the second window via Google/SSO can send the auth token back to the
*first*, already-open window instead — a known quirk of how the OAuth redirect resolves when two
instances are open. The fix is to sign in with **email + one-time code** on the second window
instead of SSO.

Skills, `CLAUDE.md`, and settings are unaffected by any of this — `--user-data-dir` only
relocates the browser-session part of the profile, not Claude Code's own config directory, so
both windows (and the terminal) see the same skill set automatically.

## Part 2 — A free/cheap-model gateway, for the terminal only

The natural instinct is to make this a *third* Desktop window too — Claude Desktop does have a
built-in "route through a third-party gateway" feature (Developer settings → Configure
Third-Party Inference). It turns out to be the wrong tool here: its configuration lives in one
file shared by the entire Desktop installation, not scoped to a `--user-data-dir` profile, so
turning it on affects *every* open window at once — incompatible with keeping two native-account
windows open. The gateway lane lives in a terminal `claude` session instead, where its
configuration is just an environment variable read at process start, independent per window.

The gateway itself is [OmniRoute](https://github.com/diegosouzapw/OmniRoute), a local
AI-request router. Two things are worth fixing before pointing real traffic at it:

- **Default password.** A fresh install's dashboard password is `CHANGEME` until you set one —
  and setting `INITIAL_PASSWORD` in its `.env` *after* the first run has no effect, because the
  password only gets written to the database once. Reset it directly:
  `printf '%s' 'a-real-password' | omniroute reset-password --password-stdin`.
- **Network exposure.** It binds `0.0.0.0` by default. On a machine with a permissive existing
  firewall rule for Node.js (common — many dev tools create one), that means the dashboard is
  reachable from the whole local network by default, and its own "is this local?" check only
  looks at the HTTP `Host` header, which is trivial to spoof — not a real boundary. Set
  `OMNIROUTE_SERVER_HOST=127.0.0.1` in its `.env` to bind loopback-only instead.

From there: add real provider API keys (DeepSeek is cheap and reliable; OpenRouter unlocks a
rotating set of genuinely free models; Gemini's free tier is generous but occasionally returns
an empty response on tool-heavy requests), and chain them into a priority-ordered **combo**
rather than trusting OmniRoute's built-in `auto` routing across its keyless free providers —
that pool turned out to be unreliable enough in testing (30–60+ second requests, frequent
429s/502s, occasional full `503` after exhausting every candidate) to be worse than just picking
two or three known-good targets and letting the combo fail over between them.

## Part 3 — Getting Claude Code CLI to actually use it

This is the part that took the longest to get right, and every failure mode looked identical
from the outside ("something's wrong with the model") while having a completely different cause:

**Failure 1 — the CLI hijacks your first message into a menu.** Naming the gateway's model
something that resembles a real (even retired) Anthropic model id — `claude-sonnet-4-5` was the
one tried here — makes Claude Code intercept it at startup with an interactive "a newer version
is available, pick one" prompt. Your first typed message gets consumed as a menu selection, not
a chat turn, and nothing reaches the gateway. Fix: pick a name that looks nothing like an
Anthropic id.

**Failure 2 — accepted, but rejected on first use.** With a clean custom name, Claude Code shows
it as the active model in the startup banner — looks fine — but the first real message returns
*"There's an issue with the selected model... it may not exist or you may not have access to
it,"* and the gateway's own request log shows nothing arrived at all. Claude Code validates any
model string set via `ANTHROPIC_MODEL` against a recognized list before ever sending a request —
a known alias, anything starting with `claude-`, or a name explicitly registered as a custom
option. The fix, straight from Anthropic's own docs, is one more environment variable:
`ANTHROPIC_CUSTOM_MODEL_OPTION=<same custom name>`, which explicitly skips that check.

**Failure 3 — everything above fixed, still hitting real Anthropic.** `/status` inside the
session showed `Anthropic base URL: https://api.anthropic.com` — the real endpoint — despite
`settings.json` correctly pointing at the gateway. The cause: a real environment variable in the
*process that launched* `claude` (inherited from a parent shell, in this case) silently
overrides whatever `settings.json`'s `env` block says. The fix is defensive: launch the gateway
session from a small script that explicitly clears the relevant `ANTHROPIC_*` variables first,
so nothing inherited can leak through — see `open_gateway.bat` in this repo.

One more thing worth knowing: `/status`'s Usage tab, which shows a running cost and a percentage
of your weekly limit, is a **local estimate** built from tokens the CLI itself has observed —
it explicitly warns costs "may be inaccurate" for a model it doesn't recognize as Anthropic's
own, and it does not reflect real subscription billing. Don't use it to sanity-check whether the
gateway is actually being hit. The one number that can't lie is the gateway's own request log,
timestamp-matched to when you sent the message.

## Part 4 — The tray manager

`tray_manager.py` in this repo ties the three lanes together: open, minimize, or close each
Desktop account by name, and one-click launch a terminal already switched to gateway or native
mode. A few implementation details that weren't obvious going in:

- Both Desktop windows run as a process literally named `Claude.exe` with the same window
  title, so distinguishing "Account 1's window" from "Account 2's window" for close/minimize
  can't rely on process name or title — it has to inspect each process's **command line** for
  `--user-data-dir`, via WMI (`Get-CimInstance Win32_Process`), then act on the matching PIDs.
  Minimize specifically needs `pywin32`'s `EnumWindows` to find the actual window handle for
  those PIDs and call `ShowWindow(..., SW_MINIMIZE)` directly — there's no per-window API at
  the process level.
- A tray icon drawn with Pillow's default font is essentially invisible at real tray-icon size;
  load an actual TrueType font and size it deliberately.
- Building a `python ... on & claude` command as one Python string and passing it through
  `cmd /k` inside `subprocess.Popen([...])` runs into Windows' nested-quoting rules and silently
  opens a blank shell instead of running anything. Writing the same two commands to a plain
  `.bat` file and launching *that* sidesteps it entirely.

**Not built yet**: a mini dashboard showing each account's remaining weekly quota, to
recommend which one to use right now. There's no headless way to pull that number — it only
renders inside the interactive `/status` screen, and the terminal CLI's own auth status command
can't see which account a given Desktop window is logged into (they're separate auth surfaces
entirely). Scripting the interactive TUI and parsing its output is the likely path, and it's
its own project.

## Part 5 — A fourth lane: a local model, no subscription at all

The first four lanes all still depend on a Claude subscription somewhere. The fourth doesn't:
a GGUF model running locally through `llama-server` (from llama.cpp), exposed from the same
tray menu under **Local Models**, with three things it's useful for — picking which downloaded
model to run, starting it standalone so another tool can talk to it directly, and pointing
Claude Code CLI at it.

The model list isn't hardcoded. The tray submenu scans the LM Studio downloads folder every
time it opens, so a model downloaded or deleted there shows up or disappears without editing
anything. Whichever one is running, it's always served under the same fixed alias
(`local-model-llama-cpp`) regardless of which file is actually behind it — a small trick that pays off
twice: another local harness already configured to talk to that alias keeps working no matter
which model is currently loaded, and the OmniRoute wiring built for the CLI route (below) only
has to be built once, ever, instead of being re-pointed every time the model changes.

Pointing Claude Code at it needed the same trick as the free-model gateway lanes: OmniRoute in
between, translating between the Anthropic Messages API (what Claude Code speaks) and
OpenAI-compatible chat completions (what llama-server speaks). The one new wrinkle was that
"a self-hosted OpenAI-compatible endpoint at an arbitrary URL" isn't one of OmniRoute's
catalog provider types — it needed a lower-level **provider node** (an internal concept for
attaching a custom base URL, with its own required `prefix` and `apiType` fields), and that
node in turn needed its own credential registered separately, keyed on the node's own
generated id rather than anything resembling a normal provider name. None of it was
documented anywhere obvious; all of it was found by reading what each REST error demanded
next until the requests stopped failing.

The other thing worth knowing before trying this: it's marked **experimental** in the tray
menu, deliberately. Claude Code's full system prompt — every tool, every installed skill's
description, every MCP server's instructions — turned out not to be a fixed size at all:
three separate fresh sessions measured 37,590 / 58,167 / 87,243 tokens on this machine, which
has a large number of skills and MCP servers registered. Two different failures came out of
that. The smaller end is just slow — a 37K-token prompt succeeded, but only after a run of
failed requests first, consistent with Claude Code retrying a request that was still
legitimately being prefilled rather than anything actually broken; the fix there is patience,
30-90+ seconds for a first message. The larger end doesn't fit at all — an 87K-token prompt
hit `request (87243 tokens) exceeds the available context size (65536 tokens)` outright, a
context window can't be raised past a point without more VRAM, and there's no single size
that's safe against a number that isn't stable. Picking a model and starting the server for
another harness (Hermes) doesn't have any of this trouble — Hermes' own prompt is small and
fixed by design. Only the Claude-Code-CLI-specific route runs into it, which is exactly why
that one option carries the warning and the other two don't.

## Why write this down

None of the individual pieces here are secret, but stitching them into "two subscriptions plus a
free-tier lane, running at once, without crossing Anthropic's actual policy line" took a lot of
trial and error even with a lot of documentation in hand — three near-identical-looking failure
modes in Part 3 alone, each needing a different one-line fix. If it was this non-obvious to work
through once, it's probably worth writing up properly for anyone else trying the same thing.
