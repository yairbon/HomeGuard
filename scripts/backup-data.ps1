# Copies everything that exists only on this laptop into a dated folder OUTSIDE the
# repository: the approved faces' photos, the state file (settings, password
# hash, device key, event log) and the local alerts file (email account and app
# password). None of it is in git, so none of it survives a deleted folder or a
# new laptop. Run it after enrolling faces or changing settings, and before the
# showcase.
#
#   .\scripts\backup-data.ps1
#   .\scripts\backup-data.ps1 -To D:\homeguard-backups      # e.g. a USB stick
#
# The backup holds secrets (device key, email app password): keep it somewhere
# only you can read, never in the repository or a shared folder.
param([string]$To = 'C:\dev\homeguard-backups')

$python = Join-Path $PSScriptRoot '..\python'
$stamp = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'
$dest = Join-Path $To $stamp
New-Item -ItemType Directory -Force $dest | Out-Null

foreach ($item in 'faces', 'state.json', 'alerts.local.json') {
    $source = Join-Path $python $item
    if (Test-Path $source) { Copy-Item $source $dest -Recurse -Force }
}
$photos = @(Get-ChildItem (Join-Path $dest 'faces') -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -ne '.gitkeep' }).Count
Write-Host "Backed up to $dest ($photos face photo(s))."
if ($photos -eq 0) { Write-Host 'WARNING: there are no face photos, so face disarm cannot recognise anyone.' -ForegroundColor Yellow }
