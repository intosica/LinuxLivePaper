"""ffmpeg/GdkPixbuf helpers: cached tile thumbnails, probing, exact frame grabs, MP4 proxies.
All functions block; call them from worker threads."""
import functools
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from . import library  # noqa: E402

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "linuxlivepaper"
WIDTH = 480
# square the pixels first: DVD/VOB video is often anamorphic (720x480 shown as 16:9), and filters/pixbufs ignore SAR
SQUARE = "scale=trunc(iw*sar/2)*2:ih,setsar=1"
LOG = CACHE / "log.txt"
# Every ffmpeg runs at the lowest CPU priority: a 4K cell render can keep all cores busy for seconds, and at normal
# priority that starved the desktop (audio pops/lag in other apps). Niced, it only uses CPU nobody else wants.
NICE = ["nice", "-n", "19"] if shutil.which("nice") else []
# One preview bake at a time: each edit while playing used to start another full render on top of the unfinished
# ones. Holders re-check that they are still the latest request after acquiring it and skip if not.
BAKE_LOCK = threading.Lock()


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


def prune(folder, max_bytes):
    """Delete the oldest files under `folder` until it holds at most max_bytes (render caches only grow otherwise)."""
    try:
        files = sorted((f for f in Path(folder).rglob("*") if f.is_file()), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        for f in files:
            if total <= max_bytes:
                break
            total -= f.stat().st_size
            f.unlink(missing_ok=True)
    except OSError:
        pass


@functools.lru_cache(maxsize=64)
def _video_info(src, mtime):
    """(codec, coded width, coded height) of the first video stream."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name,width,height", "-of", "csv=p=0", src], capture_output=True,
                         text=True).stdout.strip().split(",")
    try:
        return out[0], int(out[1]), int(out[2])
    except (IndexError, ValueError):
        return "", 0, 0


LOWRES_CODECS = {"mpeg2video", "mpeg1video", "mjpeg"}  # decoders that can decode straight to 1/2, 1/4, 1/8 size


def lowres(src, need_w, need_h):
    """Largest `-lowres` level (0-3) whose decoded frames are still at least need_w x need_h (0 = full size).
    A 4K MPEG-2 cell shown at 270x480 decodes ~3x faster at 1/4 size."""
    codec, w, h = _video_info(str(src), os.stat(src).st_mtime_ns)
    if codec not in LOWRES_CODECS or not w:
        return 0
    f = 0
    while f < 3 and (w >> (f + 1)) >= need_w and (h >> (f + 1)) >= need_h:
        f += 1
    return f


PREVIEW_HEIGHTS = (("Full", 0), ("720p", 720), ("540p", 540), ("360p", 360), ("240p", 240))


def preview_height():
    """Height (px) the editor/collage *previews* are rendered at, None = the source's own size. Saved files never
    use it. Preference "preview_height" (default 540)."""
    return library.load_config().get("preview_height", 540) or None


def gpu_enabled():
    """The user's "GPU acceleration" preference (default on); LINUXLIVEPAPER_ENCODER=x264 forces CPU."""
    if os.environ.get("LINUXLIVEPAPER_ENCODER", "").lower() in ("x264", "cpu", "software"):
        return False
    return bool(library.load_config().get("gpu", True))


def nvenc():
    """Encode on the GPU: wanted (preference) and NVENC works here."""
    return gpu_enabled() and nvenc_available()


_cache_locks, _cache_guard = {}, threading.Lock()


def cached(out, build, tries=2):
    """Path of `out`, built by build(tmp_path) if missing. Safe for concurrent callers (play + re-bake + Try it all
    ask for the same file): one builds while the others wait, every attempt writes its own temp file that is renamed
    atomically, and a failed build is retried once (a half-written or racing cache file used to fail a preview
    until the next Play)."""
    out = Path(out)
    with _cache_guard:
        lock = _cache_locks.setdefault(str(out), threading.Lock())
    with lock:
        for attempt in range(tries):
            if out.exists() and probe(str(out))[0] > 0:  # a truncated/corrupt leftover is rebuilt, not trusted
                return out
            out.unlink(missing_ok=True)
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(f"{out.stem}.{uuid.uuid4().hex[:8]}.tmp{out.suffix}")
            try:
                build(tmp)
                os.replace(tmp, out)
            except Exception as ex:
                tmp.unlink(missing_ok=True)
                log("cache build failed:", out.name, ex)
                if attempt == tries - 1:
                    raise
        if out.exists() and probe(str(out))[0] > 0:
            return out
        raise RuntimeError("the preview render produced no playable video")


@functools.cache
def nvenc_available():
    """NVIDIA hardware H.264 encoding works here (driver + ffmpeg build)."""
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


HWDEC_CODECS = {"h264", "hevc", "vp9", "av1"}  # NVDEC wins here; MPEG-2 measured slower (and uses -lowres instead)


def hwdec(src, cover=None):
    """Input args that decode src on the GPU (NVDEC) when GPU acceleration is on. cover=(w, h): the frames only need
    to cover that size, so NVDEC also downscales them on the GPU (cuvid -resize) and the CPU never touches a full-size
    frame. 4K H.264 → 480x270, 20 s: 9.1 CPU-s with -hwaccel cuda, 1.4 with -resize (48 on the CPU alone)."""
    if not nvenc():  # same switch + NVIDIA check as encoding
        return []
    codec, cw, ch = _video_info(str(src), os.stat(src).st_mtime_ns)
    if codec not in HWDEC_CODECS:
        return []
    if cover and cw and ch:
        dw = probe(str(src))[1] or cw
        f = max(cover[0] / dw, cover[1] / ch)
        if f < 0.95:
            return ["-c:v", f"{codec}_cuvid", "-resize", f"{max(2, round(cw * f / 2) * 2)}x{max(2, round(ch * f / 2) * 2)}"]
    return ["-hwaccel", "cuda"]


def _software(cmd):
    """cmd with GPU decode dropped and an NVENC encoder swapped back to libx264 (busy GPU, too many sessions, odd
    size)."""
    if "-hwaccel" in cmd:
        i = cmd.index("-hwaccel")
        cmd = cmd[:i] + cmd[i + 2:]
    if "-resize" in cmd:  # ["-c:v", "<codec>_cuvid", "-resize", "WxH"] in front of -i
        i = cmd.index("-resize")
        cmd = cmd[:i - 2] + cmd[i + 2:]
    if "h264_nvenc" not in cmd:
        return cmd
    i = cmd.index("h264_nvenc")
    q = cmd[cmd.index("-cq") + 1]
    return cmd[:i - 1] + ["-c:v", "libx264", "-preset", "faster", "-crf", q] + cmd[i + 9:]  # keeps -pix_fmt


def run(cmd, timeout=600):
    """Like subprocess.run(check=True), but the failure message is ffmpeg's own last line (shown in the UI).
    A failed GPU encode is retried once on the CPU."""
    p = subprocess.run(NICE + cmd, capture_output=True, timeout=timeout)
    if p.returncode and ("h264_nvenc" in cmd or "-hwaccel" in cmd or "-resize" in cmd):
        log("GPU ffmpeg failed, retrying on the CPU:", p.stderr.decode("utf-8", "replace").strip()[-300:])
        p = subprocess.run(NICE + _software(cmd), capture_output=True, timeout=timeout)
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


def grab_frame(src, t, height=None, chain="", key=False):
    """Frame at t seconds as a GdkPixbuf (or None), optionally through an ffmpeg filter chain.

    key: nearest keyframe at/before t instead of the exact frame (filmstrips) — skips decoding up to t, ~6× cheaper.
    Seeking to the very last frame of a file yields nothing, so fall back a little when the exact time is empty."""
    at = max(0.0, t)
    for back in (0.0, 0.2, 0.6):
        pb = _frame_at(src, max(0.0, at - back), height, chain, key)
        if pb is not None:
            return pb
    return _frame_at(src, 0.0, height, chain, key) if at else None


def _frame_at(src, t, height=None, chain="", key=False):
    vf = ["-vf", ",".join(c.strip(",") for c in (SQUARE, chain, f"scale=-2:{height}" if height else "") if c.strip(","))]
    seek = ["-skip_frame", "nokey", "-noaccurate_seek"] if key else ["-threads", "2"]  # 2 threads: less CPU, as fast
    png = subprocess.run([*NICE, "ffmpeg", "-v", "error", *seek, "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", *vf,
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
    out = CACHE / "thumbs" / (_key(src, "mid") + ".jpg")  # "mid": frame at 50%, not the old ~1 s
    if out.exists():
        return str(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if item.kind != "live":  # video or GIF (GdkPixbuf's GIF loader chokes on many real-world GIFs)
            pb = grab_frame(src, video_duration(src) / 2, None)
            pb = pb.scale_simple(WIDTH, max(1, pb.get_height() * WIDTH // pb.get_width()),
                                 GdkPixbuf.InterpType.BILINEAR)
        else:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(src, WIDTH, -1, True)
            pb = pb.apply_embedded_orientation() or pb
        if pb.get_has_alpha():  # flatten for JPEG: composite_color_simple stays RGBA, which the flatpak's glycin JPEG encoder rejects
            w, h = pb.get_width(), pb.get_height()
            flat = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, w, h)
            flat.fill(0x202020ff)
            pb.composite(flat, 0, 0, w, h, 0, 0, 1, 1, GdkPixbuf.InterpType.BILINEAR, 255)
            pb = flat
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
    trim = (["-ss", f"{start:.3f}"] if start is not None else []) + (["-to", f"{end:.3f}"] if end is not None else [])
    _, w, h = probe(src)
    oh = min(height, h // 2 * 2)
    return str(cached(out, lambda tmp: run(
        ["ffmpeg", "-v", "error", "-y", *trim, "-i", src, "-map", "0:v:0", "-map", "0:a:0?",
         "-vf", f"{SQUARE},scale=-2:'min({height},trunc(ih/2)*2)',format=yuv420p",
         *h264(True, (w * oh // max(1, h), oh)), "-c:a", "aac", str(tmp)])))


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
        assert _software(["ffmpeg", "-ss", "1", "-c:v", "h264_cuvid", "-resize", "480x270", "-i", "x", "o.mkv"]) == \
            ["ffmpeg", "-ss", "1", "-i", "x", "o.mkv"]
        assert _software(["ffmpeg", "-hwaccel", "cuda", "-i", "x", "o.mkv"]) == ["ffmpeg", "-i", "x", "o.mkv"]
        vob = os.path.join(d, "dvd.vob")  # anamorphic 16:9 DVD: 720x480 storage, 32:27 pixels
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=720x480:d=1", "-f", "lavfi",
                        "-i", "sine=d=1", "-target", "ntsc-dvd", "-aspect", "16:9", vob], check=True)
        assert probe(vob)[1:] == (853, 480), probe(vob)
        assert grab_frame(vob, 0.5).get_width() == 852
        assert probe(mp4_proxy(vob))[1:] == (852, 480), probe(mp4_proxy(vob))
        # GPU preference off -> x264 whatever the hardware; on -> NVENC only if it works here
        real = gpu_enabled
        globals()["gpu_enabled"] = lambda: False
        assert h264()[1] == "libx264"
        globals()["gpu_enabled"] = real
        # concurrent callers for one cache file: built once, nobody fails on a shared temp file; a corrupt leftover is rebuilt
        calls, outs = [], []

        def build(tmp):
            calls.append(1)
            time.sleep(0.3)
            run(["ffmpeg", "-v", "error", "-y", "-i", gif, "-pix_fmt", "yuv420p", str(tmp)])
        target = Path(d, "c", "x.mp4")
        ts = [threading.Thread(target=lambda: outs.append(cached(target, build))) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(calls) == 1 and len(outs) == 4 and probe(str(target))[0] > 0, (calls, outs)
        target.write_bytes(b"garbage")
        cached(target, build)
        assert len(calls) == 2 and probe(str(target))[0] > 0
        assert not list(Path(d, "c").glob("*.tmp.*"))
    print("thumbs ok")
