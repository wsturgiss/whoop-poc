#!/usr/bin/env bash
# Symlink NOOP's StrandAnalytics sources (minus the WhoopStore/Apple-only engines) into this package.
# NOOP code is NOT vendored into this repo; set NOOP_DIR if your checkout lives elsewhere.
set -euo pipefail
NOOP_DIR=${NOOP_DIR:-$HOME/src/noop}
here=$(cd "$(dirname "$0")" && pwd)
src=$NOOP_DIR/Packages/StrandAnalytics
[ -d "$src/Sources/StrandAnalytics" ] || { echo "missing $src (git -C $NOOP_DIR sparse-checkout add Packages)"; exit 1; }
# These two import WhoopStore, whose RawOutbox needs Apple's Compression framework (no Linux port).
# The rest depend on those two engines (found by building on Linux). None are part of the sleep stager.
EXCLUDE="AnalyticsEngine.swift ReadinessEngine.swift RestSubScoreTrace.swift SleepDebt.swift CaptureAccumulator.swift RecoveryForecast.swift StepsEstimateEngine+Trace.swift ${EXTRA_EXCLUDE:-}"
rm -rf "$here/Sources/StrandAnalytics" "$here/Tests"
mkdir -p "$here/Sources/StrandAnalytics" "$here/Tests/StrandAnalyticsTests"
for f in "$src"/Sources/StrandAnalytics/*.swift; do
  case " $EXCLUDE " in *" $(basename "$f") "*) continue;; esac
  ln -s "$f" "$here/Sources/StrandAnalytics/"
done
for f in "$src"/Tests/StrandAnalyticsTests/SleepStager*Tests.swift; do ln -s "$f" "$here/Tests/StrandAnalyticsTests/"; done
ln -sfn "$NOOP_DIR/Packages/WhoopProtocol" "$here/WhoopProtocol"
echo "linked $(ls "$here/Sources/StrandAnalytics" | wc -l) StrandAnalytics sources from $src"
