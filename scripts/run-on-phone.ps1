<#
.SYNOPSIS
    Gets HomeGuard running on a USB-connected Android phone in one command.

.DESCRIPTION
    Does every step end to end: checks the phone is visible, starts the Python
    backend, opens the USB reverse tunnel, installs the APK, launches the app,
    and then verifies the app actually reached the backend rather than just
    assuming it did.

    Ordering here is not cosmetic. `adb reverse` must exist BEFORE the app
    launches: the app opens its WebSocket in the HttpBackendService constructor,
    and if the tunnel isn't up yet that first attempt fails. There is a 3-second
    reconnect loop, but the mismatch is genuinely confusing while it lasts —
    arm/disarm succeeds over REST while the screen never updates. See
    CLAUDE.md and .claude/commands/flutter-run.md.

.PARAMETER Apk
    Path to a prebuilt APK. If omitted, the script builds one via WSL2 (native
    Windows Gradle builds are broken on this machine — see CLAUDE.md).

.PARAMETER ResetAppData
    Wipes the app's saved data before installing. Use this if the app was
    previously pointed at a Tailscale/LAN address: `adb install -r` preserves
    SharedPreferences, so a stale "Backend URL" from an earlier run survives
    the reinstall and the app would ignore the USB tunnel entirely.

.PARAMETER SkipBackend
    Don't start the backend (use when it's already running in another window).

.EXAMPLE
    .\scripts\run-on-phone.ps1 -Apk "$HOME\Downloads\homeguard-arm64.apk"

.EXAMPLE
    .\scripts\run-on-phone.ps1 -ResetAppData
#>

param(
    [string]$Apk,
    [switch]$ResetAppData,
    [switch]$SkipBackend
)

$ErrorActionPreference = "Stop"

$RepoRoot    = Split-Path -Parent $PSScriptRoot
$Adb         = "C:\dev\android-sdk\platform-tools\adb.exe"
$PackageName = "com.homeguard.homeguard_app"
$BuiltApk    = Join-Path $RepoRoot "app\build\app\outputs\flutter-apk\app-debug.apk"
$BackendPy   = Join-Path $RepoRoot "python\venv\Scripts\python.exe"
$Port        = 8000

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "   OK   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "   ..   $msg" -ForegroundColor Yellow }

# ---- 1. Phone -----------------------------------------------------------------
# The Windows-side adb is the one that sees the device. The adb inside WSL2 does
# not — that's why builds happen in WSL2 but every device command runs here.
Step "Checking the phone is connected"
if (-not (Test-Path $Adb)) {
    throw "adb not found at $Adb. Install Android platform-tools, or edit `$Adb in this script."
}
$devices = & $Adb devices | Select-Object -Skip 1 | Where-Object { $_ -match "\S" }
$ready   = $devices | Where-Object { $_ -match "\sdevice$" }
if (-not $ready) {
    if ($devices -match "unauthorized") {
        throw "Phone is connected but UNAUTHORIZED. Unlock it and tap 'Allow USB debugging' on the prompt, then re-run."
    }
    if ($devices -match "offline") {
        throw "Phone shows as OFFLINE. Unplug and replug the cable (or toggle USB debugging off/on), then re-run."
    }
    throw "No phone detected. Check the cable, and that Developer options -> USB debugging is on. (``adb devices`` showed: $($devices -join '; '))"
}

# Every adb call below is pinned to one serial with -s. Without it, adb refuses
# to run with "more than one device/emulator" the moment an emulator is also up,
# which is easy to hit and gives a confusing failure halfway through the run.
$serials  = $ready | ForEach-Object { ($_ -split "\s+")[0] }
$physical = $serials | Where-Object { $_ -notmatch "^emulator-" }
if ($physical.Count -gt 1) {
    throw "More than one physical device attached ($($physical -join ', ')). Unplug the others, or set `$Target by hand in this script."
}
$Target = if ($physical) { @($physical)[0] } else { @($serials)[0] }
if (-not $physical) { Warn "no physical phone found — falling back to the emulator $Target" }
Ok "targeting $Target"

# ---- 2. Backend ---------------------------------------------------------------
Step "Starting the backend"
if ($SkipBackend) {
    Warn "skipped (-SkipBackend)"
} elseif (-not (Test-Path $BackendPy)) {
    throw "No Python venv at $BackendPy. Create it first: cd python; python -m venv venv; .\venv\Scripts\pip.exe install -r requirements.txt"
} else {
    $alreadyUp = $false
    try {
        Invoke-RestMethod -Uri "http://localhost:$Port/health" -TimeoutSec 2 | Out-Null
        $alreadyUp = $true
    } catch { }

    if ($alreadyUp) {
        Warn "already running on port $Port — leaving it alone"
    } else {
        # 0.0.0.0 rather than localhost so the same running backend also serves
        # a Tailscale address later without needing a restart.
        Start-Process -FilePath $BackendPy `
            -ArgumentList "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "$Port" `
            -WorkingDirectory (Join-Path $RepoRoot "python")
        # 90s, not 30s: this backend imports OpenCV (opencv-contrib-python)
        # and numpy and loads the Haar cascade at startup, which on a cold
        # Windows run regularly takes over 30 seconds. A 30s cap made the
        # script throw "Backend never answered" while the backend was in
        # fact starting normally and came up moments later — a false
        # failure that aborted the run before it ever reached install.
        $waitSeconds = 90
        Write-Host "   waiting for it to come up (up to ${waitSeconds}s; OpenCV import makes the first start slow)..."
        $up = $false
        foreach ($i in 1..$waitSeconds) {
            Start-Sleep -Seconds 1
            try {
                Invoke-RestMethod -Uri "http://localhost:$Port/health" -TimeoutSec 2 | Out-Null
                $up = $true; break
            } catch { }
            if ($i % 15 -eq 0) { Write-Host "   still waiting... ${i}s" }
        }
        if (-not $up) { throw "Backend never answered on http://localhost:$Port/health after ${waitSeconds}s. Check the window it opened for the error." }
        Ok "backend healthy on port $Port"
    }
}

