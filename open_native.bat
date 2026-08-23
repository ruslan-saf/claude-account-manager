@echo off
REM Clear any inherited ANTHROPIC_* vars from the parent process, then
REM toggle.py off removes them from settings.json too -- belt and suspenders.
set ANTHROPIC_BASE_URL=
set ANTHROPIC_AUTH_TOKEN=
set ANTHROPIC_MODEL=
set ANTHROPIC_CUSTOM_MODEL_OPTION=
set ANTHROPIC_CUSTOM_MODEL_OPTION_NAME=
set ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION=
python "%~dp0toggle_gateway.py" off
claude
