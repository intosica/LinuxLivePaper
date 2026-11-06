"""Library folder config + scan. The gallery shows the LinuxLivePaper library folder, collections included.
Items: *live* (still + video sharing a stem in one folder, Apple's IMG_1.JPG + IMG_1.MOV convention),
plain *image*, plain *video*, and *gif*."""
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".vob", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts",
             ".wmv", ".asf"}  # DVD/MPEG-PS/TS and ASF/WMV too
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
    kind: str            # "live" | "image" | "video" | "gif"
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
        if stills and videos:  # pair the first of each; the leftovers stay standalone
            s, v = stills.pop(0), videos.pop(0)
            items.append(Item("live", s[1], v[1], max(s[2], v[2])))
        items += [Item("image", p, p, m) for _, p, m in stills]
        items += [Item("video", p, p, m) for _, p, m in videos]
        items += [Item("gif", p, p, m) for ext, p, m in files if ext in GIF_EXT]
    return items


def item_for(path):
    """The Item the gallery would show for one file (a live image when its still/motion pair is on disk), so a
    menu opened from somewhere else (a collage cell, a file browser tile) offers the same actions."""
    p = Path(path)
    for it in scan_dir(p.parent):
        if os.path.abspath(it.motion) == os.path.abspath(p) or os.path.abspath(it.path) == os.path.abspath(p):
            return it
    ext = p.suffix.lower()
    kind = "gif" if ext in GIF_EXT else "image" if ext in IMAGE_EXT else "video"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    return Item(kind, str(p), str(p), mtime)


def scan(root=None):
    """Scan, oldest first (the gallery shows newest at the bottom, like iOS Photos). Subfolders are read when the
    user wants them (`scan_subfolders` preference); the collection/ folders are always read, so All can show the
    collected items too (Preferences decides whether it does, and the tab kind filters them). Legacy hard-linked
    duplicates (same file in the root and in a collection) show once, as the collection's copy."""
    root = str(root or library_dir())
    subfolders = load_config().get("scan_subfolders", True)
    best = {}
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel = Path(dirpath).relative_to(root).parts
        if not subfolders:  # root + collection/<Name>, nothing else
            if not rel:
                dirnames[:] = [d for d in dirnames if d == COLLECTIONS]
            elif rel[0] != COLLECTIONS or len(rel) > 1:
                dirnames[:] = []
        for it in scan_dir(dirpath):
            try:
                st = os.stat(it.motion)
                key = (st.st_dev, st.st_ino)
            except OSError:
                key = it.motion
            if key not in best or len(Path(it.path).parts) > len(Path(best[key].path).parts):
                best[key] = it
    return sorted(best.values(), key=lambda i: i.mtime)


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
    """Trash an item's files."""
    for f in item_files(item):
        _trash(f)
    meta = Path(item.path).parent / META
    try:
        data = json.loads(meta.read_text())
        if data.pop(Path(item.path).stem, None) is not None:
            meta.write_text(json.dumps(data, indent=1))
    except (OSError, ValueError):
        pass


# --- collections: <library>/collection/<Name>/ holding the items that belong to them (each item lives in one place) -----------------
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
    """Move the collection's items back to the library root, then trash the (now icon-only) folder."""
    for it in c.items():
        remove_from_collection(it)
    _trash(c.path)


def _move_item(item, folder):
    """Move an item's files (and sidecar meta) into folder. Returns False if it is already there."""
    folder.mkdir(parents=True, exist_ok=True)
    src = [Path(f) for f in item_files(item)]
    if src[0].parent.resolve() == folder.resolve():
        return False
    stem = src[0].stem
    if any((folder / f.name).exists() for f in src):  # a different item with the same name
        n = 2
        while any((folder / f"{stem} {n}{f.suffix}").exists() for f in src):
            n += 1
        stem = f"{stem} {n}"
    meta = get_meta(src[0])
    for f in src:
        shutil.move(f, folder / f"{stem}{f.suffix}")
    if meta:
        set_meta(folder / f"{stem}{src[0].suffix}", **meta)
    old = src[0].parent / META
    try:
        data = json.loads(old.read_text())
        if data.pop(src[0].stem, None) is not None:
            old.write_text(json.dumps(data, indent=1))
    except (OSError, ValueError):
        pass
    return True


