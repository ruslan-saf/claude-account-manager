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
  (`llama-server.exe`, default `C:\llama.cpp`) and at least one GGUF model under
  `C:\llama.cpp\models\<publisher>\<model>\` (override with `LLAMACPP_MODELS_DIR`).

## Quick start

```powershell
git clone https://github.com/<your-fork>/claude-account-manager.git
cd claude-account-manager
pip install -r requirements.txt
python tray_manager.py
```

A tray icon appears with the menu described in Part 4 below. To also autostart it at login, edit
`Claude_Account_Manager.vbs` if you cloned the repo somewhere other than
`C:/GitProjects/claude-account-manager`, then drop a shortcut to it in
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

The model list isn't hardcoded. The tray submenu scans the models folder (`C:\llama.cpp\models`) every
time it opens, so a model put there or deleted shows up or disappears without editing
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

### An uncensored model that is actually fast enough to agent with

The lane above was written when the models on hand were general chat models that happened to
expose tool calling. Two things had to be true for the local lane to be worth using for real
work, and neither was: the model has to hold a 65536-token context without spilling out of
16 GB of VRAM, and it has to be trained for tool calling rather than merely capable of it.

`gpt-oss-20b`, in the abliterated build from huihui-ai, satisfies both, and the numbers are
better than expected. It is a Mixture-of-Experts model, roughly 21B parameters in total but
only about 3.6B active per token, so generation speed tracks a much smaller dense model. The
MXFP4 quantization is the format the weights were released in rather than a lossy conversion,
which keeps the file at 11.3 GB without the quality cost a Q4 re-quantization of a 20B model
would carry. Abliteration removes the refusal direction from the residual stream, leaving tool
calling untouched.

Measured here on an RTX 4080 SUPER:

- 14.05 GB of 16.38 GB VRAM in use, every layer on the GPU, no CPU offload;
- 78 to 172 tokens/s generation, depending on how full the context is;
- 4600 to 6500 tokens/s prefill;
- 131072-token context, double what every other entry in `TUNED_FLAGS` gets.

That last figure is the one that changes the lane's character. The failure described above,
where an 87,243-token Claude Code system prompt was rejected outright, was a consequence of
the 65536-token ceiling, and the ceiling was a consequence of VRAM. gpt-oss has an unusually
small KV cache (24 layers, 8 KV heads of 64 dimensions each), so doubling the window cost
about 0.65 GB. An 88,274-token request now prefills in 19 seconds instead of failing. The
30-90 second first-message wait is gone for the same reason.

One configuration detail is worth recording because it is easy to get wrong. The Qwen3 entry
in `TUNED_FLAGS` passes `--reasoning-format none`, and copying that onto gpt-oss breaks it.
gpt-oss emits Harmony channels, and llama.cpp's default parsing already routes the analysis
channel into `reasoning_content` and function calls into `tool_calls`; forcing `none` dumps
the raw channel markup into `content`, and the tool call stops being extractable.

### What the uncensored lane actually needs: eyes, and a forgiving tool-call format

gpt-oss ran fast and passed a short tool-calling check, so it went in. Driving Hermes against it
for a real session exposed two failures that a short check cannot see, and both are worth
recording because neither is about the model being weak.

The first is that it has no eyes. Hermes drives a browser through `computer_use`, which works by
sending a screenshot; gpt-oss is text-only, so llama-server answered every one of those with
`image input is not supported - hint: if this is unexpected, you may need to provide the mmproj`
— 62 times in one session. The agent was not failing to click the button, it was never shown
the button.

The second is the Harmony format. gpt-oss does not emit plain tool calls; it emits channel-tagged
markup that llama.cpp parses into `tool_calls`. Under load the abliterated build stops emitting
that markup correctly — llama.cpp logged 44 `common_chat_peg_parse: unparsed peg-native output`
warnings in the same session, on output like
`<|channel|>functions.read_preview<|channel|>commentary json<|message|>` where the opening tag is
duplicated and the `<|start|>assistant` prefix is missing. Each one reached Hermes as an
`openai.APIError` after three retries. This is the cost of abliteration that the benchmarks do
not show: removing the refusal direction also perturbs the model's grip on its own special
tokens, and Harmony has no slack for that. Ordinary ChatML tool calls degrade far more
gracefully.

Qwen3-VL-8B, in the abliterated build, answers both problems: it is multimodal, and it speaks
ChatML. Re-running the same shape of workload — 15,257 prompt tokens, a screenshot in context,
twelve tool definitions — produced two well-formed tool calls, zero parse warnings and zero
image rejections. It uses 10.8 GB of VRAM with the projector loaded and generates at about 96
tokens/s.

gpt-oss stays installed, because for text-only work its 131072-token context and 78-172 tokens/s
are still the best thing here. It just is not the model to point at anything that takes
screenshots.

### Where an 8B model stops being enough

Qwen3-VL-8B fixed the two mechanical failures — it sees images and it emits parseable tool
calls — and that was enough to make the lane work at all. It was not enough to make it useful
for long tasks, and the way it failed is worth separating from the earlier failures because
nothing was malformed this time.

In one Hermes session it hit the harness's loop guard twice. First it repeated a `computer_use`
call that had already returned `Permission denied` until Hermes refused to run it again — "the
same tool call failed 5 times with identical arguments" — ending the turn at `guardrail_halt`
after 56 tool turns. It then immediately did the same thing with `skill_manage`, eight identical
retries against an error message that was never going to change, each one costing an identical
142 tokens of output. Second `guardrail_halt`.

That is a different class of problem from malformed Harmony markup or a missing projector. The
model read an error, understood it well enough to produce fluent text about it, and then did
not let it change what it did next. No sampler setting fixes that; adding a presence penalty
had already fixed the one loop that *was* a sampling artifact (a repeated line inside a tool
argument) and left this one untouched.

Qwen3-VL-30B-A3B, same abliterated line, replaces it for anything long-running. It is also a
Mixture-of-Experts model — about 30B parameters with roughly 3B active per token — so the cost
of the extra capability is mostly VRAM rather than speed. At Q4_K_M the weights are 17.3 GB
against 16 GB of VRAM, so `--n-cpu-moe` keeps the expert tensors of the first N layers in system
RAM while attention, the projector and the remaining experts stay on the GPU; with 8 of 128
experts firing per token, the CPU share of the work is much smaller than the offloaded parameter
share suggests.

The quantization choice is deliberate and goes the opposite way from what fitting in VRAM would
suggest. IQ4_XS (15.2 GB) and Q3_K_M (13.7 GB) would both need far less offload, but
quantization below Q4 degrades instruction-following before it degrades anything else, and
instruction-following is the entire reason for the upgrade. Paying for it in offload rather
than in quantization is the trade that matches the goal.

gpt-oss-20b was removed outright at this point. Its one remaining advantage was a 131072-token
context for text-only work, which does not offset being blind in a lane whose whole purpose is
driving a screen.

### The bug this uncovered: every vision model here was running blind

Chasing the `mmproj` error revealed that `local_llama.py` had never passed `--mmproj` at all. It
deliberately skipped `mmproj-*.gguf` files when scanning, correctly treating them as helpers
rather than chat models, and then never attached them to anything. GLM-4.6V-Flash and
gemma-4-12B both ship a projector in their folder, both were selectable from the tray, and both
had been starting without it.

That failure is quiet in a way worth noting. The language weights load, the server reports
healthy, text chat works normally, and only an actual image request fails — so a vision model
configured this way looks fine right up until something asks it to look at something.
`_find_mmproj()` now attaches the projector sitting next to a model (preferring F16 over BF16
over F32), `_build_command()` passes it, and the tray labels such models `[vision]` so the
distinction is visible before starting the server rather than after.

## Why write this down

None of the individual pieces here are secret, but stitching them into "two subscriptions plus a
free-tier lane, running at once, without crossing Anthropic's actual policy line" took a lot of
trial and error even with a lot of documentation in hand — three near-identical-looking failure
modes in Part 3 alone, each needing a different one-line fix. If it was this non-obvious to work
through once, it's probably worth writing up properly for anyone else trying the same thing.
