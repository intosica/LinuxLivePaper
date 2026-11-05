"""Small widgets shared by the gallery, the editor and the collage maker."""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

import os  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from pathlib import Path  # noqa: E402

from . import library, thumbs  # noqa: E402

NEW = "New Collection…"


_thumbs = ThreadPoolExecutor(max_workers=2)


class FileBrowser(Gtk.Box):
    """Slide-out folder browser for the collage maker: thumbnails of the videos, GIFs and live images in a folder.
    Click a tile → on_pick(clip path); drag a tile onto a cell (it carries a Gdk.FileList, like a file manager)."""

    def __init__(self, folder, on_pick):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=280, hexpand=False,
                         css_classes=["file-browser"])
        self.on_pick, self.folder, self.gen = on_pick, None, 0
        bar = Gtk.Box(spacing=4, margin_start=6, margin_end=6, margin_top=6)
        self.up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Parent folder", css_classes=["flat"])
        self.up.connect("clicked", lambda *_: self.load(self.folder.parent))
        self.title = Gtk.Label(hexpand=True, xalign=0, ellipsize=3, css_classes=["heading"])  # 3 = END
        other = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Open another folder…", css_classes=["flat"])
        other.connect("clicked", self._other)
        for w in (self.up, self.title, other):
            bar.append(w)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=2,
                                min_children_per_line=2, homogeneous=True, column_spacing=6, row_spacing=6,
                                valign=Gtk.Align.START, margin_start=6, margin_end=6, margin_bottom=6)
        self.flow.connect("child-activated", lambda _f, ch: ch.activate_fn())
        self.append(bar)
        self.append(Gtk.Label(label="Click to fill the selected cell · drag onto any cell", wrap=True,
                              css_classes=["dim-label", "caption"], margin_start=6, margin_end=6))
        self.append(Gtk.ScrolledWindow(child=self.flow, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.load(folder)

    def _other(self, _b):
        def picked(d, res):
            try:
                self.load(d.select_folder_finish(res).get_path())
            except GLib.Error:
                pass
        Gtk.FileDialog(title="Browse folder", initial_folder=Gio.File.new_for_path(str(self.folder))) \
            .select_folder(self.get_root(), None, picked)

    def load(self, folder):
        self.folder = Path(folder)
        self.gen += 1
        self.title.set_label(self.folder.name or str(self.folder))
        self.title.set_tooltip_text(str(self.folder))
        self.up.set_sensitive(self.folder.parent != self.folder)
        self.flow.remove_all()
        try:
            dirs = sorted((e for e in os.scandir(self.folder) if e.is_dir() and not e.name.startswith(".")),
                          key=lambda e: e.name.lower())
        except OSError:
            dirs = []
        for e in dirs:
            self._tile(e.name, Gtk.Image(icon_name="folder-symbolic", pixel_size=48), lambda p=e.path: self.load(p))
        for it in sorted(library.scan_dir(self.folder), key=lambda i: i.name.lower()):
            pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, height_request=110, can_shrink=True)
            tile = self._tile(it.name, pic, lambda m=it.motion: self.on_pick(m), it.motion)
            _thumbs.submit(self._thumb, it, pic, tile, self.gen)

    def _tile(self, name, pic, fn, drag_path=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, tooltip_text=name)
        box.append(pic)
        box.append(Gtk.Label(label=name, ellipsize=2, css_classes=["caption"]))  # 2 = MIDDLE
        child = Gtk.FlowBoxChild(child=box, css_classes=["card"])
        child.activate_fn = fn
        if drag_path:
            src = Gtk.DragSource(actions=Gdk.DragAction.COPY)
            src.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(
                Gdk.FileList.new_from_list([Gio.File.new_for_path(drag_path)])))
            src.connect("drag-begin", lambda s, _d: s.set_icon(Gtk.WidgetPaintable.new(pic), 20, 20))
            child.add_controller(src)
        self.flow.append(child)
        return child

    def _thumb(self, item, pic, tile, gen):
        path = thumbs.make_thumb(item)
        if path and gen == self.gen:
            GLib.idle_add(lambda: gen == self.gen and pic.set_filename(path) and False)


ZOOM_MAX, ZOOM_STEP = 20.0, 1.25  # ×20 of the fitted size; one wheel notch / button press = ×1.25


def zoom_about(z, c, nz, p, size, base):
    """One axis of a cursor-anchored zoom. c: image coordinate (0..1) shown at the widget centre at zoom z; returns
    the new centre at zoom nz so the image point under pointer p (widget px) stays under it. size/base: widget and
    fitted image length in px."""
    u = (p - (size / 2 - c * base * z)) / (base * z)  # image point under the pointer
    return u + (size / 2 - p) / (base * nz)


