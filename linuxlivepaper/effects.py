"""Bake-in effects for the creator: looks (per-frame color), effects (stylised motion), speed and direction.
Everything is one ffmpeg filter_complex, so the editor preview proxy and the saved MOV are rendered identically."""
import functools
import hashlib
import os
import subprocess
from pathlib import Path

from . import thumbs

# id → (label, ffmpeg chain). Chains keep the frame size; may use internal [labels] (unique prefixes).
LOOKS = {
    "none": ("Original", ""),
    "vivid": ("Vivid", "eq=saturation=1.45:contrast=1.08"),
    "warm": ("Warm", "colorbalance=rs=0.08:gs=0.02:bs=-0.08:rm=0.06:bm=-0.06,eq=saturation=1.1"),
    "cool": ("Cool", "colorbalance=rs=-0.06:bs=0.1:rm=-0.04:bm=0.08"),
    "cine": ("Cinematic", "colorbalance=rs=-0.08:gs=0.02:bs=0.1:rh=0.08:bh=-0.08,eq=contrast=1.1:saturation=1.1"),
    "fade": ("Faded", "curves=all='0/0.12 1/0.9',eq=saturation=0.75"),
    "vintage": ("Vintage", "curves=preset=vintage"),
    "sepia": ("Sepia", "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"),
    "mono": ("Mono", "hue=s=0"),
    "noir": ("Noir", "hue=s=0,eq=contrast=1.5:brightness=-0.05,vignette=PI/4"),
}
EFFECTS = {
    "none": ("None", ""),
    "glitch": ("Glitch", "rgbashift=rh=-8:bh=8:rv=2,noise=alls=12:allf=t"),
    "vhs": ("VHS", "rgbashift=rh=-4:bh=4,eq=saturation=0.8:contrast=1.1,noise=alls=22:allf=t+u,gblur=sigma=0.8"),
    "dreamy": ("Dreamy", "format=gbrp,split[fxa][fxb];[fxb]gblur=sigma=18[fxc];[fxa][fxc]blend=all_mode=screen:all_opacity=0.6"),
    "pulse": ("Pulse", "eq=brightness=0.06*sin(2*PI*t*2):saturation=1+0.35*sin(2*PI*t*2):eval=frame"),
    "shake": ("Shake", "scale=trunc(iw*1.08/2)*2:-2,crop=iw/1.08:ih/1.08:"
                       "(iw-ow)/2+sin(t*37)*iw*0.012:(ih-oh)/2+cos(t*29)*ih*0.012"),
    "rainbow": ("Rainbow", "hue=h=t*120"),
    "pixel": ("Pixel", "pixelize=w=14:h=14"),
    "mirror": ("Mirror", "crop=trunc(iw/4)*2:ih:0:0,split[fxl][fxr];[fxr]hflip[fxf];[fxl][fxf]hstack"),
}
SPEED_MIN, SPEED_MAX = 0.1, 4.0  # the Speed slider's range (1.0 = normal)
DIRECTIONS = {"forward": "Forward", "reverse": "Reverse", "bounce": "Bounce"}
DEFAULT = {"look": "none", "effect": "none", "speed": 1.0, "direction": "forward"}


def atempo(speed):
    """Pitch-preserving time-stretch (ffmpeg's WSOLA `atempo`, the usual way to slow audio without the chipmunk/
    demon pitch shift). One atempo stage only reaches 0.5x, so slower speeds are split into equal stages (four
    gentle 0.56x stages sound cleaner than 0.5, 0.5, 0.5, 0.8)."""
    n = 1
    while speed ** (1 / n) < 0.5:
        n += 1
    return ",".join([f"atempo={speed ** (1 / n):.6f}"] * n)


def frame_chain(opts):
    """Per-frame part (look + effect), usable as -vf for still previews."""
    return ",".join(c for c in (LOOKS[opts["look"]][1], EFFECTS[opts["effect"]][1]) if c)


def out_length(length, opts):
    n = length / opts["speed"]
    return 2 * n if opts["direction"] == "bounce" else n


def out_time(rel, length, opts):
    """Where source time `rel` (seconds after the selection start) lands in the rendered clip."""
    t = rel / opts["speed"]
    return length / opts["speed"] - t if opts["direction"] == "reverse" else t


def out_size(w, h, height=None):
    """Even output size, optionally capped to `height`."""
    if height and h > height:
        w, h = w * height / h, height
    return int(w) // 2 * 2, int(h) // 2 * 2


def src_time(u, length, opts):
    """Inverse of out_time: source seconds after the selection start shown at rendered time u."""
    n = length / opts["speed"]
    u = u % (2 * n) if opts["direction"] == "bounce" else u
    if opts["direction"] == "reverse" or u > n:
        u = 2 * n - u if u > n else n - u
    return min(max(u * opts["speed"], 0.0), length)


