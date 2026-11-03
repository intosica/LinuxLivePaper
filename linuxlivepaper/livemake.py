"""Create a live image (still + motion pair) from a video: the format the gallery pairs on.
Writes Apple-style identifiers too, so iPhone compatibility can be finished later (see docs/ARCHITECTURE.md)."""
import subprocess
import time
import uuid
from pathlib import Path

from .library import LIBRARY_DIR


def make_live(src, start=0.0, end=None, key_time=None, out_dir=LIBRARY_DIR, name=None):
    """Trim src to [start, end] → <name>.mov, and grab the frame at key_time (relative to start) → <name>.jpg."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = name or time.strftime("LLP_%Y%m%d_%H%M%S")
    mov, jpg = out_dir / f"{name}.mov", out_dir / f"{name}.jpg"
    cid = str(uuid.uuid4()).upper()
    trim = ["-ss", f"{start:.3f}"] + (["-to", f"{end:.3f}"] if end is not None else [])
    subprocess.run(["ffmpeg", "-v", "error", "-y", *trim, "-i", str(src),
                    "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                    "-c:a", "aac", "-movflags", "use_metadata_tags",
                    "-metadata", f"com.apple.quicktime.content.identifier={cid}",
                    "-metadata", "com.apple.quicktime.software=LinuxLivePaper", str(mov)], check=True)
    key = key_time if key_time is not None else 0.0
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{key:.3f}", "-i", str(mov),
                    "-frames:v", "1", "-q:v", "2", str(jpg)], check=True)
    return jpg, mov


if __name__ == "__main__":
    import tempfile
    from .library import scan
    with tempfile.TemporaryDirectory() as d:
        src = Path(d, "src.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:d=4", str(src)], check=True)
        out = Path(d, "out")
        jpg, mov = make_live(src, 1.0, 3.0, 0.5, out, "t")
        assert jpg.stat().st_size and mov.stat().st_size
        assert [i.kind for i in scan([out])] == ["live"]
        tags = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", str(mov)],
                              capture_output=True, text=True).stdout
        assert "content.identifier" in tags, tags
    print("livemake ok")