def clamp_center(z, c, size, base):
    """Keep the zoomed image covering the widget along one axis (centred while it is smaller than the widget)."""
    half = size / (2 * base * z)
    return 0.5 if half >= 0.5 else min(max(c, half), 1 - half)


class ZoomPicture(Gtk.Widget):
    """A picture that zooms: the scroll wheel zooms about the pointer, dragging pans while zoomed, zoom_by/reset for
    buttons and keys. The child Gtk.Picture (self.pic; GifPlay animates it) is allocated at its fitted size and moved
    with a transform, so the full-resolution texture is sampled at every zoom level."""

    def __init__(self, path, on_zoom=None):
        super().__init__(overflow=Gtk.Overflow.HIDDEN, hexpand=True, vexpand=True)
        self.pic = Gtk.Picture.new_for_filename(path)
        self.pic.set_content_fit(Gtk.ContentFit.FILL)  # we allocate it at the image's own aspect ratio
        self.pic.set_parent(self)
        self.z, self.c, self.ptr, self.on_zoom = 1.0, [0.5, 0.5], None, on_zoom or (lambda _z: None)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", lambda _c, _dx, dy: self.zoom_by(ZOOM_STEP ** -dy, self.ptr) or True)
        self.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: setattr(self, "ptr", (x, y)))
        motion.connect("leave", lambda *_: setattr(self, "ptr", None))
        self.add_controller(motion)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda *_: setattr(self, "_from", list(self.c)))
        drag.connect("drag-update", self._pan)
        self.add_controller(drag)

    def do_dispose(self):
        if self.pic.get_parent() is self:
            self.pic.unparent()
        Gtk.Widget.do_dispose(self)

    def do_measure(self, _orientation, _for_size):
        return 0, 0, -1, -1  # fills whatever the viewer gives it

    def _base(self):
        w, h = self.get_width(), self.get_height()
        p = self.pic.get_paintable()
        iw, ih = (p.get_intrinsic_width(), p.get_intrinsic_height()) if p else (0, 0)
        if iw <= 0 or ih <= 0 or w <= 0 or h <= 0:
            return w, h, max(w, 1), max(h, 1)
        s = min(w / iw, h / ih)
        return w, h, iw * s, ih * s

    def _clamp(self):
        w, h, bw, bh = self._base()
        self.c = [clamp_center(self.z, self.c[0], w, bw), clamp_center(self.z, self.c[1], h, bh)]
        return w, h, bw, bh

    def do_size_allocate(self, _w, _h, _baseline):
        w, h, bw, bh = self._clamp()
        t = Gsk.Transform.new().translate(Graphene.Point().init(w / 2 - self.c[0] * bw * self.z,
                                                                h / 2 - self.c[1] * bh * self.z)).scale(self.z, self.z)
        self.pic.allocate(max(1, round(bw)), max(1, round(bh)), -1, t)

    def zoom_by(self, factor, at=None):
        """Zoom by factor about `at` (widget px; default the centre), between fit (1) and ZOOM_MAX."""
        nz = min(max(self.z * factor, 1.0), ZOOM_MAX)
        if nz == self.z:
            return
        w, h, bw, bh = self._base()
        px, py = at or (w / 2, h / 2)
        self.c = [zoom_about(self.z, self.c[0], nz, px, w, bw), zoom_about(self.z, self.c[1], nz, py, h, bh)]
        self.z = nz
        self._changed()

    def reset(self):
        self.z, self.c = 1.0, [0.5, 0.5]
        self._changed()

    def _changed(self):
        self._clamp()
        self.set_cursor_from_name("grab" if self.z > 1 else None)
        self.queue_allocate()
        self.on_zoom(self.z)

    def _pan(self, _g, dx, dy):
        if self.z > 1:
            _w, _h, bw, bh = self._base()
            self.c = [self._from[0] - dx / (bw * self.z), self._from[1] - dy / (bh * self.z)]
            self._clamp()
            self.queue_allocate()