def graph(opts, has_audio, size):
    """filter_complex string; outputs [vout] at exactly `size` (and [aout] if has_audio)."""
    v = [c for c in (frame_chain(opts), f"setpts=(PTS-STARTPTS)/{opts['speed']}") if c]
    d = opts["direction"]
    # ponytail: reverse/bounce buffer every frame in RAM (fine for seconds-long clips; segment it for long ones)
    if d == "reverse":
        v.append("reverse")
    elif d == "bounce":
        v.append("split[bf][bb];[bb]reverse[br];[bf][br]concat=n=2:v=1:a=0")
    parts = [f"[0:v]{','.join(v)},scale={size[0]}:{size[1]},setsar=1,format=yuv420p[vout]"]
    if has_audio:
        a = ["asetpts=PTS-STARTPTS"] + ([atempo(opts["speed"])] if opts["speed"] != 1 else [])
        if d == "reverse":
            a.append("areverse")
        elif d == "bounce":
            a.append("asplit[af][ab];[ab]areverse[ar];[af][ar]concat=n=2:v=0:a=1")
        parts.append(f"[0:a:0]{','.join(a)}[aout]")
    return ";".join(parts)


def has_audio(src):
    return _has_audio(str(src), os.stat(src).st_mtime_ns)


@functools.lru_cache(maxsize=256)
def _has_audio(src, mtime):  # an ffprobe per call adds up when a collage re-renders every cell
    return bool(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                                "-of", "csv=p=0", src], capture_output=True, text=True).stdout.strip())


def render(src, start, end, out, opts=None, height=None, fast=False, extra=(), loop=False, progress=None):
    """Render [start, end] of src through the effect graph to out (H.264/AAC)."""
    opts = {**DEFAULT, **(opts or {})}
    audio = has_audio(src)
    _, w, h = thumbs.probe(str(src))
    trim = (["-stream_loop", "-1"] if loop else []) + ["-ss", f"{start:.3f}"] + \
        (["-t", f"{end - start:.3f}"] if end is not None else [])
    cmd = ["ffmpeg", "-v", "error", "-y", *trim, *thumbs.hwdec(src, height and not frame_chain(opts) and out_size(w, h, height)), "-i", str(src), "-filter_complex", graph(opts, audio, out_size(w, h, height)),
           "-map", "[vout]", *(["-map", "[aout]", "-c:a", "aac"] if audio else []),
           *thumbs.h264(fast, out_size(w, h, height)), *extra, str(out)]
    thumbs.run(cmd, progress=progress, total=end is not None and out_length(end - start, opts))
    return out


def preview(src, start, end, opts, cache_dir, height=None, loop=False):
    """Cached low-res render of the selection for looping previews (`loop`: repeat a too-short source)."""
    key = hashlib.sha1(f"{src}:{os.stat(src).st_mtime_ns}:{start:.3f}:{end:.3f}:{sorted(opts.items())}:{height}:{loop}"
                       .encode())
    out = Path(cache_dir) / (key.hexdigest() + ".mp4")
    return str(thumbs.cached(out, lambda tmp: render(src, start, end, tmp, opts, height=height, fast=True, loop=loop)))


if __name__ == "__main__":
    import json
    import tempfile
    probe = ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json"]
    with tempfile.TemporaryDirectory() as d:
        src = Path(d, "src.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=3", "-f", "lavfi",
                        "-i", "sine=d=3", "-shortest", str(src)], check=True)
        # every look and effect renders and keeps the size; speed/bounce change the length as expected
        cases = [{"look": k} for k in LOOKS] + [{"effect": k} for k in EFFECTS] + \
                [{"speed": 2.0, "direction": "bounce"}, {"speed": 0.5, "direction": "reverse"}, {"speed": 0.15}]
        for o in cases:
            out = render(src, 0.5, 2.5, Path(d, "o.mp4"), o, fast=True)
            j = json.loads(subprocess.run([*probe, str(out)], capture_output=True, text=True).stdout)
            sizes = [(s["width"], s["height"]) for s in j["streams"] if s["codec_type"] == "video"]
            o = {**DEFAULT, **o}
            assert sizes == [(320, 240)], (o, sizes)
            assert abs(float(j["format"]["duration"]) - out_length(2.0, o)) < 0.15, (o, j["format"])
            assert len(j["streams"]) == 2, o
        # slow audio keeps its pitch: a 440 Hz sine at 0.25x is still ~440 Hz and 4x as long
        wav = Path(d, "s.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1", "-af", atempo(0.25),
                        "-ar", "8000", "-ac", "1", str(wav)], check=True)
        import wave
        w = wave.open(str(wav))
        pcm = memoryview(w.readframes(w.getnframes())).cast("h")
        hz = sum(a < 0 <= b for a, b in zip(pcm, pcm[1:])) / (w.getnframes() / w.getframerate())
        assert abs(w.getnframes() / w.getframerate() - 4) < 0.1 and abs(hz - 440) < 15, (hz, w.getnframes())
        assert all(float(x) >= 0.5 for x in atempo(0.1).replace("atempo=", "").split(","))
        assert out_time(0.5, 2.0, {**DEFAULT, "direction": "reverse"}) == 1.5
        assert out_time(1.0, 2.0, {**DEFAULT, "speed": 2.0}) == 0.5
        for o in ({**DEFAULT, "direction": "reverse", "speed": 0.5}, {**DEFAULT, "speed": 2.0}):
            assert abs(src_time(out_time(0.7, 2.0, o), 2.0, o) - 0.7) < 1e-9, o
        b = {**DEFAULT, "direction": "bounce"}
        assert src_time(0.5, 2.0, b) == 0.5 and src_time(3.5, 2.0, b) == 0.5
    print("effects ok")
