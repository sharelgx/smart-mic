#!/usr/bin/env bash
# Build the Smart Mic board with the verified Youteli 500 mAh cell profile.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
build_dir="$root/work/build-youteli-passport"
idf.py -C "$root/firmware/ai-passport" -B "$build_dir" \
    -D "SDKCONFIG=$build_dir/sdkconfig" -D SMART_MIC_YOUTELI_BATTERY=ON build