class HoldButton(Gtk.Box):
    """Press-and-hold button: on_press while held, on_release when let go.

    A plain box, not a Gtk.Button — the button's own gesture claims the click sequence, which made the old
    Gtk.Button version fire only sometimes."""

    def __init__(self, label="Try it", icon_name="media-playback-start-symbolic", tooltip_text=None,
                 on_press=None, on_release=None):
        super().__init__(spacing=6, css_classes=["hold"], tooltip_text=tooltip_text, valign=Gtk.Align.CENTER)
        self.press_fn, self.release_fn, self.down = on_press, on_release, False
        self.icon = Gtk.Image(icon_name=icon_name)
        self.text = Gtk.Label(label=label)
        self.append(self.icon)
        self.append(self.text)
        # only "released" ends the hold: GtkGestureClick also emits "stopped" the moment the press is
        # claimed, which ended every hold instantly and made the preview flicker
        click = Gtk.GestureClick()
        click.connect("pressed", lambda _g, n, _x, _y: self._pressed() if n == 1 else None)
        click.connect("released", lambda *_a: self._released())
        self.add_controller(click)

    def _pressed(self):
        if self.down:
            return
        self.down = True
        self.add_css_class("holding")
        if self.press_fn:
            self.press_fn()

    def _released(self):
        if not self.down:
            return
        self.down = False
        self.remove_css_class("holding")
        if self.release_fn:
            self.release_fn()


class CollectionRow(Adw.ComboRow):
    """'Save to collection' picker: None, every collection, and New Collection… (prompts for a name)."""

    def __init__(self, title="Save to collection", select=None):
        super().__init__(title=title, subtitle="Also add it to a collection")
        self._fill(select)
        self.connect("notify::selected", self._picked)

    def _fill(self, select=None):
        self.names = [c.name for c in library.collections()]
        self._busy = True
        self.set_model(Gtk.StringList.new(["None", *self.names, NEW]))
        self.set_selected(self.names.index(select) + 1 if select in self.names else 0)
        self._busy = False

    def _picked(self, *_):
        if self._busy or self.get_selected() != len(self.names) + 1:
            return
        self.set_selected(0)
        collection_dialog(self.get_root(), lambda c: self._fill(c.name))

    def collection(self):
        i = self.get_selected()
        return library.get_collection(self.names[i - 1]) if 1 <= i <= len(self.names) else None


def preview_quality_dropdown():
    """Header control: how big previews are rendered (smaller = faster). Saved files always use full quality."""
    labels = [f"Preview: {n}" for n, _ in thumbs.PREVIEW_HEIGHTS]
    dd = Gtk.DropDown(model=Gtk.StringList.new(labels), valign=Gtk.Align.CENTER, tooltip_text=(
        "Preview size: smaller previews render and play faster. Takes effect the next time you press Play. "
        "Saving always uses full quality."))
    cur = thumbs.preview_height() or 0
    dd.set_selected(next((i for i, (_, h) in enumerate(thumbs.PREVIEW_HEIGHTS) if h == cur), 2))
    dd.connect("notify::selected", lambda w, _p: library.set_pref("preview_height",
                                                                   thumbs.PREVIEW_HEIGHTS[w.get_selected()][1]))
    return dd


def folder_icon(size):
    """The desktop's own folder icon (follows the user's icon theme)."""
    return Gtk.Image(icon_name="folder", pixel_size=size)


def _texture(pb, size):
    """Texture for a collection icon, pre-scaled (CPU, high quality) to at most 2× the shown size: a big image
    squeezed by the GPU on every zoom step left stray bright pixels at its edges."""
    w, h = pb.get_width(), pb.get_height()
    k = min(1.0, 2 * size / max(w, h))
    if k < 1.0:
        pb = pb.scale_simple(max(1, round(w * k)), max(1, round(h * k)), GdkPixbuf.InterpType.HYPER)
    return Gdk.Texture.new_for_pixbuf(pb)


def icon_widget(icon, size):
    """Collection icon: PNG/JPG/WebP/GIF, animated ones play while on screen; no/bad file -> the folder icon."""
    if not icon:
        return folder_icon(size)
    pic = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
    # The picture is an overlay on an empty size_request box: a Picture's natural size is the image's own pixel size,
    # which would otherwise override `size` (collection zoom did nothing for real, big icon files).
    holder = Gtk.Overlay(child=Gtk.Box(width_request=size, height_request=size), halign=Gtk.Align.CENTER,
                         overflow=Gtk.Overflow.HIDDEN)
    holder.add_overlay(pic)
    try:
        anim = GdkPixbuf.PixbufAnimation.new_from_file(str(icon))
    except GLib.Error:  # GdkPixbuf chokes on some real-world GIFs; ffmpeg can still give the first frame
        pb = thumbs.grab_frame(str(icon), 0, size * 2)
        if pb is None:
            return folder_icon(size)
        pic.set_paintable(_texture(pb, size))
        return holder
    it = anim.get_iter(None)
    pic.set_paintable(_texture(it.get_pixbuf(), size))
    if not anim.is_static_image():
        tick = [0]

        def step():
            it.advance(None)
            pic.set_paintable(_texture(it.get_pixbuf(), size))
            tick[0] = GLib.timeout_add(max(20, it.get_delay_time()), step)
            return False

        def stop(*_):
            if tick[0]:
                GLib.source_remove(tick[0])
                tick[0] = 0

        def start(*_):
            stop()
            tick[0] = GLib.timeout_add(max(20, it.get_delay_time()), step)
        pic.connect("map", start)
        pic.connect("unmap", stop)
    return holder


