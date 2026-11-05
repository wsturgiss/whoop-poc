#!/usr/bin/env bash
# Build whoop-stage (NOOP's SleepStager on Linux). Needs: Swift 6.x (swiftly) + ~/src/noop with Packages/.
#   git -C ~/src/noop sparse-checkout add Packages
# On Arch/Omarchy the Ubuntu toolchain also needs libncurses.so.6 / libxml2.so.2 compat links; see README.
set -euo pipefail
cd "$(dirname "$0")"
[ -f "$HOME/.local/share/swiftly/linux-env.sh" ] && . "$HOME/.local/share/swiftly/linux-env.sh"
./link-noop.sh
swift build -c release --product whoop-stage 2>&1 | grep -v "no version information available"
[ "${1:-}" = "--test" ] && swift test 2>&1 | grep -v "no version information available" | grep -E "Executed|error|fail" | tail -3
echo "built $(pwd)/.build/release/whoop-stage"
