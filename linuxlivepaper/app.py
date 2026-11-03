"""LinuxLivePaper GTK4/libadwaita app: gallery grid + viewer; the editor lives in editor.py."""
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import library, thumbs  # noqa: E402
from .editor import EditorPage  # noqa: E402

APP_ID = "io.github.linuxlivepaper"
TILE = 220
FILTERS = [("All", None), ("Live", "live"), ("Videos", "video"), ("GIFs", "gif")]
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


def media_filters():
    f = Gtk.FileFilter(name="Videos and GIFs")
    f.add_mime_type("video/*")
    f.add_mime_type("image/gif")
    store = Gio.ListStore(item_type=Gtk.FileFilter)
    store.append(f)
    return store


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


def playable(item, callback):
    """callback(path) on the main thread with something GTK can play: GIFs go through an MP4 proxy."""
    if item.kind != "gif":
        callback(item.motion)
        return

    def work():
        try:
            path = thumbs.mp4_proxy(item.motion)
        except Exception:
            path = None
        GLib.idle_add(lambda: path and callback(path))
    pool.submit(work)


class ItemObj(GObject.Object):
    def __init__(self, item):
        super().__init__()
        self.item = item
        self.texture = None
        self.duration = None
        self.loading = False


class Tile(Gtk.Overlay):
    """Fixed-size tile; the picture is an overlay child so its natural size doesn't stretch the row.
    Hovering plays the item muted."""

    def __init__(self):
        super().__init__(css_classes=["tile"], overflow=Gtk.Overflow.HIDDEN, margin_start=4, margin_end=4,
                         margin_top=4, margin_bottom=4)
        self.set_child(Gtk.Box(width_request=TILE, height_request=TILE))
        self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.add_overlay(self.pic)
        self.badge = None
        self.obj = None
        self.stream = None
        self.hovering = False
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
        elif o.duration:
            self.badge = badge(fmt_time(o.duration), "media-playback-start-symbolic")
        if self.badge:
            self.add_overlay(self.badge)

    def _load(self, obj):
        path = thumbs.make_thumb(obj.item)
        tex = None
        if path:
            try:
                tex = Gdk.Texture.new_from_filename(path)
            except GLib.Error:
                pass
        dur = thumbs.video_duration(obj.item.path) if obj.item.kind == "video" else None

        def done():
            obj.texture, obj.duration, obj.loading = tex, dur, False
            if self.obj is obj and self.stream is None:
                self.pic.set_paintable(tex)
                self._set_badge()
        GLib.idle_add(done)

    def _schedule_play(self):
        self.hovering = True
        if self.obj and not self._hover_id:
            self._hover_id = GLib.timeout_add(250, self._play)

    def _play(self):
        self._hover_id = 0
        obj = self.obj
        if obj:
            playable(obj.item, lambda p: self.hovering and self.obj is obj and self.stream is None
                     and setattr(self, "stream", play_stream(p, self.pic)))
        return False

    def stop(self):
        self.hovering = False
        if self._hover_id:
            GLib.source_remove(self._hover_id)
            self._hover_id = 0
        if self.stream:
            self.stream.set_playing(False)
            self.stream = None
            self.pic.set_paintable(self.obj.texture if self.obj else None)


