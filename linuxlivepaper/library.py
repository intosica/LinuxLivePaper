"""Library folder config + scan. The gallery shows only the LinuxLivePaper library folder.
Items: *live* (still + video sharing a stem in one folder, Apple's IMG_1.JPG + IMG_1.MOV convention),
plain *video*, and *gif*. Standalone photos are ignored on purpose (not a photo gallery)."""
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".vob", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts"}  # DVD/MPEG-PS/TS too
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


def set_pref(key, value):
    cfg = load_config()
    cfg[key] = value
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


def set_library_dir(path):
    set_pref("library", str(path))


META = ".linuxlivepaper.json"  # per-folder sidecar: {"<stem>": {"transition": "blur", ...}}


def get_meta(path):
    p = Path(path)
    try:
        return json.loads((p.parent / META).read_text()).get(p.stem, {})
    except (OSError, ValueError):
        return {}


def set_meta(path, **kw):
    p = Path(path)
    f = p.parent / META
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault(p.stem, {}).update(kw)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, f)


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
    """Recursive scan, oldest first (the gallery shows newest at the bottom, like iOS Photos).
    The library's collection/ folder is skipped: collections are browsed on their own."""
    root = str(root or library_dir())
    items = []
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not (dirpath == root and d == COLLECTIONS)]
        items += scan_dir(dirpath)
    items.sort(key=lambda i: i.mtime)
    return items


def motion_for(path):
    """The clip to use for a dropped/picked file: videos and GIFs as they are, a live still → its video."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in VIDEO_EXT or ext in GIF_EXT:
        return str(p)
    if ext in IMAGE_EXT:
        return next((i.motion for i in scan_dir(p.parent) if i.kind == "live" and Path(i.path) == p), None)
    return None


def item_files(item):
    return list(dict.fromkeys([item.path, item.motion]))


def _trash(path):
    """Move to the desktop Trash (recoverable); plain delete only where there is no Trash."""
    from gi.repository import Gio, GLib
    try:
        Gio.File.new_for_path(str(path)).trash(None)
    except GLib.Error:
        p = Path(path)
        shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)


def delete_item(item):
    """Trash an item's files. Collection copies are separate links, so they stay."""
    for f in item_files(item):
        _trash(f)
    meta = Path(item.path).parent / META
    try:
        data = json.loads(meta.read_text())
        if data.pop(Path(item.path).stem, None) is not None:
            meta.write_text(json.dumps(data, indent=1))
    except (OSError, ValueError):
        pass


# --- collections: <library>/collection/<Name>/ holding hard links (or copies) of items -----------------
COLLECTIONS = "collection"
ICON_EXT = (".png", ".webp", ".gif", ".jpg", ".jpeg")  # stored as .icon<ext> in the collection folder


@dataclass
class Collection:
    name: str
    path: Path

    @property
    def icon(self):
        return next((p for e in ICON_EXT if (p := self.path / f".icon{e}").exists()), None)

    def items(self):
        return scan(self.path)


def collections_dir():
    return library_dir() / COLLECTIONS


def clean_name(name):
    name = " ".join(name.replace("/", " ").split()).lstrip(".")
    if not name:
        raise ValueError("Give the collection a name")
    return name


def collections():
    root = collections_dir()
    if not root.is_dir():
        return []
    return sorted((Collection(d.name, d) for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")),
                  key=lambda c: c.name.lower())


def get_collection(name):
    return Collection(name, collections_dir() / name)


def create_collection(name, icon=None):
    c = get_collection(clean_name(name))
    if c.path.exists():
        raise ValueError(f"A collection called {c.name} already exists")
    c.path.mkdir(parents=True)
    if icon:
        set_collection_icon(c, icon)
    return c


def set_collection_icon(c, icon):
    for e in ICON_EXT:
        (c.path / f".icon{e}").unlink(missing_ok=True)
    if icon:
        shutil.copyfile(icon, c.path / f".icon{Path(icon).suffix.lower()}")


def rename_collection(c, new):
    new = clean_name(new)
    if new == c.name:
        return c
    dest = collections_dir() / new
    if dest.exists():
        raise ValueError(f"A collection called {new} already exists")
    c.path.rename(dest)
    return Collection(new, dest)


def delete_collection(c):
    """Trash the collection folder. Library items are untouched (the folder holds separate links)."""
    _trash(c.path)


def add_to_collection(item, c):
    """Hard-link (copy if the filesystem can't) the item's files into the collection, so deleting either one
    never removes the other. Returns False if it is already there."""
    c.path.mkdir(parents=True, exist_ok=True)
    src = [Path(f) for f in item_files(item)]
    stem = src[0].stem
    if (c.path / src[0].name).exists():
        if all(os.path.samefile(f, c.path / f.name) for f in src if (c.path / f.name).exists()):
            return False
        n = 2  # different item with the same name
        while any((c.path / f"{stem} {n}{f.suffix}").exists() for f in src):
            n += 1
        stem = f"{stem} {n}"
    for f in src:
        dest = c.path / f"{stem}{f.suffix}"
        try:
            os.link(f, dest)
        except OSError:
            shutil.copy2(f, dest)
    meta = get_meta(src[0])
    if meta:
        set_meta(c.path / f"{stem}{src[0].suffix}", **meta)
    return True


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
        set_meta(live.path, transition="zoom")
        set_meta(Path(d, "IMG_1.MOV"), x=1)
        assert get_meta(live.motion) == {"transition": "zoom", "x": 1} and get_meta(Path(d, "b.mp4")) == {}
        assert len(scan(d)) == 4  # sidecar is hidden
        assert motion_for(live.path) == live.motion and motion_for(Path(d, "a.png")) is None
        assert motion_for(Path(d, "c.gif")).endswith("c.gif") and motion_for(Path(d, "notes.txt")) is None
        assert [i.mtime for i in scan(d)] == sorted(i.mtime for i in scan(d))  # oldest first
    with tempfile.TemporaryDirectory() as d:  # collections
        library_dir = lambda: Path(d)  # noqa: E731
        _trash = lambda p: shutil.rmtree(p) if Path(p).is_dir() else Path(p).unlink()  # keep the real Trash clean
        Path(d, "IMG_2.jpg").write_text("still")
        Path(d, "IMG_2.mov").write_text("motion")
        live = scan(d)[0]
        set_meta(live.path, transition="fade")
        c = create_collection("  Trips/2026 ")
        assert c.name == "Trips 2026" and [x.name for x in collections()] == ["Trips 2026"]
        assert add_to_collection(live, c) and not add_to_collection(live, c)
        assert len(scan(d)) == 1, "collection items must not show up in All"
        [copy] = c.items()
        assert copy.kind == "live" and get_meta(copy.path) == {"transition": "fade"}
        delete_item(live)  # deleting from All keeps the collection copy
        assert scan(d) == [] and Path(copy.motion).read_text() == "motion"
        c = rename_collection(c, "Trips")
        assert [x.name for x in collections()] == ["Trips"] and len(c.items()) == 1
        delete_item(c.items()[0])
        assert c.items() == []
        try:
            create_collection("Trips")
            raise AssertionError("duplicate allowed")
        except ValueError:
            pass
    print("library ok")
