"""ffmpeg/GdkPixbuf helpers: cached tile thumbnails, probing, exact frame grabs, MP4 proxies.
All functions block; call them from worker threads."""
import functools
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "linuxlivepaper"
WIDTH = 480
# square the pixels first: DVD/VOB video is often anamorphic (720x480 shown as 16:9), and filters/pixbufs ignore SAR
SQUARE = "scale=trunc(iw*sar/2)*2:ih,setsar=1"
LOG = CACHE / "log.txt"


def log(*parts):
    """Append a line to ~/.cache/linuxlivepaper/log.txt — the only place a failed render explains itself."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 400_000:
            LOG.unlink()
        with LOG.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(str(p) for p in parts)}\n")
    except OSError:
        pass


@functools.cache
def nvenc():
    """NVIDIA hardware H.264 encoding works here (driver + ffmpeg build). LINUXLIVEPAPER_ENCODER=x264 turns it off."""
    if os.environ.get("LINUXLIVEPAPER_ENCODER", "").lower() in ("x264", "cpu", "software"):
        return False
    try:
        return subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "color=s=256x256:d=0.1", "-c:v",
                               "h264_nvenc", "-f", "null", "-"], timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def h264(fast=True, size=None, q=None):
    """Video encoder args: NVENC on the GPU when available (quiet fans, ~4x faster), else libx264.
    q is the x264 CRF (NVENC's -cq is matched to it). NVENC rejects frames under ~146x49, those stay on x264."""
    q = q or (23 if fast else 18)
    if nvenc() and not (size and (size[0] < 160 or size[1] < 64)):
        # ponytail: NVIDIA only; AMD/Intel (VAAPI) fall back to x264, add h264_vaapi if someone asks
        return ["-c:v", "h264_nvenc", "-preset", "p2" if fast else "p5", "-rc", "vbr", "-cq", str(q), "-b:v", "0",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "ultrafast" if fast else "faster", "-crf", str(q)]


def _software(cmd):
    """cmd with an NVENC encoder swapped back to libx264 (busy GPU, too many sessions, odd size)."""
    i = cmd.index("h264_nvenc")
    q = cmd[cmd.index("-cq") + 1]
    return cmd[:i - 1] + ["-c:v", "libx264", "-preset", "faster", "-crf", q] + cmd[i + 9:]  # keeps -pix_fmt


def run(cmd, timeout=600):
    """Like subprocess.run(check=True), but the failure message is ffmpeg's own last line (shown in the UI).
    A failed GPU encode is retried once on the CPU."""
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode and "h264_nvenc" in cmd:
        log("nvenc failed, retrying with x264:", p.stderr.decode("utf-8", "replace").strip()[-300:])
        p = subprocess.run(_software(cmd), capture_output=True, timeout=timeout)
    if p.returncode:
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(err[-1] if err else f"ffmpeg exited {p.returncode}")
    return p


def _key(src, *extra):
    st = os.stat(src)
    return hashlib.sha1(f"{src}:{st.st_mtime_ns}:{extra}".encode()).hexdigest()


def probe(src):
    """(duration_s, display width, height) of a video/GIF (width corrected for non-square pixels)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "format=duration:stream=width,height,sample_aspect_ratio", "-of", "json", src],
                         capture_output=True, text=True).stdout
    try:
        j = json.loads(out)
        s = j["streams"][0]
        n, _, d = s.get("sample_aspect_ratio", "1:1").partition(":")
        sar = int(n) / int(d) if n.isdigit() and d.isdigit() and int(n) and int(d) else 1.0
        return float(j["format"].get("duration", 0) or 0), round(int(s["width"]) * sar), int(s["height"])
    except (ValueError, KeyError, IndexError):
        return 0.0, 16, 9


def video_duration(src):
    return probe(src)[0]


def grab_frame(src, t, height=None, chain=""):
    """Frame at t seconds as a GdkPixbuf (or None), optionally through an ffmpeg filter chain.

    Seeking to the very last frame of a file yields nothing, so fall back a little when the exact time is empty."""
    at = max(0.0, t)
    for back in (0.0, 0.2, 0.6):
        pb = _frame_at(src, max(0.0, at - back), height, chain)
        if pb is not None:
            return pb
    return _frame_at(src, 0.0, height, chain) if at else None


def _frame_at(src, t, height=None, chain=""):
    vf = ["-vf", ",".join(c.strip(",") for c in (SQUARE, chain, f"scale=-2:{height}" if height else "") if c.strip(","))]
    png = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", *vf,
                          "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True, timeout=60).stdout
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


def alpha_frame(path, height=360):
    """First frame of an image/GIF as a PNG buffer with its alpha intact (stickers)."""
    key = CACHE / "stickers" / (_key(path, height) + ".png")
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-frames:v", "1",
                        "-vf", f"scale=-1:'min({height},ih)'", str(key)], check=True)
    return key


def mp4_proxy(src, start=None, end=None, height=720):
    """Cached H.264 MP4 of src (optionally trimmed). Used for GIF playback (GTK's GStreamer
    player can't play GIFs) and for frame-exact looping of the editor selection."""
    out = CACHE / "proxies" / (_key(src, start, end, height) + ".mp4")
    if out.exists():
        return str(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    trim = (["-ss", f"{start:.3f}"] if start is not None else []) + (["-to", f"{end:.3f}"] if end is not None else [])
    tmp = out.with_suffix(".tmp.mp4")
    _, w, h = probe(src)
    oh = min(height, h // 2 * 2)
    run(["ffmpeg", "-v", "error", "-y", *trim, "-i", src, "-map", "0:v:0", "-map", "0:a:0?",
         "-vf", f"{SQUARE},scale=-2:'min({height},trunc(ih/2)*2)',format=yuv420p",
         *h264(True, (w * oh // max(1, h), oh)), "-c:a", "aac", str(tmp)])
    os.replace(tmp, out)
    return str(out)


def repeat(src, out, n, extra=()):
    """`out` = `src` played n times back to back (stream copy, no re-encode)."""
    run(["ffmpeg", "-v", "error", "-y", "-stream_loop", str(max(0, int(n) - 1)), "-i", str(src), "-map", "0",
         "-c", "copy", *extra, str(out)])
    return out


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        gif = os.path.join(d, "a.gif")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:d=1", gif], check=True)
        dur, w, h = probe(gif)
        assert (w, h) == (160, 90) and 0.9 < dur < 1.1, (dur, w, h)
        assert grab_frame(gif, 0.5, 40).get_height() == 40
        assert grab_frame(gif, 0.5, 40, "hue=s=0,") is not None  # an empty trailing filter (no look) is fine
        p = mp4_proxy(gif)
        assert probe(p)[1:] == (160, 90), probe(p)
        assert mp4_proxy(gif) == p  # cached
        assert 2.9 < probe(repeat(p, os.path.join(d, "r.mp4"), 3))[0] < 3.2
        big = os.path.join(d, "big.mp4")  # GPU (or CPU fallback) encode, and the x264 swap keeps the quality
        run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=1", *h264(False), big])
        assert probe(big)[1:] == (640, 360)
        nv = ["ffmpeg", "-i", "x", "-c:v", "h264_nvenc", "-preset", "p2", "-rc", "vbr", "-cq", "19", "-b:v", "0",
              "-pix_fmt", "yuv420p", "out.mp4"]
        assert _software(nv) == ["ffmpeg", "-i", "x", "-c:v", "libx264", "-preset", "faster", "-crf", "19", "-pix_fmt",
                                "yuv420p", "out.mp4"]
        assert h264(size=(96, 160))[1] == "libx264"
        vob = os.path.join(d, "dvd.vob")  # anamorphic 16:9 DVD: 720x480 storage, 32:27 pixels
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=720x480:d=1", "-f", "lavfi",
                        "-i", "sine=d=1", "-target", "ntsc-dvd", "-aspect", "16:9", vob], check=True)
        assert probe(vob)[1:] == (853, 480), probe(vob)
        assert grab_frame(vob, 0.5).get_width() == 852
        assert probe(mp4_proxy(vob))[1:] == (852, 480), probe(mp4_proxy(vob))
    print("thumbs ok")