class LiveView(Gtk.Overlay):
    """Shows the still; press and hold (or Space) to play the motion with sound, like iOS."""

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
    def __init__(self, win, objs, index):
        super().__init__(title="")
        self.win, self.objs, self.index, self.stream, self.view = win, objs, index, None, None
        self.header = Adw.HeaderBar()
        self.make_btn = Gtk.Button(label="Make Live Image", css_classes=["suggested-action"])
        self.make_btn.connect("clicked", lambda *_: win.open_editor(self.objs[self.index].item.motion))
        self.header.pack_end(self.make_btn)
        self.frame = Gtk.Overlay(css_classes=["viewer"], hexpand=True, vexpand=True)
        self.frame.set_child(Gtk.Box())
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
        elif keyval == Gdk.KEY_Escape:
            self.get_ancestor(Adw.NavigationView).pop()
        elif keyval == Gdk.KEY_space and isinstance(self.view, LiveView):
            self.view.play()
        else:
            return False
        return True

    def go(self, step):
        self.index = (self.index + step) % len(self.objs)
        self.show()

    def _stop(self):
        if self.stream:
            self.stream.set_playing(False)
            self.stream = None
        if isinstance(self.view, LiveView):
            self.view.stop()
        elif isinstance(self.view, Gtk.Video) and self.view.get_media_stream():
            self.view.get_media_stream().set_playing(False)

    def show(self):
        self._stop()
        item = self.objs[self.index].item
        self.set_title(item.name)
        self.make_btn.set_visible(item.kind != "live")
        if item.kind == "live":
            view = LiveView(item)
        elif item.kind == "video":
            view = Gtk.Video.new_for_filename(item.path)
            view.set_autoplay(True)
            view.set_loop(True)
        else:  # gif: loop the MP4 proxy
            view = Gtk.Picture.new_for_filename(item.path)  # first frame until the proxy is ready
            view.set_content_fit(Gtk.ContentFit.CONTAIN)

            def ready(path, v=view):
                if self.view is v:
                    self.stream = play_stream(path, v)
            playable(item, ready)
        view.set_hexpand(True)
        view.set_vexpand(True)
        self.frame.set_child(view)
        self.view = view
        self.grab_focus()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="LinuxLivePaper", default_width=1280, default_height=860)
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

        self.status = Adw.StatusPage(icon_name="video-x-generic-symbolic", title="Loading your collection…")
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
        create = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Create a live image from a video or GIF")
        create.connect("clicked", self._pick_source)
        imp = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Import videos and GIFs into the library")
        imp.connect("clicked", self._import)
        header.pack_start(create)
        header.pack_start(imp)
        menu = Gio.Menu()
        menu.append("Library Folder…", "win.prefs")
        menu.append("Open Library Folder", "win.open-folder")
        menu.append("Rescan", "win.rescan")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        for name, fn in (("prefs", self._prefs), ("rescan", self.rescan),
                         ("open-folder", lambda: Gtk.FileLauncher.new(
                             Gio.File.new_for_path(str(library.library_dir()))).launch(self, None, None))):
            act = Gio.SimpleAction(name=name)
            act.connect("activate", lambda *_a, f=fn: f())
            self.add_action(act)

        tv = Adw.ToolbarView(content=self.stack)
        tv.add_top_bar(header)
        self.toasts = Adw.ToastOverlay(child=tv)
        self.nav = Adw.NavigationView()
        self.nav.add(Adw.NavigationPage(child=self.toasts, title="Gallery", tag="gallery"))
        self.set_content(self.nav)
        self.rescan()

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def _set_kind(self, kind):
        self.kind = kind
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_status()

    def _update_status(self):
        if self.model.get_n_items():
            self.stack.set_visible_child_name("grid")
            return
        self.status.set_title("Nothing here yet")
        self.status.set_description(f"Your library is {library.library_dir()}.\n"
                                    "Press + to create a live image, or import videos and GIFs.")
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
        self.nav.push(Viewer(self, objs, pos))

    # --- creating & importing -------------------------------------------------------------------
    def open_editor(self, path):
        self.nav.push(EditorPage(path, self._saved))

    def _saved(self, page, res, err):
        if err:
            page.save_btn.set_sensitive(True)
            page.save_btn.set_label("Save Live Image")
            self.toast(f"Saving failed: {err}")
            return
        self.nav.pop_to_tag("gallery")
        self.toast(f"Saved {res[0].stem}")
        self.rescan()

    def _pick_source(self, *_):
        def picked(d, res):
            try:
                self.open_editor(d.open_finish(res).get_path())
            except GLib.Error:
                pass
        Gtk.FileDialog(title="Choose a video or GIF", filters=media_filters()).open(self, None, picked)

    def _import(self, *_):
        def picked(d, res):
            try:
                files = [f.get_path() for f in d.open_multiple_finish(res)]
            except GLib.Error:
                return
            dest = library.library_dir()

            def work():
                for f in files:
                    shutil.copy2(f, dest)
                GLib.idle_add(lambda: (self.toast(f"Imported {len(files)} file(s)"), self.rescan()))
            threading.Thread(target=work, daemon=True).start()
        Gtk.FileDialog(title="Import videos and GIFs", filters=media_filters()).open_multiple(self, None, picked)

    def _prefs(self):
        dlg = Adw.PreferencesDialog(title="Preferences")
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Library", description="The gallery shows this folder, and new live "
                                     "images are saved here. Existing files are not moved.")
        row = Adw.ActionRow(title="LinuxLivePaper folder", subtitle=str(library.library_dir()))
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER,
                           tooltip_text=f"Reset to {library.DEFAULT_LIBRARY}", css_classes=["flat"])

        def apply(path):
            library.set_library_dir(path)
            row.set_subtitle(str(library.library_dir()))
            self.rescan()

        def chosen(d, res):
            try:
                apply(d.select_folder_finish(res).get_path())
            except GLib.Error:
                pass
        change.connect("clicked", lambda *_: Gtk.FileDialog(title="Choose the LinuxLivePaper folder")
                       .select_folder(self, None, chosen))
        reset.connect("clicked", lambda *_: apply(library.DEFAULT_LIBRARY))
        row.add_suffix(reset)
        row.add_suffix(change)
        group.add(row)
        page.add(group)
        dlg.add(page)
        dlg.present(self)


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
