#!/usr/bin/env bash
# Copies the shared iOS sources into the SwiftPM layout xtool expects (Shared/ goes into both targets).
# Default is app only (no App Group entitlement, so a free Apple ID can sign it). `--with-share` adds the
# share extension + App Group, which needs a paid account and an xtool with the #379 signing fix.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf Sources
mkdir -p Sources/Magpie
cp -r ../Magpie/. Sources/Magpie/
rm -rf Sources/Magpie/Assets.xcassets Sources/Magpie/*.entitlements
cp -r ../Shared/. Sources/Magpie/
if [ "${1:-}" = "--with-share" ]; then
    mkdir -p Sources/MagpieShare
    cp -r ../MagpieShare/. Sources/MagpieShare/
    rm -f Sources/MagpieShare/*.entitlements
    cp -r ../Shared/. Sources/MagpieShare/
    cp Package.share.swift Package.swift
    cp xtool.share.yml xtool.yml
fi