# ---- 3. USB tunnel ------------------------------------------------------------
# Must come before install/launch — see the note in the header.
Step "Opening the USB reverse tunnel (before launching the app)"
& $Adb -s $Target reverse --remove-all 2>$null | Out-Null
& $Adb -s $Target reverse "tcp:$Port" "tcp:$Port" | Out-Null
$tunnel = & $Adb -s $Target reverse --list
if ($tunnel -notmatch "tcp:$Port") { throw "adb reverse did not take. Got: $tunnel" }
Ok "phone's localhost:$Port now reaches this PC ($tunnel)"

# ---- 4. APK -------------------------------------------------------------------
Step "Getting an APK"
if ($Apk) {
    if (-not (Test-Path $Apk)) { throw "APK not found at $Apk" }
    $apkPath = (Resolve-Path $Apk).Path
    Ok "using $apkPath"
} else {
    Warn "no -Apk given, building via WSL2 (native Windows Gradle is broken here — see CLAUDE.md)"
    $wslProject = "/mnt/" + $RepoRoot.Substring(0,1).ToLower() + $RepoRoot.Substring(2).Replace("\","/")
    $buildCmd = @"
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
export ANDROID_HOME=/opt/android-sdk
export PATH="/opt/flutter/bin:/opt/android-sdk/platform-tools:/opt/android-sdk/cmdline-tools/latest/bin:`$PATH"
cd $wslProject/app
flutter build apk --debug
"@
    # Record the timestamp before building so a stale artifact can be
    # detected afterwards (see the freshness check below).
    $apkBefore = if (Test-Path $BuiltApk) { (Get-Item $BuiltApk).LastWriteTime } else { [datetime]::MinValue }

    wsl -d Ubuntu -- bash -c $buildCmd
    if ($LASTEXITCODE -ne 0) {
        throw "WSL2 build failed. If the errors look like unrelated Dart parse failures, it's usually a stale cache from mixing Windows/WSL2 builds — run 'flutter clean && flutter pub get' inside WSL2 and retry."
    }
    if (-not (Test-Path $BuiltApk)) { throw "Build reported success but no APK at $BuiltApk" }

    # Existence alone is NOT proof the build produced anything: Gradle can
    # report success while leaving a previous APK untouched (seen in
    # practice after switching git branches — the build "succeeded" and the
    # script happily installed a 3-hour-old APK that predated the branch,
    # so the phone ran code that lacked the newly added screen entirely and
    # the failure looked like a UI bug rather than a build one). Compare the
    # write time instead, and say so plainly rather than installing a stale
    # artifact.
    $apkAfter = (Get-Item $BuiltApk).LastWriteTime
    if ($apkAfter -le $apkBefore) {
        throw @"
Build reported success but did not rewrite the APK (still dated $apkAfter).
That artifact predates this build, so installing it would put stale code on
the phone. This is usually the Windows/WSL2 stale-cache trap in CLAUDE.md.
Fix: wsl -d Ubuntu -- bash -c 'cd $wslProject/app && flutter clean && flutter pub get && flutter build apk --debug'
"@
    }
    $apkPath = $BuiltApk
    Ok "built $apkPath (fresh, $apkAfter)"
}

# ---- 5. Install ---------------------------------------------------------------
Step "Installing on the phone"
if ($ResetAppData) {
    # SharedPreferences survives `install -r`, so a Backend URL saved in an
    # earlier session would override localhost and make the tunnel useless.
    & $Adb -s $Target shell pm clear $PackageName 2>$null | Out-Null
    Ok "cleared saved app data (backend URL resets to http://localhost:$Port)"
}
& $Adb -s $Target install -r $apkPath
if ($LASTEXITCODE -ne 0) { throw "adb install failed. If it mentions signatures, uninstall first: adb uninstall $PackageName" }
Ok "installed"

# ---- 6. Launch ----------------------------------------------------------------
Step "Launching"
& $Adb -s $Target shell monkey -p $PackageName -c android.intent.category.LAUNCHER 1 | Out-Null
Start-Sleep -Seconds 4
Ok "launched"

# ---- 7. Verify ----------------------------------------------------------------
# Don't just claim success — confirm the phone actually reached the backend.
Step "Verifying the phone can reach the backend"
$fromPhone = & $Adb -s $Target shell "curl -s -m 5 http://localhost:$Port/health" 2>$null
if ($fromPhone -match '"ok"\s*:\s*true') {
    Ok "phone reached the backend through the tunnel: $fromPhone"
} else {
    Warn "couldn't confirm from the phone (many Android builds ship no curl — this is not necessarily a failure)."
    Warn "Check the app instead: the dashboard header should read ONLINE."
}

Write-Host "`nDone." -ForegroundColor Green
Write-Host "Log in with the default password: homeguard"
Write-Host "If the header shows OFFLINE, the app is probably still pointed at an old"
Write-Host "Backend URL from a previous run — re-run with -ResetAppData."
