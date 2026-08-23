@echo off
echo ============================================================
echo  EXPERIMENTAL: Claude Code CLI on a local model
echo ============================================================
echo  Claude Code's own system prompt (tools + all installed skills
echo  + MCP servers) has measured 37K-87K tokens on this machine --
echo  much bigger than what a local model can reliably process fast,
echo  and it can exceed the model's context window outright (you'll
echo  see "request (N tokens) exceeds the available context size").
echo  The first message may take 30-90s+ or fail. This is a known
echo  limitation, not a bug -- see Part 5 of the claude-multi-account-
echo  gateway skill / this repo's README for details.
echo ============================================================
echo.
REM Clear any inherited ANTHROPIC_* vars from the parent process so only
REM what settings.json's env block specifies (written by toggle.py) applies.
set ANTHROPIC_BASE_URL=
set ANTHROPIC_AUTH_TOKEN=
set ANTHROPIC_MODEL=
set ANTHROPIC_CUSTOM_MODEL_OPTION=
set ANTHROPIC_CUSTOM_MODEL_OPTION_NAME=
set ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION=

REM Make sure the selected local model is loaded and healthy in llama-server
REM before pointing the CLI at it (through OmniRoute -- Claude Code speaks
REM the Anthropic Messages API, llama-server only speaks OpenAI-compatible
REM chat completions, OmniRoute translates between the two).
python "%~dp0local_llama.py" start-server-bg
if errorlevel 1 (
    echo.
    echo llama-server failed to start -- see above. Not launching Claude Code.
    pause
    exit /b 1
)

python "%~dp0toggle_gateway.py" on http://localhost:20128 "" local-model
claude
