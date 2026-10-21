# Starts the HomeGuard backend (and, if built, the web app on the same port).
#
#   .\scripts\start-backend.ps1                 # default board addresses
#   .\scripts\start-backend.ps1 -DoorIp 192.168.68.130
#   .\scripts\start-backend.ps1 -LogAllRequests # also show heartbeats and camera frames
#
# Or double-click scripts\start-backend.cmd, which opens this in its own window.
#
# The window is the backend: closing it (or Ctrl+C) stops it. It shows what is
# happening as it happens (armed, door opened, alarm, email sent). The boards'
# heartbeats and the camera's frame pushes are left out so the lines that matter
# stay visible; -LogAllRequests puts them back, for diagnosing.
#
# The boards get their addresses from the router's DHCP, so they can change;
# if a board shows offline, check its IP and pass it here. Restarting the
# backend logs the app out and resets the alarm state, by design.
param(
    [string]$DoorIp   = '192.168.68.123',
    [string]$CameraIp = '192.168.68.125',
    [int]$Port = 8000,
    [switch]$LogAllRequests
)

$python = Join-Path $PSScriptRoot '..\python'
$env:HOMEGUARD_DOOR_CONTROLLER_URL = "http://$DoorIp"
$env:HOMEGUARD_PORT = "$Port"   # the UDP announcement tells the boards this port
$env:HOMEGUARD_CAMERA_STREAM_URL   = "http://$CameraIp/stream"
# The boards that send heartbeats (the camera's frames are its heartbeat).
$env:HOMEGUARD_BOARDS = 'ultrasonic,door_controller'
# The door controller's secrets.h: on a first run the app password starts as
# its KEYPAD_PIN, and a later password change keeps that line in step, so a
# reflash never hands the board an old PIN.
$secrets = Join-Path $PSScriptRoot '..\esp32\door_keypad_controller\secrets.h'
if (Test-Path $secrets) { $env:HOMEGUARD_SECRETS_H = (Resolve-Path $secrets).Path }
if ($LogAllRequests) { $env:HOMEGUARD_LOG_ALL_REQUESTS = '1' }

$Host.UI.RawUI.WindowTitle = 'HomeGuard backend'
$lan = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } |
        Select-Object -First 1).IPv4Address.IPAddress
Write-Host ''
Write-Host '  HomeGuard backend' -ForegroundColor Cyan
Write-Host "  App:  http://${lan}:$Port/   (this laptop: http://localhost:$Port/)"
Write-Host '  Stop: close this window, or press Ctrl+C'
Write-Host ''

Set-Location $python
if (-not (Test-Path '..\app\build\web\index.html')) {
    Write-Host 'No web build found - run scripts\build-web.ps1 to serve the app from here.'
}
& .\venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port $Port
