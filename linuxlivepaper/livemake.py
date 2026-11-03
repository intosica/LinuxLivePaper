"""Create a live image (still + motion pair) from a video: the format the gallery pairs on.
Writes Apple-style identifiers too, so iPhone compatibility can be finished later (see docs/ARCHITECTURE.md)."""
import subprocess
import time
import uuid
from pathlib import Path

from . import effects, overlays as ov
from .library import library_dir


def make_live(src, start=0.0, end=None, key_time=None, out_dir=None, name=None, opts=None, overlay_list=()):
    """Render [start, end] of src (video or GIF) through effects `opts` → <name>.mov, and grab the frame at
    key_time (source seconds after start, mapped through speed/reverse) → <name>.jpg. Defaults to the library folder."""
    jpg, mov = out_paths(out_dir, name)
    opts = {**effects.DEFAULT, **(opts or {})}
    render = ov.render if overlay_list else effects.render
    kw = {"overlays": overlay_list} if overlay_list else {}
    render(src, start, end, mov, opts, extra=live_tags(), **kw)
    length = (end if end is not None else effects.thumbs.video_duration(str(src))) - start
    key = effects.out_time(key_time or 0.0, length, opts)
    still(mov, min(max(0.0, key), effects.out_length(length, opts) - 0.05), jpg)
    return jpg, mov


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
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(mov),
                    "-frames:v", "1", "-q:v", "2", str(jpg)], check=True)


if __name__ == "__main__":
    import tempfile
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
        jpg, mov = make_live(src, 0.5, 2.5, 1.9, out, "r", {"direction": "reverse", "look": "mono"})
        assert jpg.stat().st_size and 1.9 < effects.thumbs.video_duration(str(mov)) < 2.1
    print("livemake ok")
