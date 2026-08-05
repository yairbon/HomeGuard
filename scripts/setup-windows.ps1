<#
HomeGuard dev environment setup (Windows).
Reproduces the Flutter + Android toolchain from scratch: JDK 17, Android SDK
command-line tools, Flutter SDK. Run from an elevated or normal PowerShell
window, then close and reopen your terminal (or a new machine reboot) so the
PATH changes take effect.

Everything is installed under C:\dev on purpose — Android/Gradle builds are
known to break on Windows when the path contains non-ASCII characters (e.g.
a Hebrew/Cyrillic/accented username under C:\Users\<name>). Keep the SDKs
and this project on an ASCII-only path.
#>

$ErrorActionPreference = "Stop"

$DevRoot     = "C:\dev"
$FlutterDir  = "$DevRoot\flutter"
$SdkRoot     = "$DevRoot\android-sdk"
$CmdlineZip  = "$SdkRoot\cmdline-tools\tools.zip"

# Android SDK command-line tools — version/checksum from
# https://developer.android.com/studio#command-tools
# If this fails, that page's numbers have moved on; grab the current ones.
$CmdlineToolsUrl    = "https://dl.google.com/android/repository/commandlinetools-win-15859902_latest.zip"
$CmdlineToolsSha256 = "90ae805d20434428bffcb699c290860f19bb5f66a67e6b330067e3de801fb04a"

New-Item -ItemType Directory -Force -Path $DevRoot | Out-Null

# 1. JDK 17 (Android Gradle builds require 17+; plain "java" on PATH is often too old)
Write-Host "== Installing JDK 17 ==" -ForegroundColor Cyan
winget install --id ojdkbuild.openjdk.17.jdk -e --accept-package-agreements --accept-source-agreements --silent
$JavaHome = (Get-ChildItem "C:\Program Files\ojdkbuild" -Directory | Select-Object -First 1).FullName
[Environment]::SetEnvironmentVariable("JAVA_HOME", $JavaHome, "User")

# 2. Android Studio (IDE + emulator support; not strictly required to build/run on a
#    physical device, but useful to have)
Write-Host "== Installing Android Studio ==" -ForegroundColor Cyan
winget install --id Google.AndroidStudio -e --accept-package-agreements --accept-source-agreements --silent

# 3. Flutter SDK (official stable channel)
Write-Host "== Cloning Flutter SDK ==" -ForegroundColor Cyan
if (-not (Test-Path $FlutterDir)) {
    git clone -b stable https://github.com/flutter/flutter.git $FlutterDir
} else {
    Write-Host "Flutter already present at $FlutterDir, skipping clone."
}

# 4. Android SDK command-line tools (headless — avoids the Android Studio GUI wizard)
Write-Host "== Downloading Android SDK command-line tools ==" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path "$SdkRoot\cmdline-tools" | Out-Null
Invoke-WebRequest -Uri $CmdlineToolsUrl -OutFile $CmdlineZip

$actualHash = (Get-FileHash -Path $CmdlineZip -Algorithm SHA256).Hash.ToLower()
if ($actualHash -ne $CmdlineToolsSha256) {
    throw "Checksum mismatch for Android cmdline-tools download. Expected $CmdlineToolsSha256, got $actualHash. Re-check https://developer.android.com/studio#command-tools for the current version."
}

if (-not (Test-Path "$SdkRoot\cmdline-tools\latest")) {
    Expand-Archive -Path $CmdlineZip -DestinationPath "$SdkRoot\cmdline-tools" -Force
    Rename-Item "$SdkRoot\cmdline-tools\cmdline-tools" "latest"
}
Remove-Item $CmdlineZip -Force

# 5. Accept licenses + install SDK packages (headless)
Write-Host "== Accepting Android SDK licenses and installing packages ==" -ForegroundColor Cyan
$env:JAVA_HOME = $JavaHome
$sdkmanager = "$SdkRoot\cmdline-tools\latest\bin\sdkmanager.bat"

$licenseInput = ("y`n" * 20)
$licenseInput | & $sdkmanager --sdk_root=$SdkRoot --licenses | Out-Null

& $sdkmanager --sdk_root=$SdkRoot `
    "platform-tools" `
    "platforms;android-34" "build-tools;34.0.0" `
    "platforms;android-36" "build-tools;28.0.3"

# 6. Environment variables + PATH (User scope — no admin required)
Write-Host "== Configuring environment variables ==" -ForegroundColor Cyan
[Environment]::SetEnvironmentVariable("ANDROID_HOME", $SdkRoot, "User")
[Environment]::SetEnvironmentVariable("ANDROID_SDK_ROOT", $SdkRoot, "User")

$userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
$additions = @(
    "$FlutterDir\bin",
    "$SdkRoot\platform-tools",
    "$SdkRoot\cmdline-tools\latest\bin"
)
foreach ($p in $additions) {
    if ($userPath -notlike "*$p*") { $userPath = "$userPath;$p" }
}
[Environment]::SetEnvironmentVariable("PATH", $userPath, "User")

# 7. Verify
Write-Host "== Running flutter doctor ==" -ForegroundColor Cyan
$env:PATH += ";" + ($additions -join ";")
& "$FlutterDir\bin\flutter.bat" config --android-sdk $SdkRoot
& "$FlutterDir\bin\flutter.bat" doctor

Write-Host ""
Write-Host "Setup complete. Close and reopen your terminal so the new PATH takes effect everywhere." -ForegroundColor Green
