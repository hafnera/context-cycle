#!/bin/sh
# Builds "Context Cycle.app" from ContextCycleApp.swift (no Xcode project needed).
#   ./build.sh            -> app/build/Context Cycle.app
#   ./build.sh --install  -> additionally copies it to ~/Applications
set -e
cd "$(dirname "$0")"
APP="build/Context Cycle.app"
rm -rf build
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/scripts"
ARCH=$(uname -m)
swiftc -O -parse-as-library -target "$ARCH-apple-macos13.0" -o "$APP/Contents/MacOS/ContextCycle" ContextCycleApp.swift
cp Info.plist "$APP/Contents/Info.plist"
cp ../skills/cycle-context/scripts/*.py "$APP/Contents/Resources/scripts/"   # fallback copy of the extractor
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
echo "built: $PWD/$APP"
if [ "$1" = "--install" ]; then
  mkdir -p "$HOME/Applications"; rm -rf "$HOME/Applications/Context Cycle.app"
  cp -R "$APP" "$HOME/Applications/"; echo "installed: $HOME/Applications/Context Cycle.app"
fi
