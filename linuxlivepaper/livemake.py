"""Create a live image (still + motion pair) from a video: the format the gallery pairs on.
Writes Apple-style identifiers too, so iPhone compatibility can be finished later (see docs/ARCHITECTURE.md)."""
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import effects, interp, overlays as ov, thumbs
from .library import library_dir


def make_live(src, start=0.0, end=None, key_time=None, out_dir=None, name=None, opts=None, overlay_list=(),
              repeat=1, interpolate=None):
    """Render [start, end] of src (video or GIF) through effects `opts` → <name>.mov (played `repeat` times back
    to back), and grab the frame at key_time (source seconds after start, mapped through speed/reverse) → <name>.jpg.
    interpolate: interp settings (RIFE/FLAVR frames added to the one pass before repeating), None/off = none.
    Defaults to the library folder."""
    jpg, mov = out_paths(out_dir, name)
    opts = {**effects.DEFAULT, **(opts or {})}
    render = ov.render if overlay_list else effects.render
    kw = {"overlays": overlay_list} if overlay_list else {}
    with tempfile.TemporaryDirectory() as d:
        finish(lambda out, extra: render(src, start, end, out, opts, extra=extra, **kw), mov, d, repeat, interpolate)
    length = (end if end is not None else effects.thumbs.video_duration(str(src))) - start
    key = effects.out_time(key_time or 0.0, length, opts)
    f = interp.stretch(interpolate)
    still(mov, min(max(0.0, key * f), effects.out_length(length, opts) * f - 0.05), jpg)
    return jpg, mov


def finish(render, mov, d, repeat=1, interpolate=None, tags=None):
    """render(out, extra) one pass, interpolate it, repeat it → mov tagged as a live MOV. d: scratch dir."""
    tags = live_tags() if tags is None else tags
    on = interp.enabled(interpolate)
    once = mov if repeat <= 1 else Path(d, "once" + Path(mov).suffix)
    raw = Path(d, "raw" + Path(mov).suffix) if on else once
    render(raw, tags if raw == mov else ())
    if on:
        interp.apply(raw, once, interpolate, tags if once == mov else ())
    if repeat > 1:
        effects.thumbs.repeat(once, mov, repeat, tags)
    return mov


def out_paths(out_dir=None, name=None):
    out_dir = Path(out_dir or library_dir())
    out_dir.mkdir(parents=True, exist_ok=True)
    name = name or time.strftime("LLP_%Y%m%d_%H%M%S")
    return out_dir / f"{name}.jpg", out_dir / f"{name}.mov"


def live_tags():
    """ffmpeg output args that tag a MOV as the motion half of a live image."""
    return ["-movflags", "use_metadata_tags", "-metadata",
            f"com.apple.quicktime.content.identifier={str(uuid.uuid4()).upper()}",
            "-metadata", "com.apple.quicktime.software=LinuxLivePaper"]


def still(mov, t, jpg):
    subprocess.run([*thumbs.NICE, "ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(mov),
                    "-frames:v", "1", "-q:v", "2", str(jpg)], check=True)


if __name__ == "__main__":
    from .library import scan
    with tempfile.TemporaryDirectory() as d:
        src = Path(d, "src.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:d=4", str(src)], check=True)
        out = Path(d, "out")
        jpg, mov = make_live(src, 1.0, 3.0, 0.5, out, "t")
        assert jpg.stat().st_size and mov.stat().st_size
        assert [i.kind for i in scan(out)] == ["live"]
        tags = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", str(mov)],
                              capture_output=True, text=True).stdout
        assert "content.identifier" in tags, tags
        gif = Path(d, "g.gif")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=99x75:d=2", str(gif)], check=True)
        jpg, mov = make_live(gif, 0, 1.5, 0.2, out, "g")
        assert mov.stat().st_size and jpg.stat().st_size
        jpg, mov = make_live(src, 0, 2.0, 0.5, out, "o", overlay_list=[ov.text("hi", size=0.2)])
        assert mov.stat().st_size and jpg.stat().st_size
        jpg, mov = make_live(src, 0.5, 2.5, 1.9, out, "r", {"direction": "reverse", "look": "mono"})
        assert jpg.stat().st_size and 1.9 < effects.thumbs.video_duration(str(mov)) < 2.1
        jpg, mov = make_live(src, 0.5, 2.5, 1.0, out, "x", repeat=3)
        assert 5.8 < effects.thumbs.video_duration(str(mov)) < 6.3 and "content.identifier" in subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags", str(mov)], capture_output=True, text=True).stdout
        from . import interp  # 2x smoother then repeated twice (only when PyTorch + a RIFE model are set up)
        sm = {**interp.DEFAULTS, "engine": "rife", "factor": 2}
        if interp.python(sm) and interp.installed(sm):
            jpg, mov = make_live(src, 0.5, 2.5, 1.0, out, "i", repeat=2, interpolate=sm)
            assert 3.8 < effects.thumbs.video_duration(str(mov)) < 4.3 and jpg.stat().st_size
            assert "content.identifier" in subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags",
                                                           str(mov)], capture_output=True, text=True).stdout
            jpg, mov = make_live(src, 0.5, 2.5, 1.0, out, "s", interpolate={**sm, "mode": "slow"})
            assert 3.8 < effects.thumbs.video_duration(str(mov)) < 4.3
    print("livemake ok")
