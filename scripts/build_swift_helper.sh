#!/bin/bash
# Build ScreenCaptureHelper as a universal binary (arm64 + x86_64)
set -euo pipefail

SWIFT_SRC="helios/swift/ScreenCaptureHelper.swift"
OUTPUT="helios/bin/ScreenCaptureHelper"
ARM64_TMP="/tmp/ScreenCaptureHelper_arm64"
X86_TMP="/tmp/ScreenCaptureHelper_x86_64"

mkdir -p helios/bin

echo "Compiling arm64..."
swiftc -O \
  -target arm64-apple-macos13 \
  "$SWIFT_SRC" \
  -o "$ARM64_TMP"

echo "Compiling x86_64..."
swiftc -O \
  -target x86_64-apple-macos13 \
  "$SWIFT_SRC" \
  -o "$X86_TMP"

echo "Creating universal binary..."
lipo -create "$ARM64_TMP" "$X86_TMP" -output "$OUTPUT"
rm -f "$ARM64_TMP" "$X86_TMP"

codesign --force --sign - "$OUTPUT"
echo "Built universal binary for ScreenCaptureHelper"
