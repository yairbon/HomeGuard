# Fills in the email part of python\alerts.local.json by asking you questions, so
# nobody has to edit JSON by hand. The app password is typed hidden, goes only
# into that local file (never committed), and is never shown or logged.
#
#   .\scripts\set-alert-email.ps1
#
# Needs a Gmail account that SENDS the alerts, with 2-Step Verification on and
# an "app password" for it: https://myaccount.google.com/apppasswords
# Afterwards run .\scripts\test-alerts.ps1 to send a real test.

$ErrorActionPreference = 'Stop'
$path = Join-Path $PSScriptRoot '..\python\alerts.local.json'

function Split-Addresses([string]$text) {
    return @($text -split '[,;\s]+' | Where-Object { $_ -match '^[^@\s]+@[^@\s]+\.[^@\s]+$' })
}

$sender = (Read-Host 'Gmail address that SENDS the alerts').Trim()
$secure = Read-Host 'Its 16-letter app password (typing is hidden)' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $password = ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)) -replace '\s', '' }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
# These two lists are only the STARTING values: on the first start the backend
# copies them into its settings, and from then on they are edited in the app
# (Settings -> Alerts).
$alertTo = Split-Addresses (Read-Host 'Email address(es) that get EVERY alarm (separate with commas)')
$escalationTo = Split-Addresses (Read-Host 'Emergency contact email(s) added if the alarm is NOT cleared (optional, Enter to skip)')

if (-not $sender -or -not $password -or $alertTo.Count -eq 0) {
    throw 'Need the sending address, its app password and at least one address that gets alarms. Nothing was saved.'
}

$config = [ordered]@{
    whatsapp = @()
    email    = [ordered]@{
        smtp_host     = 'smtp.gmail.com'
        smtp_port     = 587
        username      = $sender
        app_password  = $password
        alert_to      = $alertTo
        escalation_to = $escalationTo
    }
}
$json = $config | ConvertTo-Json -Depth 5
# UTF-8 without a byte-order mark, so every reader parses it.
[IO.File]::WriteAllText((Resolve-Path (Split-Path $path)).Path + '\alerts.local.json', $json, (New-Object Text.UTF8Encoding $false))
Write-Host "Saved. Alarms will be emailed to $($alertTo.Count) address(es); escalation adds $($escalationTo.Count) contact(s)."
Write-Host 'Now run .\scripts\test-alerts.ps1 to send a real test.'
