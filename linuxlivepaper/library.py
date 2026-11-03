"""Scan folders into gallery items. A *live* item = still image + video sharing a stem in one folder
(Apple's IMG_0001.JPG + IMG_0001.MOV convention, which is also what our creator writes)."""
import os
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
GIF_EXT = {".gif"}
LIBRARY_DIR = Path.home() / "Pictures" / "LinuxLivePaper"


@dataclass
class Item:
    kind: str            # "live" | "video" | "gif" | "photo"
    path: str            # still for live/photo, the file itself otherwise
    motion: str | None   # video for live/video/gif
    mtime: float

    @property
    def name(self):
        return Path(self.path).stem


def default_roots():
    from gi.repository import GLib
    roots = [LIBRARY_DIR]
    for d in (GLib.UserDirectory.DIRECTORY_PICTURES, GLib.UserDirectory.DIRECTORY_VIDEOS):
        p = GLib.get_user_special_dir(d)
        if p:
            roots.append(Path(p))
    return roots


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
        ext = os.path.splitext(e.name)[1].lower()
        if ext in IMAGE_EXT or ext in VIDEO_EXT or ext in GIF_EXT:
            groups.setdefault(os.path.splitext(e.name)[0].lower(), []).append((ext, e.path, e.stat().st_mtime))
    items = []
    for files in groups.values():
        stills = [f for f in files if f[0] in IMAGE_EXT]
        videos = [f for f in files if f[0] in VIDEO_EXT]
        if stills and videos:  # pair the first of each; leftovers stay standalone
            s, v = stills.pop(0), videos.pop(0)
            items.append(Item("live", s[1], v[1], max(s[2], v[2])))
        items += [Item("photo", p, None, m) for _, p, m in stills]
        items += [Item("video", p, p, m) for _, p, m in videos]
        items += [Item("gif", p, p, m) for ext, p, m in files if ext in GIF_EXT]
    return items


def scan(roots=None):
    """Recursive scan, newest first, de-duplicated across overlapping roots."""
    seen, items = set(), []
    for root in roots or default_roots():
        for dirpath, dirnames, _ in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            real = os.path.realpath(dirpath)
            if real in seen:
                continue
            seen.add(real)
            items += scan_dir(dirpath)
    items.sort(key=lambda i: i.mtime, reverse=True)
    return items


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        for n in ("IMG_1.JPG", "IMG_1.MOV", "a.png", "b.mp4", "c.gif", ".hidden.png", "notes.txt"):
            Path(d, n).touch()
        Path(d, "sub").mkdir()
        Path(d, "sub", "x.heic").touch()
        kinds = sorted(i.kind for i in scan([Path(d)]))
        assert kinds == ["gif", "live", "photo", "photo", "video"], kinds
        live = next(i for i in scan([Path(d)]) if i.kind == "live")
        assert live.path.endswith("IMG_1.JPG") and live.motion.endswith("IMG_1.MOV")
    print("library ok")
