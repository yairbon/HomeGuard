@echo off
rem Double-click to start the HomeGuard backend in its own visible window.
rem Extra arguments are passed through, for example:  start-backend.cmd -LogAllRequests
powershell -NoProfile -ExecutionPolicy Bypass -NoExit -File "%~dp0start-backend.ps1" %*
