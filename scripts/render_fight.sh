#!/usr/bin/env bash
# Render a `duel fight` page to video: headless Chrome draws each frame, ffmpeg encodes it.
# usage: scripts/render_fight.sh fight.html out.mp4 [out.gif]
#   MP4: 1280x720, 30fps, H.264 + the page's seeded sound. GIF: 640px wide, 15fps (64-colour palette, no dither: ~5MB for 45s).
# Needs google-chrome (or $CHROME), node >= 20 and ffmpeg. No network, no API calls.
set -euo pipefail
[[ $# -ge 2 ]] || { echo "usage: $0 fight.html out.mp4 [out.gif]" >&2; exit 2; }
html=$1 mp4=$2 gif=${3:-}
frames=$(mktemp -d)
trap 'rm -rf "$frames"' EXIT
node --experimental-websocket --no-warnings "$(dirname "$0")/render_fight.mjs" "$html" "$frames"
ffmpeg -loglevel error -y -framerate 30 -i "$frames/f%05d.png" -i "$frames/audio.wav" \
  -c:v libx264 -pix_fmt yuv420p -crf 18 -c:a aac -b:a 128k -shortest -movflags +faststart "$mp4"
if [[ -n $gif ]]; then
  ffmpeg -loglevel error -y -framerate 30 -i "$frames/f%05d.png" \
    -vf "fps=15,scale=640:-1:flags=bilinear,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" "$gif"
fi
echo "rendered: $mp4${gif:+ $gif}"
