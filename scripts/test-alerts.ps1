# Sends a real test alert over every channel set up in python\alerts.local.json
# and prints what happened. Same code path as Settings -> SEND TEST ALERT, but
# needs no login. Prints reasons only: never a password, key or number.
#
#   .\scripts\test-alerts.ps1

Set-Location (Join-Path $PSScriptRoot '..\python')
& .\venv\Scripts\python.exe -m backend.alerts_check
