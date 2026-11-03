"""ffmpeg/GdkPixbuf helpers: cached tile thumbnails, probing, exact frame grabs, MP4 proxies.
All functions block; call them from worker threads."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "linuxlivepaper"
WIDTH = 480


def _key(src, *extra):
    st = os.stat(src)
    return hashlib.sha1(f"{src}:{st.st_mtime_ns}:{extra}".encode()).hexdigest()


def probe(src):
    """(duration_s, width, height) of a video/GIF."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "format=duration:stream=width,height", "-of", "json", src],
                         capture_output=True, text=True).stdout
    try:
        j = json.loads(out)
        s = j["streams"][0]
        return float(j["format"].get("duration", 0) or 0), int(s["width"]), int(s["height"])
    except (ValueError, KeyError, IndexError):
        return 0.0, 16, 9


def video_duration(src):
    return probe(src)[0]


def grab_frame(src, t, height=None):
    """Exact frame at t seconds as a GdkPixbuf (or None)."""
    vf = ["-vf", f"scale=-2:{height}"] if height else []
    png = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", src, "-frames:v", "1", *vf,
                          "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True).stdout
    if not png:
        return None
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(png)
        loader.close()
        return loader.get_pixbuf()
    except GLib.Error:
        return None


def make_thumb(item):
    """Cached JPEG thumbnail path for a gallery Item, or None."""
    src = item.path
    out = CACHE / "thumbs" / (_key(src) + ".jpg")
    if out.exists():
        return str(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if item.kind != "live":  # video or GIF (GdkPixbuf's GIF loader chokes on many real-world GIFs)
            pb = grab_frame(src, min(1.0, video_duration(src) / 2), None)
            pb = pb.scale_simple(WIDTH, max(1, pb.get_height() * WIDTH // pb.get_width()),
                                 GdkPixbuf.InterpType.BILINEAR)
        else:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(src, WIDTH, -1, True)
            pb = pb.apply_embedded_orientation() or pb
        if pb.get_has_alpha():  # flatten for JPEG
            pb = pb.composite_color_simple(pb.get_width(), pb.get_height(),
                                           GdkPixbuf.InterpType.BILINEAR, 255, 16, 0x202020, 0x202020)
        tmp = out.with_suffix(".tmp")
        pb.savev(str(tmp), "jpeg", ["quality"], ["85"])
        os.replace(tmp, out)
        return str(out)
    except Exception:
        return None


def mp4_proxy(src, start=None, end=None, height=720):
    """Cached H.264 MP4 of src (optionally trimmed). Used for GIF playback (GTK's GStreamer
    player can't play GIFs) and for frame-exact looping of the editor selection."""
    out = CACHE / "proxies" / (_key(src, start, end, height) + ".mp4")
    if out.exists():
        return str(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    trim = (["-ss", f"{start:.3f}"] if start is not None else []) + (["-to", f"{end:.3f}"] if end is not None else [])
    tmp = out.with_suffix(".tmp.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", *trim, "-i", src, "-map", "0:v:0", "-map", "0:a:0?",
                    "-vf", f"scale=-2:'min({height},trunc(ih/2)*2)',format=yuv420p",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-c:a", "aac", str(tmp)], check=True)
    os.replace(tmp, out)
    return str(out)


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        gif = os.path.join(d, "a.gif")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:d=1", gif], check=True)
        dur, w, h = probe(gif)
        assert (w, h) == (160, 90) and 0.9 < dur < 1.1, (dur, w, h)
        assert grab_frame(gif, 0.5, 40).get_height() == 40
        p = mp4_proxy(gif)
        assert probe(p)[1:] == (160, 90), probe(p)
        assert mp4_proxy(gif) == p  # cached
    print("thumbs ok")
