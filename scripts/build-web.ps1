# Builds the Flutter web app into app\build\web, which the backend serves from
# "/" (see mount_web_app in python/backend/main.py). Run it again after any
# change to the app; restart the backend only if the folder did not exist yet.
#
#  - HOMEGUARD_SAME_ORIGIN: the page talks to the address it was loaded from,
#    so one build works from the laptop, a phone on the WiFi or Tailscale.
#  - --no-web-resources-cdn: the renderer (CanvasKit) is bundled instead of
#    fetched from Google at runtime, so the page works with no internet.
#  - --pwa-strategy=none: no service worker, so a rebuilt app is never hidden
#    behind a stale cached copy on a phone.
param([string]$Flutter = 'C:\dev\flutter\bin\flutter.bat')

$ErrorActionPreference = 'Stop'
Push-Location (Join-Path $PSScriptRoot '..\app')
try {
    & $Flutter pub get
    if ($LASTEXITCODE -ne 0) { throw "flutter pub get failed" }
    # Flutter prints a deprecation warning on stderr; under 'Stop' PowerShell would
    # treat that as a failure, so only the exit code decides here.
    $ErrorActionPreference = 'Continue'
    & $Flutter build web --release --no-web-resources-cdn --pwa-strategy=none --dart-define=HOMEGUARD_SAME_ORIGIN=true
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($code -ne 0) { throw "flutter build web failed" }
    Write-Host "Built app\build\web"
} finally {
    Pop-Location
}
