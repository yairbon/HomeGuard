#!/bin/bash
# HomeGuard Android/Gradle build environment — inside WSL2 Ubuntu.
#
# Why this exists: this specific dev machine's Windows install has a broken
# Winsock catalog registration (AF_UNIX registered for SOCK_DGRAM only, no
# SOCK_STREAM entry), which breaks java.nio.channels.Selector.open() in
# every JDK 17 build — and therefore every native-Windows Gradle build —
# with "java.io.IOException: Unable to establish loopback connection".
# Confirmed NOT fixable via netsh winsock reset / sfc /scannow /
# DISM RestoreHealth + reboot. Building inside WSL2 (real Linux, AF_UNIX
# works natively) sidesteps it entirely. On a healthy Windows machine, the
# native scripts/setup-windows.ps1 path should just work — try that first.
#
# Run from Windows: wsl -d Ubuntu -- bash /mnt/c/dev/HomeGuard/scripts/setup-wsl.sh

set -e

# Pinned to the same version CI uses (.github/workflows/ci.yml). Cloning
# `-b stable` instead gives a fresh machine whatever stable happens to be that
# day, and `flutter analyze --fatal-infos --fatal-warnings` can then disagree
# with CI in either direction — a failure that won't reproduce locally, or a
# local failure CI doesn't see. Bump both together.
FLUTTER_VERSION="3.44.9"

CMDLINE_TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip"
CMDLINE_TOOLS_SHA256="4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583"

echo "== Installing JDK 17 =="
sudo apt-get update -qq
sudo apt-get install -y -qq openjdk-17-jdk-headless unzip curl git

echo "== Cloning Flutter SDK =="
if [ ! -d /opt/flutter ]; then
    sudo git clone -b "$FLUTTER_VERSION" --depth 1 https://github.com/flutter/flutter.git /opt/flutter
    sudo chown -R "$(whoami)" /opt/flutter
fi

echo "== Downloading Android SDK command-line tools =="
sudo mkdir -p /opt/android-sdk/cmdline-tools
sudo chown -R "$(whoami)" /opt/android-sdk
# Guarded so the script can be re-run, which matters because everything above
# this point is already idempotent and a partial failure (a dropped download,
# an apt hiccup) is exactly when you want to run it again. Unguarded, the
# second run hit `unzip` against an existing tree, which prompts, reads EOF
# under `wsl -- bash script`, and aborts on `set -e`; and if it got past that,
# `mv cmdline-tools latest` would have nested the new SDK *inside* the
# existing latest/. The Windows counterpart (setup-windows.ps1) already
# guards the same step.
if [ ! -d /opt/android-sdk/cmdline-tools/latest ]; then
    curl -fsSL -o /opt/android-sdk/cmdline-tools/tools.zip "$CMDLINE_TOOLS_URL"
    actual=$(sha256sum /opt/android-sdk/cmdline-tools/tools.zip | cut -d' ' -f1)
    if [ "$actual" != "$CMDLINE_TOOLS_SHA256" ]; then
        echo "CHECKSUM MISMATCH: expected $CMDLINE_TOOLS_SHA256, got $actual"
        echo "Re-check https://developer.android.com/studio#command-tools for the current version."
        rm -f /opt/android-sdk/cmdline-tools/tools.zip
        exit 1
    fi
    cd /opt/android-sdk/cmdline-tools
    unzip -q tools.zip
    mv cmdline-tools latest
    rm tools.zip
else
    echo "   (already installed, skipping)"
fi

echo "== Accepting licenses and installing SDK packages =="
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
yes | /opt/android-sdk/cmdline-tools/latest/bin/sdkmanager --sdk_root=/opt/android-sdk --licenses > /dev/null
/opt/android-sdk/cmdline-tools/latest/bin/sdkmanager --sdk_root=/opt/android-sdk \
  "platform-tools" "platforms;android-34" "build-tools;34.0.0" \
  "platforms;android-36" "build-tools;28.0.3"

echo "== Verifying =="
export ANDROID_HOME=/opt/android-sdk
export PATH="/opt/flutter/bin:/opt/android-sdk/platform-tools:/opt/android-sdk/cmdline-tools/latest/bin:$PATH"
flutter config --android-sdk /opt/android-sdk
flutter doctor

echo ""
echo "Setup complete. Build with:"
echo "  wsl -d Ubuntu -- bash -c 'export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 ANDROID_HOME=/opt/android-sdk; export PATH=\"/opt/flutter/bin:/opt/android-sdk/platform-tools:\$PATH\"; cd /mnt/c/dev/HomeGuard/app && flutter build apk --debug'"