def add_to_collection(item, c):
    """Move the item into the collection (one copy on disk). Returns False if it is already there."""
    return _move_item(item, c.path)


def remove_from_collection(item):
    """Move the item back to the library root."""
    return _move_item(item, library_dir())


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        for n in ("IMG_1.JPG", "IMG_1.MOV", "a.png", "b.mp4", "c.gif", ".hidden.mp4", "notes.txt"):
            Path(d, n).touch()
        Path(d, "sub").mkdir()
        Path(d, "sub", "x.webm").touch()
        items = scan(d)
        assert sorted(i.kind for i in items) == ["gif", "image", "live", "video", "video"], items
        assert next(i for i in items if i.kind == "image").motion.endswith("a.png"), "a lone still is an image"
        live = next(i for i in items if i.kind == "live")
        assert live.path.endswith("IMG_1.JPG") and live.motion.endswith("IMG_1.MOV")
        set_meta(live.path, transition="zoom")
        set_meta(Path(d, "IMG_1.MOV"), x=1)
        assert get_meta(live.motion) == {"transition": "zoom", "x": 1} and get_meta(Path(d, "b.mp4")) == {}
        set_meta(Path(d, "b.mp4"), bookmarks=[1.5, 2.25])
        assert get_meta(Path(d, "b.mp4"))["bookmarks"] == [1.5, 2.25], "timeline bookmarks round-trip through the sidecar"
        set_meta(Path(d, "b.mp4"), bookmarks=[])
        assert len(scan(d)) == 5  # sidecar is hidden
        assert motion_for(live.path) == live.motion and motion_for(Path(d, "a.png")) is None
        assert motion_for(Path(d, "c.gif")).endswith("c.gif") and motion_for(Path(d, "notes.txt")) is None
        assert [i.mtime for i in scan(d)] == sorted(i.mtime for i in scan(d))  # oldest first
        assert len(scan(d)) == 5, "subfolders are read by default"
        Path(d, "sub", "deep").mkdir()
        Path(d, "sub", "deep", "y.mp4").touch()
        assert len(scan(d)) == 6
        real = load_config
        load_config = lambda: {"scan_subfolders": False}  # noqa: E731
        try:
            assert sorted(i.name for i in scan(d)) == ["IMG_1", "a", "b", "c"], "subfolders off: the root only"
        finally:
            load_config = real
    with tempfile.TemporaryDirectory() as d:  # collections
        library_dir = lambda: Path(d)  # noqa: E731
        _trash = lambda p: shutil.rmtree(p) if Path(p).is_dir() else Path(p).unlink()  # keep the real Trash clean
        Path(d, "IMG_2.jpg").write_text("still")
        Path(d, "IMG_2.mov").write_text("motion")
        live = scan(d)[0]
        set_meta(live.path, transition="fade")
        c = create_collection("  Trips/2026 ")
        assert c.name == "Trips 2026" and [x.name for x in collections()] == ["Trips 2026"]
        assert add_to_collection(live, c) and not add_to_collection(c.items()[0], c)
        assert not Path(d, "IMG_2.jpg").exists() and not Path(d, "IMG_2.mov").exists(), "moved, not copied"
        assert len(scan(d)) == 1, "All reads the collection subfolders"
        load_config = lambda: {"scan_subfolders": False}  # noqa: E731
        assert len(scan(d)) == 1, "collections are read even with subfolders off"
        [copy] = c.items()
        assert copy.kind == "live" and get_meta(copy.path) == {"transition": "fade"}
        os.link(copy.path, Path(d, "IMG_2.jpg"))  # legacy hard-linked duplicate shows once
        os.link(copy.motion, Path(d, "IMG_2.mov"))
        assert [i.path for i in scan(d)] == [copy.path]
        Path(d, "IMG_2.jpg").unlink(), Path(d, "IMG_2.mov").unlink()
        assert remove_from_collection(copy) and c.items() == [] and get_meta(Path(d, "IMG_2.jpg")) == {"transition": "fade"}
        live = scan(d)[0]
        add_to_collection(live, c)
        [copy] = c.items()
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
