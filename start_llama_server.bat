@echo off
REM Runs llama-server for the currently selected local model in this
REM window. Leave it open -- any OpenAI-compatible harness (Hermes Agent,
REM etc.) can connect to http://127.0.0.1:8080. Closing this window stops
REM the server.
python "%~dp0local_llama.py" start-server-fg
pause
