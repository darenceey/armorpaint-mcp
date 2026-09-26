#!/usr/bin/env bash
# Build ArmorPaint 1.0 from the pinned commit and run it headless for tests/test_live*.py.
#
#   tools/live_harness.sh build <dir> [--ext]   clone + (optionally) apply the native extension + build
#   tools/live_harness.sh run   <dir>           start Xvfb :99 + openbox + ArmorPaint (lavapipe)
#   tools/live_harness.sh stop
#
# Then:  DISPLAY=:99 ARMORPAINT_LIVE=1 python -m pytest tests/test_live*.py
#
# Needs (Debian/Ubuntu): clang, git, Xvfb, openbox, mesa-vulkan-drivers, libvulkan-dev,
# libx11-dev libxrandr-dev libxi-dev libxinerama-dev libxcursor-dev libxkbcommon-dev
# libwayland-dev libasound2-dev libudev-dev libgl-dev libgtk-3-dev glslang-tools.
# A window manager is required: the server locates ArmorPaint's window through
# _NET_CLIENT_LIST, which bare Xvfb does not publish.
set -euo pipefail

PINNED=906418acc600132fa927876d208eb452dc5a0967
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DISPLAY_NUM=:99

cmd=${1:-}
dir=${2:-}

case "$cmd" in
build)
	[ -n "$dir" ] || { echo "usage: $0 build <dir> [--ext]" >&2; exit 2; }
	[ -d "$dir/.git" ] || git clone https://github.com/armory3d/armorpaint "$dir"
	git -C "$dir" checkout -q "$PINNED"
	if [ "${3:-}" = "--ext" ]; then
		python3 "$REPO/patch/apply_ext_patch.py" "$dir"
	fi
	(cd "$dir/paint" && ../base/make --compile)
	out="$dir/paint/build/out"
	ln -sf "$REPO/plugin/armorpaint_mcp_bridge.c" "$out/data/plugins/armorpaint_mcp_bridge.c"
	# config.json is written on first launch; enable the bridge in it.
	if [ ! -f "$out/data/config.json" ]; then
		"$0" run "$dir" &
		sleep 10
		pkill -x ArmorPaint || true
		sleep 1
	fi
	python3 - "$out/data/config.json" <<'EOF'
import json, sys
p = sys.argv[1]
c = json.load(open(p))
c["plugins"] = sorted(set(c.get("plugins") or []) | {"armorpaint_mcp_bridge.c"})
open(p, "w").write(json.dumps(c, separators=(",", ":")))
EOF
	echo "built: $out/ArmorPaint (bridge enabled)"
	;;
run)
	[ -n "$dir" ] || { echo "usage: $0 run <dir>" >&2; exit 2; }
	pgrep -x Xvfb >/dev/null || (Xvfb "$DISPLAY_NUM" -screen 0 1920x1080x24 >/dev/null 2>&1 &)
	sleep 1
	pgrep -x openbox >/dev/null || (DISPLAY="$DISPLAY_NUM" openbox >/dev/null 2>&1 &)
	sleep 1
	export DISPLAY="$DISPLAY_NUM"
	VK_ICD_FILENAMES="$(ls /usr/share/vulkan/icd.d/lvp_icd*.json | head -1)"
	export VK_ICD_FILENAMES
	cd "$dir/paint/build/out"
	exec ./ArmorPaint
	;;
stop)
	pkill -x ArmorPaint || true
	;;
*)
	sed -n '2,16p' "$0"
	exit 2
	;;
esac