def pick_icon(win, on_pick):
    """Image file chooser (PNG, JPG, WebP, GIF); on_pick(path). `win` must be the app window (see collection_dialog)."""
    imgs = Gtk.FileFilter(name="Images (PNG, JPG, WebP, GIF)")
    for m in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        imgs.add_mime_type(m)
    filters = Gio.ListStore(item_type=Gtk.FileFilter)
    filters.append(imgs)
    dlg = Gtk.FileDialog(title="Choose an icon (PNG, JPG, WebP or GIF)", filters=filters)

    def done(d, res):
        try:
            on_pick(d.open_finish(res).get_path())
        except GLib.Error:
            pass
    dlg.open(win, None, done)


def collection_dialog(parent, on_done, collection=None):
    """Create or edit a collection: name + optional icon (PNG, JPG, WebP or GIF, animated or not) (falls back to the system folder icon).
    on_done(collection) gets the created/updated library.Collection."""
    state = {"icon": collection.icon if collection else None, "changed": False}
    holder = Gtk.Box(halign=Gtk.Align.CENTER, height_request=112)
    entry = Gtk.Entry(text=collection.name if collection else "", activates_default=True, placeholder_text="Name")
    err = Gtk.Label(css_classes=["error", "caption"], visible=False, wrap=True)
    choose = Gtk.Button(label="Choose Icon…")
    clear = Gtk.Button(label="Use Folder Icon", css_classes=["flat"])
    buttons = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
    buttons.append(choose)
    buttons.append(clear)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    for w in (holder, buttons, entry, err):
        box.append(w)

    def show_icon():
        if holder.get_first_child():
            holder.remove(holder.get_first_child())
        holder.append(icon_widget(state["icon"], 112))
        clear.set_visible(bool(state["icon"]))
    show_icon()

    def picked(path):
        state["icon"], state["changed"] = path, True
        show_icon()
    # the alert dialog is already the root of its own tree, so dlg.get_root() parents the picker to a modal dialog
    # and the file chooser stays behind it; the app window is the parent the portal wants.
    win = parent if isinstance(parent, Gtk.Window) else parent.get_root()
    choose.connect("clicked", lambda *_: pick_icon(win, picked))
    clear.connect("clicked", lambda *_: (state.update(icon=None, changed=True), show_icon()))

    dlg = Adw.AlertDialog(heading="Edit Collection" if collection else "New Collection", extra_child=box,
                          default_response="ok", close_response="cancel")
    dlg.add_response("cancel", "Cancel")
    dlg.add_response("ok", "Save" if collection else "Create")
    dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dlg.set_response_enabled("ok", bool(entry.get_text().strip()))
    dlg.set_close_response("cancel")
    entry.connect("changed", lambda e: dlg.set_response_enabled("ok", bool(e.get_text().strip())))

    def respond(d, resp):
        if resp != "ok":
            return
        try:
            if collection:
                c = library.rename_collection(collection, entry.get_text())
                if state["changed"]:
                    library.set_collection_icon(c, state["icon"])
            else:
                c = library.create_collection(entry.get_text(), state["icon"])
        except (ValueError, OSError) as ex:
            err.set_label(str(ex))
            err.set_visible(True)
            d.present(parent)  # keep it open with the message
            return
        on_done(c)
    dlg.connect("response", respond)
    dlg.present(parent)
    entry.grab_focus()
    return dlg


if __name__ == "__main__":
    # cursor-anchored zoom: the image point under the pointer stays put; zooming back out re-centres
    size, base = 1000, 500  # widget 1000 px, fitted image 500 px (letterboxed)
    def screen(z, c, u):  # widget px of image point u
        return size / 2 - c * base * z + u * base * z
    c = zoom_about(1, 0.5, 4, 600, size, base)
    u = (600 - screen(1, 0.5, 0)) / base
    assert abs(screen(4, c, u) - 600) < 1e-9
    assert clamp_center(1, 0.9, size, base) == 0.5  # smaller than the widget: centred
    assert clamp_center(4, 0.0, size, base) == 0.25 and clamp_center(4, 1.0, size, base) == 0.75
    assert abs(zoom_about(2, 0.3, 2, 123, size, base) - 0.3) < 1e-9  # no zoom change, no movement
    print("widgets ok")
