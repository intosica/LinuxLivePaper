"""Cached square-ish thumbnails (JPEG, 480px wide) for gallery tiles. Safe to call from worker threads."""
import hashlib
import os
import subprocess
from pathlib import Path

from gi.repository import GdkPixbuf

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "linuxlivepaper" / "thumbs"
WIDTH = 480


def thumb_path(src):
    key = f"{src}:{os.stat(src).st_mtime_ns}".encode()
    return CACHE / (hashlib.sha1(key).hexdigest() + ".jpg")


def video_duration(src):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", src],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def make_thumb(item):
    """Return a cached thumbnail path for an Item, or None if it can't be decoded."""
    src = item.path
    out = thumb_path(src)
    if out.exists():
        return str(out)
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    try:
        if item.kind == "video":
            ss = min(1.0, video_duration(src) / 2)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{ss:.2f}", "-i", src, "-frames:v", "1",
                            "-vf", f"scale={WIDTH}:-2", "-q:v", "4", str(tmp)], check=True, timeout=30)
        else:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(src, WIDTH, -1, True)
            pb = pb.apply_embedded_orientation() or pb
            if pb.get_has_alpha():  # flatten for JPEG
                pb = pb.composite_color_simple(pb.get_width(), pb.get_height(),
                                               GdkPixbuf.InterpType.BILINEAR, 255, 16, 0x202020, 0x202020)
            pb.savev(str(tmp), "jpeg", ["quality"], ["85"])
        os.replace(tmp, out)
        return str(out)
    except Exception:
        tmp.unlink(missing_ok=True)
        return None
