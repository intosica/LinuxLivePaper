"""Library folder config + scan. The gallery shows only the LinuxLivePaper library folder.
Items: *live* (still + video sharing a stem in one folder, Apple's IMG_1.JPG + IMG_1.MOV convention),
plain *video*, and *gif*. Standalone photos are ignored on purpose (not a photo gallery)."""
import json
import os
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
GIF_EXT = {".gif"}
DEFAULT_LIBRARY = Path.home() / "Videos" / "LinuxLivePaper"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "linuxlivepaper" / "config.json"


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def library_dir():
    d = Path(load_config().get("library", DEFAULT_LIBRARY))
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_library_dir(path):
    cfg = load_config()
    cfg["library"] = str(path)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


@dataclass
class Item:
    kind: str            # "live" | "video" | "gif"
    path: str            # still for live, the file itself otherwise
    motion: str          # the video (or the GIF itself)
    mtime: float

    @property
    def name(self):
        return Path(self.path).stem


def scan_dir(folder):
    """Items directly inside one folder."""
    groups = {}
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    for e in entries:
        if e.name.startswith(".") or not e.is_file():
            continue
        stem, ext = os.path.splitext(e.name)
        ext = ext.lower()
        if ext in IMAGE_EXT or ext in VIDEO_EXT or ext in GIF_EXT:
            groups.setdefault(stem.lower(), []).append((ext, e.path, e.stat().st_mtime))
    items = []
    for files in groups.values():
        stills = [f for f in files if f[0] in IMAGE_EXT]
        videos = [f for f in files if f[0] in VIDEO_EXT]
        if stills and videos:  # pair the first of each; a leftover video stays standalone
            s, v = stills[0], videos.pop(0)
            items.append(Item("live", s[1], v[1], max(s[2], v[2])))
        items += [Item("video", p, p, m) for _, p, m in videos]
        items += [Item("gif", p, p, m) for ext, p, m in files if ext in GIF_EXT]
    return items


def scan(root=None):
    """Recursive scan of the library folder, newest first."""
    items = []
    for dirpath, dirnames, _ in os.walk(root or library_dir()):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        items += scan_dir(dirpath)
    items.sort(key=lambda i: i.mtime, reverse=True)
    return items


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        for n in ("IMG_1.JPG", "IMG_1.MOV", "a.png", "b.mp4", "c.gif", ".hidden.mp4", "notes.txt"):
            Path(d, n).touch()
        Path(d, "sub").mkdir()
        Path(d, "sub", "x.webm").touch()
        items = scan(d)
        assert sorted(i.kind for i in items) == ["gif", "live", "video", "video"], items
        live = next(i for i in items if i.kind == "live")
        assert live.path.endswith("IMG_1.JPG") and live.motion.endswith("IMG_1.MOV")
    print("library ok")
