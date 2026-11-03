"""LinuxLivePaper GTK4/libadwaita app: gallery grid + viewer + quick live-image creator."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import library, livemake, thumbs  # noqa: E402

APP_ID = "io.github.linuxlivepaper"
TILE = 220
FILTERS = [("All", None), ("Live", "live"), ("Videos", "video"), ("Photos", "photo"), ("GIFs", "gif")]
CSS = """
.tile { border-radius: 14px; background: alpha(@view_fg_color, 0.06); }
.tile:hover { box-shadow: 0 0 0 3px @accent_bg_color; }
.badge { background: rgba(0,0,0,0.55); color: white; border-radius: 7px; padding: 2px 7px;
         margin: 8px; font-size: 0.8em; font-weight: 800; letter-spacing: 1px; }
.viewer { background: black; }
.hint { color: alpha(white, 0.7); margin: 16px; }
"""
pool = ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 4) // 2))


def fmt_time(s):
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def badge(text, icon=None):
    box = Gtk.Box(spacing=4, halign=Gtk.Align.START, valign=Gtk.Align.START, css_classes=["badge"])
    if icon:
        box.append(Gtk.Image.new_from_icon_name(icon))
    box.append(Gtk.Label(label=text))
    return box


def play_stream(path, picture, muted=True, loop=True):
    """Start playing path and swap it into picture once the first frame is decoded (no black flash)."""
    m = Gtk.MediaFile.new_for_filename(path)
    m.set_muted(muted)
    m.set_loop(loop)

    def first_frame(stream):
        stream.disconnect(hid)
        if stream.get_playing():
            picture.set_paintable(stream)
    hid = m.connect("invalidate-contents", first_frame)
    m.play()
    return m


class ItemObj(GObject.Object):
    def __init__(self, item):
        super().__init__()
        self.item = item
        self.texture = None
        self.duration = None
        self.loading = False


class Tile(Gtk.Overlay):
    """Fixed-size tile; the picture is an overlay child so its natural size doesn't stretch the row.
    Hovering a live/video/GIF tile plays it muted."""

    def __init__(self):
        super().__init__(css_classes=["tile"], overflow=Gtk.Overflow.HIDDEN, margin_start=4, margin_end=4,
                         margin_top=4, margin_bottom=4)
        self.set_child(Gtk.Box(width_request=TILE, height_request=TILE))
        self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.add_overlay(self.pic)
        self.badge = None
        self.obj = None
        self.stream = None
        self._hover_id = 0
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._schedule_play())
        motion.connect("leave", lambda *_: self.stop())
        self.add_controller(motion)

    def bind(self, obj):
        self.stop()
        self.obj = obj
        self.pic.set_paintable(obj.texture)
        self._set_badge()
        if obj.texture is None and not obj.loading:
            obj.loading = True
            pool.submit(self._load, obj)

    def unbind(self):
        self.stop()
        self.obj = None

    def _set_badge(self):
        if self.badge:
            self.remove_overlay(self.badge)
            self.badge = None
        o = self.obj
        if o.item.kind == "live":
            self.badge = badge("LIVE", "media-playlist-repeat-symbolic")
        elif o.item.kind == "gif":
            self.badge = badge("GIF")
        elif o.item.kind == "video" and o.duration:
            self.badge = badge(fmt_time(o.duration), "media-playback-start-symbolic")
        if self.badge:
            self.add_overlay(self.badge)

    @staticmethod
    def _load_obj(obj):
        path = thumbs.make_thumb(obj.item)
        tex = None
        if path:
            try:
                tex = Gdk.Texture.new_from_filename(path)
            except GLib.Error:
                pass
        dur = thumbs.video_duration(obj.item.path) if obj.item.kind == "video" else None
        return tex, dur

    def _load(self, obj):
        tex, dur = self._load_obj(obj)

        def done():
            obj.texture, obj.duration, obj.loading = tex, dur, False
            if self.obj is obj and self.stream is None:
                self.pic.set_paintable(tex)
                self._set_badge()
        GLib.idle_add(done)

    def _schedule_play(self):
        if self.obj and self.obj.item.motion and not self._hover_id:
            self._hover_id = GLib.timeout_add(250, self._play)

    def _play(self):
        self._hover_id = 0
        if self.obj:
            self.stream = play_stream(self.obj.item.motion, self.pic)
        return False

    def stop(self):
        if self._hover_id:
            GLib.source_remove(self._hover_id)
            self._hover_id = 0
        if self.stream:
            self.stream.set_playing(False)
            self.stream = None
            self.pic.set_paintable(self.obj.texture if self.obj else None)


class LiveView(Gtk.Overlay):
    """Shows the still; press and hold (or click LIVE) to play the motion with sound, like iOS."""

    def __init__(self, item):
        super().__init__()
        self.item = item
        self.still = Gdk.Texture.new_from_filename(item.path)
        self.pic = Gtk.Picture(paintable=self.still, content_fit=Gtk.ContentFit.CONTAIN)
        self.set_child(self.pic)
        self.add_overlay(badge("LIVE", "media-playlist-repeat-symbolic"))
        self.add_overlay(Gtk.Label(label="Press and hold to play", css_classes=["hint"],
                                   halign=Gtk.Align.CENTER, valign=Gtk.Align.END))
        self.stream = None
        press = Gtk.GestureClick()
        press.connect("pressed", lambda *_: self.play())
        press.connect("released", lambda *_: self.stop())
        self.add_controller(press)

    def play(self):
        self.stop()
        self.stream = play_stream(self.item.motion, self.pic, muted=False, loop=False)
        self.stream.connect("notify::ended", lambda s, _: s.get_ended() and self.stop())

    def stop(self):
        if self.stream:
            self.stream.set_playing(False)
            self.stream = None
        self.pic.set_paintable(self.still)


class Viewer(Adw.NavigationPage):
    def __init__(self, objs, index):
        super().__init__(title="")
        self.objs, self.index, self.stream, self.view = objs, index, None, None
        self.header = Adw.HeaderBar()
        self.frame = Gtk.Overlay(css_classes=["viewer"], hexpand=True, vexpand=True)
        self.content = Gtk.Box()
        self.frame.set_child(self.content)
        for icon, step, align in (("go-previous-symbolic", -1, Gtk.Align.START),
                                  ("go-next-symbolic", 1, Gtk.Align.END)):
            b = Gtk.Button(icon_name=icon, halign=align, valign=Gtk.Align.CENTER, margin_start=12,
                           margin_end=12, css_classes=["osd", "circular"])
            b.connect("clicked", lambda _b, s=step: self.go(s))
            self.frame.add_overlay(b)
        tv = Adw.ToolbarView(content=self.frame)
        tv.add_top_bar(self.header)
        self.set_child(tv)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self.connect("hidden", lambda *_: self._stop())
        self.show()

    def _key(self, _c, keyval, *_):
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            self.go(-1 if keyval == Gdk.KEY_Left else 1)
            return True
        if keyval == Gdk.KEY_Escape:
            self.get_ancestor(Adw.NavigationView).pop()
            return True
        if keyval == Gdk.KEY_space and isinstance(self.view, LiveView):
            self.view.play()
            return True
        return False

    def go(self, step):
        self.index = (self.index + step) % len(self.objs)
        self.show()

    def _stop(self):
        if self.stream:
            self.stream.set_playing(False)
            self.stream = None
        if isinstance(self.view, LiveView):
            self.view.stop()
        elif isinstance(self.view, Gtk.Video):
            self.view.get_media_stream() and self.view.get_media_stream().set_playing(False)

    def show(self):
        self._stop()
        item = self.objs[self.index].item
        self.set_title(item.name)
        if item.kind == "live":
            view = LiveView(item)
        elif item.kind == "video":
            view = Gtk.Video.new_for_filename(item.path)
            view.set_autoplay(True)
            view.set_loop(True)
        elif item.kind == "gif":
            view = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN)
            self.stream = play_stream(item.path, view)
        else:
            view = Gtk.Picture.new_for_filename(item.path)
            view.set_content_fit(Gtk.ContentFit.CONTAIN)
        view.set_hexpand(True)
        view.set_vexpand(True)
        self.frame.set_child(view)
        self.view = view
        self.grab_focus()


class CreateDialog(Adw.Dialog):
    """Quick creator: trim a video into a live image. The full editor comes later (docs/ROADMAP.md)."""

    def __init__(self, src, on_done):
        super().__init__(title="New Live Image", content_width=520)
        self.src, self.on_done = src, on_done
        dur = thumbs.video_duration(src) or 3.0
        video = Gtk.Video.new_for_filename(src)
        video.set_autoplay(True)
        video.set_loop(True)
        video.set_size_request(-1, 260)
        self.start = Adw.SpinRow.new_with_range(0, max(0.0, dur - 0.1), 0.1)
        self.start.set_title("Start (s)")
        self.length = Adw.SpinRow.new_with_range(0.5, max(0.5, dur), 0.1)
        self.length.set_title("Length (s)")
        self.length.set_value(min(3.0, dur))
        self.key = Adw.SpinRow.new_with_range(0, max(0.0, dur), 0.1)
        self.key.set_title("Cover frame (s from start)")
        for r in (self.start, self.length, self.key):
            r.set_digits(1)
        group = Adw.PreferencesGroup(title=os.path.basename(src), description=f"Duration {fmt_time(dur)}")
        for r in (self.start, self.length, self.key):
            group.add(r)
        self.button = Gtk.Button(label="Create", css_classes=["suggested-action", "pill"],
                                 halign=Gtk.Align.CENTER, margin_top=6)
        self.button.connect("clicked", self._create)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18, margin_end=18,
                      margin_top=6, margin_bottom=18)
        for w in (video, group, self.button):
            box.append(w)
        tv = Adw.ToolbarView(content=box)
        tv.add_top_bar(Adw.HeaderBar())
        self.set_child(tv)
        self.video = video

    def _create(self, _b):
        self.button.set_sensitive(False)
        self.button.set_label("Creating…")
        s, length, k = self.start.get_value(), self.length.get_value(), self.key.get_value()

        def work():
            try:
                res = livemake.make_live(self.src, s, s + length, min(k, length - 0.05))
                err = None
            except Exception as e:  # surfaced as a toast
                res, err = None, str(e)
            GLib.idle_add(lambda: (self.on_done(res, err), self.close()))
        self.video.get_media_stream() and self.video.get_media_stream().set_playing(False)
        threading.Thread(target=work, daemon=True).start()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="LinuxLivePaper", default_width=1280, default_height=820)
        self.store = Gio.ListStore(item_type=ItemObj)
        self.kind = None
        self.filter = Gtk.CustomFilter.new(lambda o: self.kind is None or o.item.kind == self.kind)
        self.model = Gtk.FilterListModel(model=self.store, filter=self.filter)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, li: li.set_child(Tile()))
        factory.connect("bind", lambda _f, li: li.get_child().bind(li.get_item()))
        factory.connect("unbind", lambda _f, li: li.get_child().unbind())
        self.grid = Gtk.GridView(model=Gtk.NoSelection(model=self.model), factory=factory, max_columns=12,
                                 min_columns=2, single_click_activate=True, margin_start=8, margin_end=8)
        self.grid.connect("activate", self._open)

        self.status = Adw.StatusPage(icon_name="image-x-generic-symbolic", title="Loading your collection…")
        self.stack = Gtk.Stack()
        self.stack.add_named(self.status, "status")
        self.stack.add_named(Gtk.ScrolledWindow(child=self.grid, vexpand=True), "grid")

        header = Adw.HeaderBar()
        switch = Gtk.Box(css_classes=["linked"])
        first = None
        for label, kind in FILTERS:
            b = Gtk.ToggleButton(label=label, group=first)
            first = first or b
            b.connect("toggled", lambda btn, k=kind: btn.get_active() and self._set_kind(k))
            switch.append(b)
        first.set_active(True)
        header.set_title_widget(switch)
        create = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New live image from a video")
        create.connect("clicked", self._pick_video)
        header.pack_start(create)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan")
        refresh.connect("clicked", lambda *_: self.rescan())
        header.pack_end(refresh)

        tv = Adw.ToolbarView(content=self.stack)
        tv.add_top_bar(header)
        self.toasts = Adw.ToastOverlay(child=tv)
        self.nav = Adw.NavigationView()
        self.nav.add(Adw.NavigationPage(child=self.toasts, title="Gallery", tag="gallery"))
        self.set_content(self.nav)
        self.rescan()

    def _set_kind(self, kind):
        self.kind = kind
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_status()

    def _update_status(self):
        if self.model.get_n_items():
            self.stack.set_visible_child_name("grid")
            return
        self.status.set_title("Nothing here yet")
        self.status.set_description("Add photos, videos or GIFs to Pictures/Videos, or press + to create a live image.")
        self.stack.set_visible_child_name("status")

    def rescan(self):
        def work():
            items = library.scan()
            GLib.idle_add(self._populate, items)
        threading.Thread(target=work, daemon=True).start()

    def _populate(self, items):
        self.store.splice(0, self.store.get_n_items(), [ItemObj(i) for i in items])
        self._update_status()

    def _open(self, _grid, pos):
        objs = [self.model.get_item(i) for i in range(self.model.get_n_items())]
        self.nav.push(Viewer(objs, pos))

    def _pick_video(self, *_):
        f = Gtk.FileFilter(name="Videos")
        f.add_mime_type("video/*")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.append(f)
        dlg = Gtk.FileDialog(title="Choose a video", filters=filters)

        def picked(d, res):
            try:
                file = d.open_finish(res)
            except GLib.Error:
                return
            CreateDialog(file.get_path(), self._created).present(self)
        dlg.open(self, None, picked)

    def _created(self, res, err):
        self.toasts.add_toast(Adw.Toast(title=f"Failed: {err}" if err else f"Saved {res[0].stem} to {res[0].parent}"))
        if not err:
            self.rescan()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        (self.props.active_window or Window(self)).present()


def main():
    return App().run(None)
