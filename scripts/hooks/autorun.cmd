@echo off
REM Machine-local hooks live in %USERPROFILE%\.workspaces
REM Run: python "%~dp0..\install.py" --path <workspaces-root>
if exist "%USERPROFILE%\.workspaces\autorun.cmd" call "%USERPROFILE%\.workspaces\autorun.cmd"
