#!/usr/bin/env python3
"""Turn the chest .webm into an animated WebP that Qt can actually play.

    uv run python scripts/make_chest_animation.py      (needs ffmpeg on PATH)

Committed as a script rather than only as the binary, for the same reason
make_icon.py is: so the asset can be regenerated from source, and so a change
to it is a readable diff rather than a blob nobody can reproduce.

Why not just ship the .webm
---------------------------
QMovie.supportedFormats() is ["gif", "webp"]. Not webm. QtMultimedia can be
made to play video, but on Windows that is Media Foundation, which needs the
Store's VP9 extension and will not composite an alpha channel over a widget.
Pulling QtMultimedia into the build for this would also add megabytes to a
45 MB artifact.

Animated WebP is played by QMovie directly, carries a real alpha channel, and
needs no new dependency at all.

Why the decoder is named explicitly
----------------------------------
The source .webm *does* carry real per-frame alpha: EBML element AlphaMode
(0x53C0) is set to 1, and the alpha plane rides in BlockAdditional. It is easy
to conclude otherwise, because almost nothing surfaces it. ffprobe reports
pix_fmt=yuv420p, which describes the base stream only, and VLC, QuickTime,
Premiere and Windows Photos all ignore VP9 alpha and render the clip on black.

ffmpeg's native vp9 decoder does the same. `-c:v libvpx-vp9` is what reads the
alpha, and it has to appear before -i because it selects the decoder for the
input rather than the encoder for the output.

An earlier version of this script keyed out the black instead, on the
assumption there was no alpha to recover. It produced about 20% fewer
semi-transparent pixels: the key was flattening anti-aliased edges the source
already had. Do not reintroduce it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "evasset" / "assets" / "eve-booty-chest.webm"
OUTPUT = ROOT / "src" / "evasset" / "assets" / "eve-booty-chest.webp"

# 256px at 15fps lands around 575 KB, a bit over 1% of the release zip. The
# source is 24fps, and halving it is not visible on a slow looping animation.
SIZE = 256
FPS = 15
QUALITY = 75


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ffmpeg is not on PATH. Install it with: winget install Gyan.FFmpeg",
              file=sys.stderr)
        return 1
    if not SOURCE.exists():
        print(f"missing source: {SOURCE}", file=sys.stderr)
        return 1

    cmd = [
        ffmpeg, "-v", "error", "-y",
        # Before -i: this picks the decoder for the input. The native vp9
        # decoder drops the alpha plane silently.
        "-c:v", "libvpx-vp9",
        "-i", str(SOURCE),
        "-vf", f"format=rgba,fps={FPS},scale={SIZE}:{SIZE}:flags=lanczos",
        "-c:v", "libwebp_anim",
        "-lossless", "0",
        "-q:v", str(QUALITY),
        "-loop", "0",
        "-an",
        str(OUTPUT),
    ]
    print(" ".join(cmd), flush=True)
    code = subprocess.call(cmd)
    if code:
        return code

    print(f"  {OUTPUT.relative_to(ROOT)}  {OUTPUT.stat().st_size:,} bytes")

    # Verify it is actually playable and actually transparent, rather than
    # trusting that ffmpeg wrote something. A silently opaque animation would
    # look like a black square pasted over the window.
    try:
        from PySide6.QtGui import QMovie
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("  (PySide6 unavailable, skipping verification)")
        return 0

    QApplication(["make_chest_animation", "-platform", "offscreen"])
    movie = QMovie(str(OUTPUT))
    if not movie.isValid():
        print("  QMovie cannot read the result", file=sys.stderr)
        return 1
    movie.start()
    movie.jumpToFrame(min(20, max(movie.frameCount() - 1, 0)))
    image = movie.currentImage()
    movie.stop()
    if not image.hasAlphaChannel():
        print("  the result has no alpha channel", file=sys.stderr)
        return 1
    corner = image.pixelColor(2, 2).alpha()
    if corner != 0:
        print(f"  corner pixel is not transparent (alpha={corner})", file=sys.stderr)
        return 1
    print(f"  verified: {movie.frameCount()} frames, alpha ok, corner transparent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
