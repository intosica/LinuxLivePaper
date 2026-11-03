"""Overlays (text and stickers) baked into a live image with ffmpeg.

An overlay is a plain dict: position/size in canvas fractions (0..1), a time window in output seconds, and a
resource (text, or an image/GIF path — GIFs keep their alpha through `format=rgba`)."""
import subprocess
import tempfile
from pathlib import Path


def text(value="Your text", **kw):
    return {"kind": "text", "text": value, "x": 0.5, "y": 0.86, "size": 0.09, "color": "#ffffff",
            "outline": True, "opacity": 1.0, "start": None, "end": None, **kw}


def sticker(path, **kw):
    return {"kind": "sticker", "path": str(path), "x": 0.5, "y": 0.5, "size": 0.35, "opacity": 1.0,
            "start": None, "end": None, **kw}


def payload(o):
    """Everything that changes the rendered result (for cache keys)."""
    return tuple(sorted((k, v) for k, v in o.items() if k != "id"))


def _esc(value, path=False):
    """Escape a value for use inside a filter_complex argument."""
    if path:  # textfile/gif paths: escaping differs from plain option values
        return str(value).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return (str(value).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            .replace("%", "\\%").replace(",", "\\,").replace("[", "\\[").replace("]", "\\]"))


def _window(o, dur):
    s = 0.0 if o.get("start") is None else max(0.0, float(o["start"]))
    e = dur if o.get("end") is None else min(dur, float(o["end"]))
    return s, e


def graph(overlays, W, H, dur, first_input=0):
    """Build (extra ffmpeg inputs, filter tail). The tail expects the label [vout] and yields [ovout]."""
    inputs, steps, label = [], [], "vout"
    n_input = first_input
    for n, o in enumerate(overlays or []):
        s, e = _window(o, dur)
        if e <= s:
            continue
        out = f"ov{n}"
        alpha = f"{max(0.0, min(1.0, o.get('opacity', 1.0))):.2f}"
        if o["kind"] == "text":
            fd, tmp = tempfile.mkstemp(prefix="llp-text-", suffix=".txt")
            with open(fd, "w") as fh:
                fh.write(o.get("text") or "")
            steps.append(
                f"[{label}]drawtext=textfile='{_esc(tmp, True)}':fontcolor={o.get('color', '#ffffff')}@{alpha}"
                f":fontsize={max(8, int(H * o.get('size', 0.09)))}"
                f":borderw={3 if o.get('outline', True) else 0}:bordercolor=black@0.55"
                f":x=(w*{o.get('x', 0.5):.4f})-text_w/2:y=(h*{o.get('y', 0.5):.4f})-text_h/2"
                f":enable='between(t,{s:.3f},{e:.3f})'[{out}]")
        else:
            idx = n_input
            n_input += 1
            inputs += (["-loop", "1"] if Path(o["path"]).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                       else ["-stream_loop", "-1"]) + ["-i", o["path"]]
            px = max(8, int(W * o.get("size", 0.35)))
            steps.append(
                f"[{idx}:v]format=rgba,scale={px}:-1:flags=lanczos,colorchannelmixer=aa={alpha},"
                f"trim=duration={dur:.3f},setpts=PTS-STARTPTS[st{n}];"
                f"[{label}][st{n}]overlay=x=(W*{o.get('x', 0.5):.4f})-w/2:y=(H*{o.get('y', 0.5):.4f})-h/2"
                f":enable='between(t,{s:.3f},{e:.3f})':eof_action=pass:shortest=0[{out}]")
        label = out
    return inputs, steps, label


def render(src, start, end, out, opts=None, overlays=(), height=None, fast=False, extra=(), loop=False):
    """Like effects.render(), with overlays composited on top."""
    from . import effects
    opts = {**effects.DEFAULT, **(opts or {})}
    audio = effects.has_audio(src)
    _, w, h = _probe(src)
    W, H = effects.out_size(w, h, height)
    trim = (["-stream_loop", "-1"] if loop else []) + ["-ss", f"{start:.3f}"] + \
        (["-t", f"{end - start:.3f}"] if end is not None else [])
    ov_inputs, steps, label = graph(overlays, W, H, end - start, first_input=1)
    base = effects.graph(opts, audio, (W, H))
    body = f"{base};" + ";".join(steps) if steps else base
    if steps:
        body += f";[{label}]format=yuv420p[fin]"
    cmd = ["ffmpeg", "-v", "error", "-y", *trim, "-i", str(src), *ov_inputs, "-filter_complex", body,
           "-map", "[fin]" if steps else "[vout]",
           *(["-map", "[aout]", "-c:a", "aac"] if audio else []),
           "-c:v", "libx264",
           *(["-preset", "ultrafast", "-crf", "23"] if fast else ["-preset", "faster", "-crf", "18"]), *extra, str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def preview(src, start, end, opts, cache_dir, height=540, loop=True, overlay_list=()):
    """Cached low-res render of the selection with overlays, for the editor's looping preview."""
    import hashlib
    key = hashlib.sha1(repr((str(src), Path(src).stat().st_mtime_ns, round(start, 3), round(end, 3),
                             sorted((opts or {}).items()), height, loop, [payload(o) for o in overlay_list],
                             [o.get("id") for o in overlay_list])).encode()).hexdigest()
    out = Path(cache_dir) / (key + ".mp4")
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.mp4")
        render(src, start, end, tmp, opts, overlays=overlay_list, height=height, fast=True, loop=loop)
        out.unlink(missing_ok=True)
        tmp.rename(out)
    return str(out)


def _probe(src):
    from . import thumbs
    return thumbs.probe(str(src))


if __name__ == "__main__":
    import json
    from . import effects
    probe = ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
             "-of", "json"]
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        src, gif = d / "src.mp4", d / "s.gif"
        # a sticker with transparency: a small circle on a transparent background
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-vf",
                        "format=rgba,geq=r=255:g=60:b=60:a='if(lt(hypot(X-32,Y-32),20),255,0)'",
                        "-frames:v", "1", str(gif)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2", str(src)],
                       check=True)
        ovs = [text("Hello", start=0.2, end=1.5), sticker(gif, size=0.25)]
        out = render(src, 0, 1.6, d / "o.mp4", overlays=ovs, fast=True)
        j = json.loads(subprocess.run([*probe, str(out)], capture_output=True, text=True).stdout)
        assert (j["streams"][0]["width"], j["streams"][0]["height"]) == (320, 240), j
        assert abs(float(j["format"]["duration"]) - 1.6) < 0.15, j
        # alpha check: the sticker's corners stay black (transparent), its middle is red
        frame = subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.8", "-i", str(out), "-frames:v", "1",
                                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
        at = lambda x, y: tuple(frame[(y * 320 + x) * 3:(y * 320 + x) * 3 + 3])  # noqa: E731
        assert at(160, 120)[0] > 120 and at(160, 120)[1] < 90, at(160, 120)   # sticker centre
        assert max(at(160, 20)) < 40, at(160, 20)                             # above it: untouched black
        # the text window really is limited
        assert effects.out_size(320, 240, 120) == (160, 120)
    print("overlays ok")
