"""LinuxLivePaper GTK4/libadwaita app: iOS-Photos-style gallery (All · Live · Collections) + viewer.
The editor lives in editor.py, the collage maker in collage_page.py."""
import math
import os
import shutil
import threading
import weakref
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk  # noqa: E402

from . import effects, export, library, thumbs  # noqa: E402
from .collage_page import CollagePage  # noqa: E402
from .editor import EditorPage  # noqa: E402
from .widgets import collection_dialog, icon_widget, pick_icon  # noqa: E402
from .playback import DEFAULT_STYLE, STYLES, GifPlay, LiveCanvas, media_file, play_stream, release  # noqa: E402

APP_ID = "io.github.linuxlivepaper"
TILE_SIZES = [80, 110, 140, 170, 220, 280, 360]  # minimum tile size steps; the grid stretches tiles to fill the width
ICON_SIZES = [40, 56, 72, 96, 128, 160]          # collection card icon steps
TILE_DEFAULT, ICON_DEFAULT = 170, 72


def step_zoom(sizes, cur, d):
    """Next size in `sizes` after cur (d=+1 bigger, -1 smaller); cur needn't be one of them."""
    i = min(range(len(sizes)), key=lambda k: abs(sizes[k] - cur))
    return sizes[max(0, min(len(sizes) - 1, i + d))]


def zoom_box(sizes, get, apply):
    """Linked [−][+] buttons: apply(new_size) is called on a step; buttons grey out at the ends."""
    box = Gtk.Box(css_classes=["linked"])
    btns = []
    for icon, d, tip in (("list-remove-symbolic", -1, "Smaller"), ("list-add-symbolic", 1, "Bigger")):
        b = Gtk.Button(icon_name=icon, tooltip_text=tip)
        b.connect("clicked", lambda _b, d=d: (apply(step_zoom(sizes, get(), d)), sync()))
        box.append(b)
        btns.append(b)

    def sync():
        btns[0].set_sensitive(get() > sizes[0])
        btns[1].set_sensitive(get() < sizes[-1])
    sync()
    box.connect("map", lambda *_: sync())  # the other page may have changed the value
    return box
SELECTED = {}  # path → Item: the multi-selection (shared by every grid)
TILES = weakref.WeakSet()
SELECT_KEYS = {"ctrl": ("Ctrl", Gdk.ModifierType.CONTROL_MASK), "shift": ("Shift", Gdk.ModifierType.SHIFT_MASK),
               "alt": ("Alt", Gdk.ModifierType.ALT_MASK)}
SELECT_SHAPES = {"linear": "Linear — paint what the pointer passes over",
                 "rect": "Rectangle — everything inside the dragged box"}
COLLAGE_LAYOUT = {1: "2h", 2: "2h", 3: "1+2", 4: "4", 5: "6", 6: "6", 7: "9", 8: "9", 9: "9"}  # clips → layout


def select_key_names():
    """(linear key, rectangle key). Same key unless the user gave each style its own; they are kept different."""
    cfg = library.load_config()
    single = cfg.get("select_key", "ctrl")
    single = single if single in SELECT_KEYS else "ctrl"
    if not cfg.get("select_split", False):
        return single, single
    lin = cfg.get("select_key_linear", single)
    lin = lin if lin in SELECT_KEYS else single
    rect = cfg.get("select_key_rect", single)
    if rect not in SELECT_KEYS or rect == lin:  # never one key for both styles
        rect = next(k for k in SELECT_KEYS if k != lin)
    return lin, rect


def select_masks():
    lin, rect = select_key_names()
    return SELECT_KEYS[lin][1], SELECT_KEYS[rect][1]


def select_shape(state=0):
    """Which drag style a press means: the held key decides when the two styles have different keys, else the pref."""
    lin, rect = select_masks()
    if state & rect and not state & lin:
        return "rect"
    if state & lin and not state & rect:
        return "linear"
    shape = library.load_config().get("select_shape", "linear")
    return shape if shape in SELECT_SHAPES else "linear"


def toggle_selected(item):
    if SELECTED.pop(item.path, None) is None:
        SELECTED[item.path] = item
    for t in TILES:
        t.sync_check()


TABS = [("All", None), ("Live", "live"), ("Collections", "collections")]
CSS = """
gridview.photos, gridview.photos > child { padding: 0; margin: 0; border-radius: 0; background: none; }
.tile { background: alpha(@view_fg_color, 0.06); }
.badge { background: rgba(0,0,0,0.45); color: white; border-radius: 6px; padding: 1px 6px;
         margin: 6px; font-size: 0.75em; font-weight: 800; letter-spacing: 1px; }
.badge.end { margin: 6px; }
.check { background: @accent_bg_color; color: white; border-radius: 99px; padding: 3px; margin: 6px; }
.tile.picked { outline: 3px solid @accent_bg_color; outline-offset: -3px; }
flowbox.collections > flowboxchild { padding: 6px; border-radius: 12px; }
.collection-name { font-weight: 700; }
.viewer { background: black; }
.nav-arrow { min-width: 44px; min-height: 44px; }
.browse-handle { min-width: 20px; min-height: 120px; padding: 0 2px; border-radius: 999px; }
.file-browser flowboxchild { padding: 0; border-radius: 10px; }
.file-browser flowboxchild picture { border-radius: 10px 10px 0 0; }
.hold { background: alpha(currentColor, 0.12); border-radius: 999px; padding: 5px 14px; }
.hold:hover { background: alpha(currentColor, 0.22); }
.hold.holding { background: #3584e4; color: white; }
.rubberband { background: alpha(@accent_bg_color, 0.22); border: 1px solid @accent_bg_color; border-radius: 2px; }
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
        self.in_collection = Path(item.path).parent.parent.name == library.COLLECTIONS


class Square(Gtk.Widget):
    """Empty widget that is always as tall as it is wide, so grid tiles are perfect squares at any width."""

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    size = library.load_config().get("grid_zoom", TILE_DEFAULT)  # shared minimum side, set by the zoom buttons

    @classmethod
    def zoom(cls, size):
        cls.size = size
        library.set_pref("grid_zoom", size)

        def walk(w):  # GTK caches each widget's size, so every live Square (all pages) must be told to re-measure
            while w:
                if isinstance(w, Square):
                    w.queue_resize()
                walk(w.get_first_child())
                w = w.get_next_sibling()
        for win in Gtk.Window.list_toplevels():
            walk(win)

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return self.size, self.size, -1, -1
        side = for_size if for_size > 0 else self.size
        return side, side, -1, -1


class Tile(Gtk.Overlay):
    """Square photo-grid tile, 1 px apart from its neighbours (iOS Photos look); the picture is an overlay so
    its natural size never stretches the row. Hovering plays the item muted; right-click opens the item menu."""

    def __init__(self, on_menu):
        super().__init__(css_classes=["tile"], overflow=Gtk.Overflow.HIDDEN, margin_end=1, margin_bottom=1)
        self.set_child(Square())
        self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.add_overlay(self.pic)
        self.badge = None
        self.obj = None
        self.stream = None
        self.hovering = False
        self._hover_id = 0
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._auto() or self._schedule_play())
        motion.connect("leave", lambda *_: self._auto() or self.stop())
        self.add_controller(motion)
        menu = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        menu.connect("pressed", lambda _g, _n, x, y: self.obj and on_menu(self, x, y, self.obj.item))
        self.add_controller(menu)
        self.check = Gtk.Image(icon_name="object-select-symbolic", css_classes=["check"], halign=Gtk.Align.START,
                               valign=Gtk.Align.START, visible=False)
        self.add_overlay(self.check)
        self.mods = 0
        press = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        press.connect("pressed", lambda g, *_: setattr(self, "mods", g.get_current_event_state()))
        self.add_controller(press)
        TILES.add(self)

    def sync_check(self):
        on = bool(self.obj and self.obj.item.path in SELECTED)
        self.check.set_visible(on)
        (self.add_css_class if on else self.remove_css_class)("picked")

    def bind(self, obj):
        self.stop()
        self.obj = obj
        self.pic.set_paintable(obj.texture)
        self._set_badge()
        self.sync_check()
        if self._auto():
            self.hovering = True
            self._play()
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
        if o.item.kind == "gif":
            self.badge = badge("GIF")
        elif o.duration:
            self.badge = badge(fmt_time(o.duration))
            self.badge.set_halign(Gtk.Align.END)
            self.badge.set_valign(Gtk.Align.END)
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

    def _auto(self):
        """GIFs are cheap (no decoder), so with "Hover to play" off they just play all the time."""
        return bool(self.obj and self.obj.item.kind == "gif" and not library.load_config().get("hover_play", True))

    def _schedule_play(self):
        self.hovering = True
        if self.obj and not self._hover_id and library.load_config().get("hover_play", True):
            self._hover_id = GLib.timeout_add(250, self._play)

    def _play(self):
        self._hover_id = 0
        obj = self.obj
        if obj and obj.item.kind == "gif":  # GdkPixbuf, no ffmpeg/GStreamer: the old proxy path is what crashed
            def loaded(anim):
                if anim and self.hovering and self.obj is obj and self.stream is None:
                    self.stream = GifPlay(anim, self.pic)
            pool.submit(lambda: GLib.idle_add(loaded, GifPlay.load(obj.item.motion)))
        elif obj:
            playable(obj.item, lambda p: self.hovering and self.obj is obj and self.stream is None
                     and setattr(self, "stream", play_stream(p, self.pic)))
        return False

    def stop(self):
        self.hovering = False
        if self._hover_id:
            GLib.source_remove(self._hover_id)
            self._hover_id = 0
        if self.stream:
            release(self.stream)
            self.stream = None
            self.pic.set_paintable(self.obj.texture if self.obj else None)


class LiveView(Gtk.Overlay):
    """Shows the still; press and hold (or hold Space) to play the motion with sound, like iOS,
    with the immersive blur transition from playback.LiveCanvas. on_live(bool) fires when playing starts/ends."""

    def __init__(self, item, on_live=None, on_held=None):
        super().__init__()
        self.item, self.on_live = item, on_live or (lambda _live: None)
        self.on_held = on_held or (lambda _held: None)  # True on press, False on release (cursor hides only while held)
        self.canvas = LiveCanvas(Gdk.Texture.new_from_filename(item.path), item.motion,
                                 on_idle=self._idle,
                                 style=library.get_meta(item.path).get("transition", DEFAULT_STYLE))
        self.set_child(self.canvas)
        press = Gtk.GestureClick()
        press.connect("pressed", lambda *_: self.play())
        press.connect("released", lambda *_: self.release())
        press.connect("cancel", lambda *_: self.release())
        self.add_controller(press)

    def _idle(self):
        self.on_live(False)

    def play(self):
        self.on_held(True)
        self.on_live(True)
        self.canvas.press()

    def release(self):
        self.on_held(False)
        self.canvas.release()

    def stop(self):
        self.canvas.stop()


EDGE_PX = 140  # the pointer must be this close to the left/right edge for that arrow to appear
CHROME_IDLE_MS = 5000  # cursor / arrows hide after the mouse rests this long
UNHIDE_PX = 40  # after playing, the pointer must travel this far (total) before the cursor comes back
TOP_PX = 70  # fullscreen: the header only slides in while the pointer is this close to the top edge


def BLANK_CURSOR(_c=[]):
    if not _c:
        tex = Gdk.MemoryTexture.new(1, 1, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(bytes(4)), 4)
        _c.append(Gdk.Cursor.new_from_texture(tex, 0, 0))
    return _c[0]


class Viewer(Adw.NavigationPage):
    """Full-bleed viewer. No on-screen hints; the ←/→ arrows only appear while the pointer is near that side
    edge (EDGE_PX) and never while a live image plays; in fullscreen (F, F11, double-click, or the header
    button) the header bar only appears while the pointer is at the top (TOP_PX) and the cursor hides only while
    the left button is held to play."""

    def __init__(self, win, objs, index):
        super().__init__(title="")
        self.win, self.objs, self.index, self.stream, self.view = win, objs, index, None, None
        self.live, self._idle_id, self._last_xy, self._edge, self._top, self.held = False, 0, None, 0, False, False
        self.hide_cursor, self._travel = False, 0.0
        self.header = Adw.HeaderBar()
        self.make_btn = Gtk.Button(label="Make Live Image", css_classes=["suggested-action"])
        self.make_btn.connect("clicked", lambda *_: win.open_editor(self.objs[self.index].item.motion))
        self.fs_btn = Gtk.Button(action_name="win.fullscreen")
        self.header.pack_end(self.fs_btn)
        self.header.pack_end(self.make_btn)
        self.style_ids = list(STYLES)
        self.style_dd = Gtk.DropDown.new_from_strings([v[0] for v in STYLES.values()])
        self.style_dd.set_tooltip_text("Press effect for this live image")
        self.style_dd.connect("notify::selected", self._style_changed)
        self.header.pack_end(self.style_dd)
        self.menu_open = False  # the style popover is open: keep the header up while the pointer is in it
        pop = self.style_dd.get_last_child()
        while pop and not isinstance(pop, Gtk.Popover):
            pop = pop.get_prev_sibling()
        if pop:
            pop.connect("notify::visible", lambda p, _: (setattr(self, "menu_open", p.get_visible()), self._header()))
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=180)
        self.frame = Gtk.Overlay(css_classes=["viewer"], hexpand=True, vexpand=True, child=self.stack)
        self.arrows = []
        for icon, step, align in (("go-previous-symbolic", -1, Gtk.Align.START),
                                  ("go-next-symbolic", 1, Gtk.Align.END)):
            b = Gtk.Button(icon_name=icon, css_classes=["osd", "circular", "nav-arrow"])
            b.connect("clicked", lambda _b, s=step: self.go(s))
            r = Gtk.Revealer(child=b, transition_type=Gtk.RevealerTransitionType.CROSSFADE, transition_duration=250,
                             reveal_child=True, halign=align, valign=Gtk.Align.CENTER, margin_start=12, margin_end=12)
            self.arrows.append(r)
            self.frame.add_overlay(r)
        edge = Gtk.EventControllerMotion()
        edge.connect("motion", self._edge_moved)
        edge.connect("leave", lambda *_: self._set_edge(0))
        self.frame.add_controller(edge)
        self.tv = Adw.ToolbarView(content=self.frame)
        self.tv.add_top_bar(self.header)
        self.set_child(self.tv)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        keys.connect("key-released", self._key_up)
        self.add_controller(keys)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._moved)
        motion.connect("leave", lambda *_: self._set_top(False))
        self.add_controller(motion)
        dbl = Gtk.GestureClick()
        dbl.connect("pressed", lambda _g, n, *_: n == 2 and win.activate_action("win.fullscreen"))
        self.frame.add_controller(dbl)
        self._fs_id = win.connect("notify::fullscreened", lambda *_: self._fullscreen_changed())
        self.connect("hidden", lambda *_: (self._stop(), setattr(self, "hide_cursor", False), self._cursor()))
        self.connect("destroy", lambda *_: win.disconnect(self._fs_id))
        self._fullscreen_changed()
        self.show()

    # --- chrome (arrows, header, cursor) --------------------------------------------------------
    def _fullscreen_changed(self):
        fs = self.win.is_fullscreen()
        self.fs_btn.set_icon_name("view-restore-symbolic" if fs else "view-fullscreen-symbolic")
        self.fs_btn.set_tooltip_text("Leave fullscreen (F)" if fs else "Fullscreen (F)")
        self.tv.set_extend_content_to_top_edge(fs)  # the picture keeps its size when the header slides away
        if not fs:
            self.hide_cursor = False
        self.show_chrome()

    def show_chrome(self, show=True):
        fs = self.win.is_fullscreen()
        self._arrows(show)
        self._header()
        self._cursor()
        if self._idle_id:
            GLib.source_remove(self._idle_id)
            self._idle_id = 0
        if show:
            self._idle_id = GLib.timeout_add(CHROME_IDLE_MS, self._idle)

    def _cursor(self):
        """Fullscreen: the pointer hides when the left button (or Space) is held to play and returns once the
        pointer has travelled UNHIDE_PX after release. A transparent cursor image, set on the frame and the
        window: works on X11 and Wayland, and survives children that set their own cursor."""
        hide = self.hide_cursor and self.win.is_fullscreen()
        cur = BLANK_CURSOR() if hide else None
        self.frame.set_cursor(cur)
        self.win.set_cursor(cur)
        surface = self.win.get_surface()
        if surface:
            surface.set_cursor(cur)  # GTK 4.22/X11 doesn't re-evaluate widget cursors while a button is down

    def _on_held(self, held):
        self.held = held
        self._travel = 0.0
        if held:
            self.hide_cursor = True
            GLib.timeout_add(60, lambda: (self._cursor(), False)[1])  # re-assert once the grab has settled
        self._cursor()

    def _header(self):
        """Windowed: always there (it holds back/close). Fullscreen: only while the pointer is up top."""
        self.tv.set_reveal_top_bars(not self.win.is_fullscreen() or ((self._top or self.menu_open) and not self.live))

    def _set_top(self, top):
        if top != self._top:
            self._top = top
            self._header()

    def _arrows(self, show=True):
        ok = show and not self.live and len(self.objs) > 1
        for r, side in zip(self.arrows, (-1, 1)):
            r.set_reveal_child(ok and self._edge == side)

    def _edge_moved(self, _c, x, _y):
        w = self.frame.get_width()
        self._set_edge(-1 if x < EDGE_PX else 1 if x > w - EDGE_PX else 0)

    def _set_edge(self, edge):
        if edge != self._edge:
            self._edge = edge
            self._arrows(self._idle_id != 0)

    def _idle(self):
        self._idle_id = 0
        self.show_chrome(False)
        return False

    def _moved(self, _c, x, y):
        self._set_top(y < TOP_PX)
        if self._last_xy and abs(x - self._last_xy[0]) + abs(y - self._last_xy[1]) < 3:
            return  # GTK sends synthetic motion when widgets change under a still pointer
        if self.hide_cursor and self._last_xy:
            if self.held:
                self._cursor()  # GTK applies cursor changes lazily; re-assert while the button is down
            else:
                self._travel += math.hypot(x - self._last_xy[0], y - self._last_xy[1])
                if self._travel >= UNHIDE_PX:
                    self.hide_cursor = False
                    self._cursor()
        self._last_xy = (x, y)
        if not self.live:
            self.show_chrome()

    def _on_live(self, live):
        self.live = live
        self.show_chrome(not live)

    # --- keys -----------------------------------------------------------------------------------
    def _key(self, _c, keyval, *_):
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            self.go(-1 if keyval == Gdk.KEY_Left else 1)
        elif keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.win.activate_action("win.fullscreen")
        elif keyval == Gdk.KEY_Escape:
            if self.win.is_fullscreen():
                self.win.unfullscreen()
            else:
                self.get_ancestor(Adw.NavigationView).pop()
        elif keyval == Gdk.KEY_space and isinstance(self.view, LiveView):
            if not self.view.canvas.is_live():  # ignore key auto-repeat
                self.view.play()
        else:
            return False
        return True

    def _style_changed(self, dd, _):
        if getattr(self, "_syncing", False) or not isinstance(self.view, LiveView):
            return
        style = self.style_ids[dd.get_selected()]
        self.view.canvas.style = style
        library.set_meta(self.view.item.path, transition=style)
        self.grab_focus()

    def _key_up(self, _c, keyval, *_):
        if keyval == Gdk.KEY_space and isinstance(self.view, LiveView):
            self.view.release()

    def go(self, step):
        self.index = (self.index + step) % len(self.objs)
        self.show()

    def _stop(self):
        if self.stream:
            release(self.stream)
            self.stream = None
        if isinstance(self.view, LiveView):
            self.view.stop()
        elif isinstance(self.view, Gtk.Video) and self.view.get_media_stream():
            release(self.view.get_media_stream())

    def show(self):
        self._stop()
        self.live = False
        item = self.objs[self.index].item
        self.set_title(item.name)
        self.make_btn.set_visible(item.kind != "live")
        self.style_dd.set_visible(item.kind == "live")
        if item.kind == "live":
            view = LiveView(item, self._on_live, self._on_held)
        elif item.kind == "video":
            view = Gtk.Video(media_stream=media_file(item.path, muted=False, loop=True), autohide=True)
            view.get_media_stream().play()
        else:  # gif: loop the MP4 proxy
            view = Gtk.Picture.new_for_filename(item.path)  # first frame until the proxy is ready
            view.set_content_fit(Gtk.ContentFit.CONTAIN)

            def ready(anim, v=view):
                if anim and self.view is v:
                    self.stream = GifPlay(anim, v)
            pool.submit(lambda: GLib.idle_add(ready, GifPlay.load(item.motion)))
        if item.kind == "live":
            self._syncing = True
            self.style_dd.set_selected(self.style_ids.index(view.canvas.style)
                                       if view.canvas.style in self.style_ids else 0)
            self._syncing = False
        view.set_hexpand(True)
        view.set_vexpand(True)
        old = self.stack.get_visible_child()
        self.stack.add_child(view)
        self.stack.set_visible_child(view)  # crossfade from the previous item
        if old:
            GLib.timeout_add(self.stack.get_transition_duration() + 60,
                             lambda: (old.get_parent() is self.stack and self.stack.remove(old), False)[1])
        self.view = view
        self.show_chrome()
        self.grab_focus()


class MediaGrid(Gtk.Stack):
    """Square photo grid over a list of library Items, with an empty-state page."""

    def __init__(self, win, empty_title, empty_desc, collection=None):
        super().__init__(vexpand=True, hexpand=True)
        self.win, self.collection = win, collection
        self.store = Gio.ListStore(item_type=ItemObj)
        self.kind = None
        self.show_collected = {}  # tab kind → whether items that live in a collection show there (Preferences)
        self.load_show_prefs()
        self.filter = Gtk.CustomFilter.new(self._keep)
        self.model = Gtk.FilterListModel(model=self.store, filter=self.filter)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, li: li.set_child(Tile(self._menu)))
        factory.connect("bind", lambda _f, li: li.get_child().bind(li.get_item()))
        factory.connect("unbind", lambda _f, li: li.get_child().unbind())
        self.grid = Gtk.GridView(model=Gtk.NoSelection(model=self.model), factory=factory, max_columns=16,
                                 min_columns=3, single_click_activate=True, css_classes=["photos"])
        self.grid.connect("activate", self._activate)
        self.status = Adw.StatusPage(icon_name="camera-photo-symbolic", title=empty_title, description=empty_desc)
        self.add_named(self.status, "status")
        scroll = Gtk.ScrolledWindow(child=self.grid)
        # Rubber band for the rectangle drag style: an overlay child positioned with margins, in viewport coords
        # (the same space the drag gesture reports), so it needs no scroll maths of its own.
        self.band = Gtk.Box(halign=Gtk.Align.START, valign=Gtk.Align.START, can_target=False,
                            css_classes=["rubberband"], visible=False)
        self.canvas = Gtk.Overlay(child=scroll)
        self.canvas.add_overlay(self.band)
        self.add_named(self.canvas, "grid")
        # iOS-style: press and drag across tiles to select (or, starting on a selected one, deselect) them all
        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-begin", self._drag_begin, scroll)
        drag.connect("drag-update", self._drag_update, scroll)
        drag.connect("drag-end", self._drag_end)
        scroll.add_controller(drag)
        self._drag = None

    def load_show_prefs(self):
        cfg = library.load_config()
        self.show_collected = {None: cfg.get("all_shows_collected", True), "live": cfg.get("live_shows_collected", True)}

    def _keep(self, o):
        if self.collection is None and o.in_collection and not self.show_collected.get(self.kind, True):
            return False
        return self.kind is None or o.item.kind == self.kind

    def set_items(self, items):
        # keep already-loaded thumbnails when the same file is still there
        old = {self.store.get_item(i).item.path: self.store.get_item(i) for i in range(self.store.get_n_items())}
        objs = []
        for it in items:
            o = old.get(it.path)
            if o is None or o.item.mtime != it.mtime:
                o = ItemObj(it)
            objs.append(o)
        self.store.splice(0, self.store.get_n_items(), objs)
        self._update()

    def set_kind(self, kind):
        self.kind = kind
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update()

    def _update(self):
        self.set_visible_child_name("grid" if self.model.get_n_items() else "status")

    def _activate(self, _grid, pos):
        tile = self._tile_at(pos)
        held = tile and tile.mods & (select_masks()[0] | select_masks()[1])
        if held or self.win.select_mode:
            toggle_selected(self.model.get_item(pos).item)
            self.win.selection_changed()
        else:
            self._open(pos)

    @staticmethod
    def _tile_under(scroll, x, y):
        w = scroll.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w and not isinstance(w, Tile):
            w = w.get_parent()
        return w if w and w.obj else None

    def _drag_begin(self, g, x, y, scroll):
        self._drag = None
        state = g.get_current_event_state()
        if not (self.win.select_mode or state & (select_masks()[0] | select_masks()[1])):
            return
        t = self._tile_under(scroll, x, y)
        if t:
            self._drag = {"start": t, "last": t, "from": (x, y), "shape": select_shape(state),
                          "paint": t.obj.item.path not in SELECTED, "moved": False, "orig": {}, "hit": set()}

    def _drag_update(self, g, dx, dy, scroll):
        d = self._drag
        if not d:
            return
        if d["shape"] == "rect":
            if not d["moved"] and dx * dx + dy * dy < 16:  # a click, not a drag yet
                return
            d["moved"] = True
            g.set_state(Gtk.EventSequenceState.CLAIMED)
            x, y = d["from"]
            self._box(d, x, y, x + dx, y + dy, scroll)
            return
        ok, sx, sy = g.get_start_point()
        t = self._tile_under(scroll, sx + dx, sy + dy)
        if not t or t is d["last"]:
            return
        if not d["moved"]:  # left the first tile: it's a drag, not a click (stops the click from toggling again)
            d["moved"] = True
            g.set_state(Gtk.EventSequenceState.CLAIMED)
            self._paint(d["start"], d["paint"])
        d["last"] = t
        self._paint(t, d["paint"])
        self.win.selection_changed()

    def _box(self, d, x0, y0, x1, y1, scroll):
        """Rectangle style: select (or deselect) every tile the dragged box touches."""
        self.band.set_margin_start(max(0, int(min(x0, x1))))
        self.band.set_margin_top(max(0, int(min(y0, y1))))
        self.band.set_size_request(max(1, int(abs(x1 - x0))), max(1, int(abs(y1 - y0))))
        self.band.set_visible(True)
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        inside = set()
        for t in list(TILES):
            if not t.obj or not t.is_ancestor(self.grid):  # only this grid's tiles
                continue
            ok, p = t.compute_point(scroll, Graphene.Point())
            if ok and p.x < x1 and p.x + t.get_width() > x0 and p.y < y1 and p.y + t.get_height() > y0:
                inside.add(t)
        for t in d["hit"] - inside:  # left the box: back to how it was before this drag
            if t.obj:  # an unbind (grid relayout) leaves a dead tile behind
                self._paint(t, d["orig"].pop(t.obj.item.path, False))
        for t in inside:
            path = t.obj.item.path
            d["orig"].setdefault(path, path in SELECTED)
            self._paint(t, d["paint"])
        d["hit"] = inside
        self.win.selection_changed()

    def _drag_end(self, *_a):
        self.band.set_visible(False)
        self._drag = None

    @staticmethod
    def _paint(tile, on):
        item = tile.obj.item
        if (item.path in SELECTED) != on:
            toggle_selected(item)

    def _tile_at(self, pos):
        item = self.model.get_item(pos)
        return next((t for t in TILES if t.obj is item), None)

    def _open(self, pos):
        objs = [self.model.get_item(i) for i in range(self.model.get_n_items())]
        self.win.nav.push(Viewer(self.win, objs, pos))

    def _menu(self, tile, x, y, item):
        self.win.item_menu(tile, x, y, item, self.collection)


class CollectionsView(Gtk.Stack):
    """Collection cards: the custom icon (PNG/JPG/WebP/GIF) or the system folder icon, the name and the item count."""

    def __init__(self, win):
        super().__init__(vexpand=True, hexpand=True)
        self.win, self.rows = win, []
        self.icon = library.load_config().get("collections_zoom", ICON_DEFAULT)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, min_children_per_line=2,
                                max_children_per_line=40, column_spacing=6, row_spacing=6, valign=Gtk.Align.START,
                                margin_start=18, margin_end=18, margin_top=18, margin_bottom=18,
                                css_classes=["collections"], activate_on_single_click=True)
        self.flow.connect("child-activated", lambda _f, child: win.open_collection(child.collection))
        new = Gtk.Button(label="New Collection", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        new.connect("clicked", lambda *_: win.new_collection())
        self.status = Adw.StatusPage(icon_name="folder-symbolic", title="No collections yet", child=new,
                                     description="Group your live images into collections. "
                                     "Right-click anything and choose Add to Collection, or create one here.")
        self.add_named(self.status, "status")
        self.add_named(Gtk.ScrolledWindow(child=self.flow), "grid")

    def zoom(self, size):
        self.icon = size
        library.set_pref("collections_zoom", size)
        self.set_collections(self.rows)

    def set_collections(self, rows):
        """rows: [(Collection, item count)]"""
        self.rows = rows
        self.flow.remove_all()
        for c, n in rows:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, width_request=self.icon + 22)
            box.append(icon_widget(c.icon, self.icon))
            box.append(Gtk.Label(label=c.name, css_classes=["collection-name"], ellipsize=3, max_width_chars=max(8, self.icon // 6)))
            box.append(Gtk.Label(label=f"{n} item{'s' if n != 1 else ''}", css_classes=["dim-label", "caption"]))
            child = Gtk.FlowBoxChild(child=box, tooltip_text=c.name)
            child.collection = c
            click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
            click.connect("pressed", lambda _g, _n, x, y, w=child: self.win.collection_menu(w, x, y, w.collection))
            child.add_controller(click)
            self.flow.append(child)
        self.set_visible_child_name("grid" if rows else "status")


def load_collections():
    """Worker-thread part of a collections refresh: item counts."""
    return [(c, len(c.items())) for c in library.collections()]


def grid_zoom():
    return zoom_box(TILE_SIZES, lambda: Square.size, Square.zoom)


class CollectionPage(Adw.NavigationPage):
    def __init__(self, win, collection):
        super().__init__(title=collection.name)
        self.win, self.collection = win, collection
        self.grid = MediaGrid(win, "This collection is empty",
                              "Right-click a live image in All or Live and choose Add to Collection.", collection)
        header = Adw.HeaderBar()
        edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Rename or change the icon")
        edit.connect("clicked", lambda *_: win.edit_collection(self.collection))
        header.pack_end(edit)
        header.pack_start(grid_zoom())
        tv = Adw.ToolbarView(content=self.grid)
        tv.add_top_bar(header)
        self.set_child(tv)
        self.refresh()

    def refresh(self):
        c = self.collection

        def work():
            items = c.items()
            GLib.idle_add(self.grid.set_items, items)
        threading.Thread(target=work, daemon=True).start()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="LinuxLivePaper", default_width=1280, default_height=860)
        self.library_grid = MediaGrid(self, "Nothing here yet", "")
        self.collections_view = CollectionsView(self)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.library_grid, "library")
        self.stack.add_named(self.collections_view, "collections")

        header = Adw.HeaderBar()
        switch = Gtk.Box(css_classes=["linked"])
        first = None
        for label, kind in TABS:
            b = Gtk.ToggleButton(label=label, group=first)
            first = first or b
            b.connect("toggled", lambda btn, k=kind: btn.get_active() and self._set_tab(k))
            switch.append(b)
        header.set_title_widget(switch)
        create_menu = Gio.Menu()
        create_menu.append("Live Image from Video or GIF…", "win.new-live")
        create_menu.append("Live Collage…", "win.new-collage")
        create_menu.append("Collection…", "win.new-collection")
        header.pack_start(Gtk.MenuButton(icon_name="list-add-symbolic", menu_model=create_menu,
                                         tooltip_text="Create"))
        imp = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Import videos and GIFs into the library")
        imp.connect("clicked", self._import)
        header.pack_start(imp)
        self.zoom_grid, self.zoom_coll = grid_zoom(), zoom_box(ICON_SIZES, lambda: self.collections_view.icon,
                                                                self.collections_view.zoom)
        header.pack_start(self.zoom_grid)
        header.pack_start(self.zoom_coll)
        self.select_mode = False
        self.select_btn = Gtk.ToggleButton(icon_name="object-select-symbolic", tooltip_text="Select several videos")
        self.select_btn.connect("toggled", lambda b: self.set_select_mode(b.get_active()))
        header.pack_start(self.select_btn)
        menu = Gio.Menu()
        menu.append("Preferences…", "win.prefs")
        menu.append("Open Library Folder", "win.open-folder")
        menu.append("Rescan", "win.rescan")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        self.fs_btn = Gtk.Button(action_name="win.fullscreen")
        header.pack_end(self.fs_btn)
        for name, fn in (("fullscreen", lambda: self.unfullscreen() if self.is_fullscreen() else self.fullscreen()),
                         ("prefs", self._prefs), ("rescan", self.rescan), ("new-live", self._pick_source),
                         ("new-collage", lambda: self.nav.push(CollagePage(self._saved))),
                         ("new-collection", self.new_collection),
                         ("open-folder", lambda: Gtk.FileLauncher.new(
                             Gio.File.new_for_path(str(library.library_dir()))).launch(self, None, None)),
                         ("open-edit", lambda: self.open_editor(self.ctx_item.motion)),
                         ("open-collage1", lambda: self.open_collage(1)),
                         ("open-collage4", lambda: self.open_collage(4)),
                         ("export-gif", lambda: self._export_ctx("gif")),
                         ("export-webp", lambda: self._export_ctx("webp")),
                         ("sel-collage", self._sel_collage), ("sel-delete", self._sel_delete),
                         ("sel-clear", self.clear_selection),
                         ("sel-add-new", lambda: collection_dialog(self, self._sel_add_to)),
                         ("delete", self._delete_ctx), ("remove-from", self._remove_from), ("add-to-new", self._add_to_new),
                         ("edit-collection", lambda: self.edit_collection(self.ctx_collection)),
                         ("rename-collection", lambda: self.rename_collection(self.ctx_collection)),
                         ("change-icon", lambda: self.change_icon(self.ctx_collection)),
                         ("delete-collection", self._delete_collection)):
            act = Gio.SimpleAction(name=name)
            act.connect("activate", lambda *_a, f=fn: f())
            self.add_action(act)
        add_to = Gio.SimpleAction(name="add-to", parameter_type=GLib.VariantType("s"))
        add_to.connect("activate", lambda _a, v: self._add_to(library.get_collection(v.get_string())))
        self.add_action(add_to)
        sel_add = Gio.SimpleAction(name="sel-add-to", parameter_type=GLib.VariantType("s"))
        sel_add.connect("activate", lambda _a, v: self._sel_add_to(library.get_collection(v.get_string())))
        self.add_action(sel_add)
        self.ctx_item = self.ctx_collection = None
        app.set_accels_for_action("win.fullscreen", ["F11"])
        self._chrome_id = 0
        self.connect("notify::fullscreened", lambda *_: (self._fullscreen_icon(), self._chrome()))
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._moved)
        self.add_controller(motion)
        self._fullscreen_icon()
        esc = Gtk.EventControllerKey()  # bubble phase: dialogs and the viewer see Escape first
        esc.connect("key-pressed", lambda _c, kv, *_: kv == Gdk.KEY_Escape and self.is_fullscreen()
                    and (self.unfullscreen() or True))
        self.add_controller(esc)

        self.tv = Adw.ToolbarView(content=self.stack)
        self.tv.add_top_bar(header)
        self.sel_bar = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        bar = Gtk.ActionBar()
        self.sel_label = Gtk.Label(label="Select videos")
        bar.pack_start(self.sel_label)
        collage = Gtk.Button(label="Live Collage", action_name="win.sel-collage")
        self.sel_add = Gtk.MenuButton(label="Add to Collection")
        clear = Gtk.Button(label="Clear", action_name="win.sel-clear")
        for b in (clear, self.sel_add, collage):
            bar.pack_end(b)
        self.sel_btns = [collage, self.sel_add, clear]
        self.sel_bar.set_child(bar)
        self.tv.add_bottom_bar(self.sel_bar)
        self.nav = Adw.NavigationView()
        self.nav.add(Adw.NavigationPage(child=self.tv, title="Gallery", tag="gallery"))
        self.toasts = Adw.ToastOverlay(child=self.nav)  # around every page, so toasts show from any page
        self.set_content(self.toasts)
        first.set_active(True)
        self.apply_select_pref()
        self.selection_changed()
        self.rescan()

    def _fullscreen_icon(self):
        fs = self.is_fullscreen()
        self.fs_btn.set_icon_name("view-restore-symbolic" if fs else "view-fullscreen-symbolic")
        self.fs_btn.set_tooltip_text("Leave fullscreen (F11)" if fs else "Fullscreen (F11)")

    def _chrome(self, show=True):
        """Fullscreen hides the header and the cursor, like the viewer; moving the mouse brings them back."""
        fs = self.is_fullscreen()
        self.tv.set_reveal_top_bars(show or not fs)
        on_gallery = self.nav.get_visible_page().get_tag() == "gallery"  # the viewer decides its own cursor
        self.set_cursor_from_name(None if show or not fs or not on_gallery else "none")
        if self._chrome_id:
            GLib.source_remove(self._chrome_id)
            self._chrome_id = 0
        if fs and show:
            self._chrome_id = GLib.timeout_add(CHROME_IDLE_MS, self._chrome_later)

    def _chrome_later(self):
        self._chrome_id = 0
        self._chrome(False)
        return False

    def _moved(self, *_a):
        page = self.nav.get_visible_page()
        if self.is_fullscreen() and page is not None and page.get_tag() == "gallery":
            self._chrome()  # the viewer owns its own chrome while it is on top

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def _set_tab(self, kind):
        self.zoom_grid.set_visible(kind != "collections")  # each tab has its own saved zoom
        self.zoom_coll.set_visible(kind == "collections")
        if kind == "collections":
            self.stack.set_visible_child_name("collections")
        else:
            self.library_grid.set_kind(kind)
            self.stack.set_visible_child_name("library")

    def rescan(self):
        """Reload everything that's on screen: the library, the collections and an open collection page."""
        self.library_grid.status.set_description(f"Your library is {library.library_dir()}.\n"
                                                 "Press + to create a live image or collage, or import videos and GIFs.")

        def work():
            items = library.scan()
            rows = load_collections()
            GLib.idle_add(lambda: (self.library_grid.set_items(items), self.collections_view.set_collections(rows)))
        threading.Thread(target=work, daemon=True).start()
        for page in self.nav.get_navigation_stack():
            if isinstance(page, CollectionPage):
                page.refresh()

    # --- context menus --------------------------------------------------------------------------
    def _popup(self, widget, x, y, menu):
        pm = Gtk.PopoverMenu.new_from_model(menu)
        pm.set_parent(widget)
        pm.set_has_arrow(False)
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = int(x), int(y), 1, 1
        pm.set_pointing_to(r)
        pm.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pm.popup()

    def item_menu(self, widget, x, y, item, collection=None):
        self.ctx_item, self.ctx_collection = item, collection
        if item.path in SELECTED:
            return self._popup(widget, x, y, self._selection_menu())
        add = Gio.Menu()
        for c in library.collections():
            if c.path.resolve() != Path(item.path).parent.resolve():
                mi = Gio.MenuItem.new(c.name, None)
                mi.set_action_and_target_value("win.add-to", GLib.Variant.new_string(c.name))
                add.append_item(mi)
        add.append("New Collection…", "win.add-to-new")
        menu = Gio.Menu()
        edit = Gio.Menu()
        edit.append("Edit as Live Image…", "win.open-edit")
        edit.append("Live Collage — 1 slot", "win.open-collage1")
        edit.append("Live Collage — 4 slots", "win.open-collage4")
        menu.append_section(None, edit)
        menu.append_submenu("Add to Collection", add)
        if Path(item.path).parent.parent.resolve() == library.collections_dir().resolve():
            menu.append("Remove from Collection", "win.remove-from")
        if item.kind == "live":  # same entries + options dialog as the editor's Save ▾ menu
            menu.append("Save as GIF…", "win.export-gif")
            menu.append("Save as WebP…", "win.export-webp")
        menu.append("Delete", "win.delete")
        self._popup(widget, x, y, menu)

    def _selection_menu(self):
        n = len(SELECTED)
        menu = Gio.Menu()
        menu.append(f"Live Collage with {n} selected", "win.sel-collage")
        menu.append_submenu(f"Add {n} selected to Collection", self._collections_menu("win.sel-add-to", "win.sel-add-new"))
        menu.append(f"Delete {n} selected", "win.sel-delete")
        menu.append("Clear Selection", "win.sel-clear")
        return menu

    @staticmethod
    def _collections_menu(action, new_action):
        add = Gio.Menu()
        for c in library.collections():
            mi = Gio.MenuItem.new(c.name, None)
            mi.set_action_and_target_value(action, GLib.Variant.new_string(c.name))
            add.append_item(mi)
        add.append("New Collection…", new_action)
        return add

    def collection_menu(self, widget, x, y, collection):
        self.ctx_collection = collection
        menu = Gio.Menu()
        menu.append("Rename Collection…", "win.rename-collection")
        menu.append("Change Icon…", "win.change-icon")
        menu.append("Edit Collection…", "win.edit-collection")
        menu.append("Delete Collection", "win.delete-collection")
        self._popup(widget, x, y, menu)

    def _export_ctx(self, fmt):
        item = self.ctx_item  # the live image's video already has every look/overlay baked in: just encode it
        export.save_as(self, fmt, item.path, lambda _tmp: item.motion)

    def _delete_ctx(self):
        item = self.ctx_item
        library.delete_item(item)
        self.toast(f"Moved {item.name} to the Trash")
        self.rescan()

    def _remove_from(self):
        item = self.ctx_item
        try:
            library.remove_from_collection(item)
        except OSError as ex:
            return self.toast(f"Couldn't move: {ex}")
        self.toast(f"Moved {item.name} back to the library")
        self.rescan()

    def _add_to(self, collection):
        item = self.ctx_item
        try:
            added = library.add_to_collection(item, collection)
        except OSError as ex:
            return self.toast(f"Couldn't add: {ex}")
        self.toast(f"Added {item.name} to {collection.name}" if added else f"Already in {collection.name}")
        self.rescan()

    # --- multi-selection ------------------------------------------------------------------------
    def selection_changed(self):
        n = len(SELECTED)
        self.sel_bar.set_reveal_child(n > 0 or self.select_mode)
        self.sel_label.set_label(f"{n} selected" if n else "Select videos")
        for b in self.sel_btns:
            b.set_sensitive(n > 0)
        self.sel_add.set_menu_model(self._collections_menu("win.sel-add-to", "win.sel-add-new"))

    def set_select_mode(self, on):
        self.select_mode = on
        if not on:
            self.clear_selection()
        self.selection_changed()

    def clear_selection(self):
        SELECTED.clear()
        for t in TILES:
            t.sync_check()
        self.selection_changed()

    def apply_select_pref(self):
        on = library.load_config().get("select_button", True)
        self.select_btn.set_visible(on)
        if not on and self.select_btn.get_active():
            self.select_btn.set_active(False)

    def _sel_collage(self):
        items = list(SELECTED.values())
        if len(items) > 9:
            self.toast("A collage holds 9 clips: using the first 9")
            items = items[:9]
        page = CollagePage(self._saved)
        page._set_layout(COLLAGE_LAYOUT[len(items)])
        for i, it in enumerate(items):
            page.set_clip(i, it.motion)
        self.nav.push(page)
        self.set_select_mode(False)
        self.select_btn.set_active(False)

    def _sel_add_to(self, collection):
        items, added = list(SELECTED.values()), 0
        try:
            for it in items:
                added += bool(library.add_to_collection(it, collection))
        except OSError as ex:
            self.toast(f"Couldn't add: {ex}")
        else:
            self.toast(f"Added {added} of {len(items)} to {collection.name}")
        self.clear_selection()
        self.rescan()

    def _sel_delete(self):
        items = list(SELECTED.values())
        for it in items:
            library.delete_item(it)
        self.toast(f"Moved {len(items)} items to the Trash")
        self.clear_selection()
        self.rescan()

    def _add_to_new(self):
        collection_dialog(self, self._add_to)

    def new_collection(self):
        collection_dialog(self, lambda c: (self.toast(f"Created {c.name}"), self.rescan()))

    def open_collection(self, collection):
        self.nav.push(CollectionPage(self, collection))

    def change_icon(self, collection):
        def picked(path):
            try:
                library.set_collection_icon(collection, path)
            except OSError as ex:
                return self.toast(f"Couldn't set icon: {ex}")
            self.rescan()
        pick_icon(self, picked)

    def edit_collection(self, collection):
        collection_dialog(self, lambda c: self._collection_changed(collection, c), collection)

    def _collection_changed(self, old, new=None):
        """After a rename/edit: an open collection page follows, and everything on screen reloads."""
        new = new or old
        for page in self.nav.get_navigation_stack():
            if isinstance(page, CollectionPage) and page.collection.path == old.path:
                page.collection = new
                page.set_title(new.name)
        self.rescan()

    def rename_collection(self, collection):
        entry = Gtk.Entry(text=collection.name, activates_default=True, placeholder_text="Name")
        err = Gtk.Label(css_classes=["error", "caption"], visible=False, wrap=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(entry)
        box.append(err)
        dlg = Adw.AlertDialog(heading=f"Rename {collection.name}", extra_child=box,
                              default_response="ok", close_response="cancel")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Rename")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_response_enabled("ok", bool(entry.get_text().strip()))
        entry.connect("changed", lambda e: dlg.set_response_enabled("ok", bool(e.get_text().strip())))

        def respond(d, resp):
            if resp != "ok":
                return
            try:
                c = library.rename_collection(collection, entry.get_text())
            except (ValueError, OSError) as ex:  # duplicate name, or the folder is busy
                err.set_label(str(ex))
                err.set_visible(True)
                d.present(self)
                return
            self._collection_changed(collection, c)
        dlg.connect("response", respond)
        dlg.present(self)
        entry.grab_focus()
        entry.select_region(0, -1)

    def _delete_collection(self):
        c = self.ctx_collection
        dlg = Adw.AlertDialog(heading=f"Delete {c.name}?", body="The collection folder goes to the Trash. "
                              "Its live images and videos move back to your library.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def respond(_d, resp):
            if resp == "delete":
                library.delete_collection(c)
                self.toast(f"Deleted {c.name}")
                self.rescan()
        dlg.connect("response", respond)
        dlg.present(self)

    # --- creating & importing -------------------------------------------------------------------
    def open_editor(self, path):
        self.nav.push(EditorPage(path, self._saved))

    def open_collage(self, slots):
        """Collage maker with the right-clicked clip in 1 slot (the rest empty) or in all 4 of a 2×2 grid."""
        page = CollagePage(self._saved)
        if slots == 1:
            page.set_clip(0, self.ctx_item.motion)
        else:
            page.fill_all(self.ctx_item.motion, 0.0, None, dict(effects.DEFAULT), page.style)
        self.nav.push(page)

    def _saved(self, page, res, err):
        if err:
            page.save_btn.set_sensitive(True)
            page.save_btn.set_label(page.SAVE_LABEL)
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
                                     "images are saved here. Collections live in its collection/ subfolder. "
                                     "Existing files are not moved.")
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
        subf = Adw.SwitchRow(title="Read subfolders", active=library.load_config().get("scan_subfolders", True),
                             subtitle="Videos and live images inside subfolders of the library folder show up too; "
                             "the collection/ folder is always read (Preferences › Gallery decides where it shows)")

        def subf_toggled(w, _p):
            library.set_pref("scan_subfolders", w.get_active())
            self.rescan()
        subf.connect("notify::active", subf_toggled)
        group.add(subf)
        page.add(group)
        gallery = Adw.PreferencesGroup(title="Gallery")
        hover = Adw.SwitchRow(title="Hover to play", subtitle="Videos, live images and GIFs play (muted) while the "
                              "pointer is over them", active=library.load_config().get("hover_play", True))
        hover.connect("notify::active", lambda w, _p: library.set_pref("hover_play", w.get_active()))
        gallery.add(hover)
        for key, tab in (("all_shows_collected", "All"), ("live_shows_collected", "Live")):
            row = Adw.SwitchRow(title=f"Show collection items in {tab}", active=library.load_config().get(key, True),
                                subtitle="Items you added to a collection also appear in this tab" if tab == "All"
                                else "Live images you added to a collection also appear in this tab")

            def toggled(w, _p, key=key):
                library.set_pref(key, w.get_active())
                g = self.library_grid
                g.load_show_prefs()
                g.filter.changed(Gtk.FilterChange.DIFFERENT)
                g._update()
            row.connect("notify::active", toggled)
            gallery.add(row)
        page.add(gallery)
        multi = Adw.PreferencesGroup(title="Multi-select", description="Pick several videos to make a collage "
                                     "or add them to a collection together. Right-click a selected video for the menu.")
        btn = Adw.SwitchRow(title="Show Select button", subtitle="The checkmark button in the gallery header",
                            active=library.load_config().get("select_button", True))

        def btn_toggled(w, _p):
            library.set_pref("select_button", w.get_active())
            self.apply_select_pref()
        btn.connect("notify::active", btn_toggled)
        multi.add(btn)
        shapes = list(SELECT_SHAPES)
        cfg = library.load_config()
        shape = Adw.ComboRow(title="Drag style", subtitle="How dragging across the grid picks videos",
                             model=Gtk.StringList.new(list(SELECT_SHAPES.values())))
        shape.set_selected(shapes.index(cfg.get("select_shape", "linear")) if cfg.get("select_shape") in shapes else 0)
        shape.connect("notify::selected", lambda w, _p: library.set_pref("select_shape", shapes[w.get_selected()]))
        multi.add(shape)
        keys = list(SELECT_KEYS)
        labels = Gtk.StringList.new([v[0] for v in SELECT_KEYS.values()])
        busy = {"on": False}

        def combo(title, subtitle):
            r = Adw.ComboRow(title=title, subtitle=subtitle, model=labels)
            multi.add(r)
            return r

        split = Adw.SwitchRow(title="A different key for each style", active=cfg.get("select_split", False),
                              subtitle="Off: both styles use one key and Drag style decides")
        multi.add(split)
        one = combo("Hold to select", "Hold this key and click or drag to add videos to the selection")
        lin = combo("Linear select key", "Hold this key to paint the videos the pointer passes over")
        rect = combo("Rectangle select key", "Hold this key and drag a box around the videos to select")

        def pick_key(which, name):
            if busy["on"]:
                return
            old_lin, old_rect = select_key_names()
            if which == "single":
                library.set_pref("select_key", name)
            elif which == "linear":
                library.set_pref("select_key_linear", name)
                if name == old_rect:  # one key each: the rectangle style takes the key linear just left
                    library.set_pref("select_key_rect", old_lin)
            else:
                library.set_pref("select_key_rect", name)
                if name == old_lin:
                    library.set_pref("select_key_linear", old_rect)
            sync()

        def sync():
            cur = library.load_config().get("select_key", "ctrl")
            a, b = select_key_names()
            busy["on"] = True
            one.set_selected(keys.index(cur) if cur in keys else 0)
            lin.set_selected(keys.index(a))
            rect.set_selected(keys.index(b))
            busy["on"] = False
            show()

        def show():
            split = library.load_config().get("select_split", False)
            one.set_visible(not split)
            lin.set_visible(split)
            rect.set_visible(split)

        one.connect("notify::selected", lambda w, _p: pick_key("single", keys[w.get_selected()]))
        lin.connect("notify::selected", lambda w, _p: pick_key("linear", keys[w.get_selected()]))
        rect.connect("notify::selected", lambda w, _p: pick_key("rect", keys[w.get_selected()]))

        def split_toggled(w, _p):
            on = w.get_active()
            library.set_pref("select_split", on)
            if on:  # start from the key that was in use
                cur = library.load_config().get("select_key", "ctrl")
                cur = cur if cur in SELECT_KEYS else "ctrl"
                library.set_pref("select_key_linear", cur)
                library.set_pref("select_key_rect", next(k for k in SELECT_KEYS if k != cur))
            sync()
        split.connect("notify::active", split_toggled)
        sync()
        page.add(multi)
        perf = Adw.PreferencesGroup(title="Performance", description="Applies to previews and to saved files.")
        gpu = Adw.SwitchRow(title="GPU acceleration", active=thumbs.gpu_enabled(),
                            subtitle="Checking the graphics card…")
        gpu.connect("notify::active", lambda w, _p: library.set_pref("gpu", w.get_active()))
        perf.add(gpu)

        def probed():  # NVENC check runs ffmpeg once; keep it off the UI thread
            ok = thumbs.nvenc_available()
            GLib.idle_add(lambda: (gpu.set_subtitle("Encode video on the NVIDIA GPU (NVENC); off = CPU (x264)" if ok
                                                    else "No NVIDIA hardware encoder found, the CPU is used"),
                                   gpu.set_sensitive(ok)))
        threading.Thread(target=probed, daemon=True).start()
        quality = Adw.ComboRow(title="Preview size", subtitle="Smaller previews render and play faster; the editor "
                               "and collage also have this in their header. Saving is always full quality.",
                               model=Gtk.StringList.new([n for n, _ in thumbs.PREVIEW_HEIGHTS]))
        cur = thumbs.preview_height() or 0
        quality.set_selected(next((i for i, (_, h) in enumerate(thumbs.PREVIEW_HEIGHTS) if h == cur), 2))
        quality.connect("notify::selected", lambda w, _p: library.set_pref("preview_height",
                                                                           thumbs.PREVIEW_HEIGHTS[w.get_selected()][1]))
        perf.add(quality)
        page.add(perf)
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

    def do_shutdown(self):
        for w in self.get_windows():  # stop playback before the display goes away
            nav = getattr(w, "nav", None)
            for page in (nav.get_navigation_stack() if nav else []):
                stop = getattr(page, "stop", None)
                if stop:
                    stop()
        Adw.Application.do_shutdown(self)


def main():
    return App().run(None)


def _selftest():
    """Multi-select key rules against a throwaway config: python3 -m linuxlivepaper.app --selftest"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        real, library.CONFIG = library.CONFIG, Path(d, "config.json")
        try:
            assert select_masks() == (Gdk.ModifierType.CONTROL_MASK,) * 2, "ctrl by default, one key for both styles"
            library.set_pref("select_key", "alt")
            assert select_key_names() == ("alt", "alt") and select_shape() == "linear"
            library.set_pref("select_split", True)
            assert select_key_names() == ("alt", "ctrl"), "both styles never share a key"
            library.set_pref("select_key_rect", "alt")  # same as the linear key: a free key is picked instead
            assert select_key_names() == ("alt", "ctrl")
            library.set_pref("select_key_rect", "shift")
            assert select_key_names() == ("alt", "shift")
            assert select_shape(Gdk.ModifierType.SHIFT_MASK) == "rect"
            assert select_shape(Gdk.ModifierType.ALT_MASK) == "linear"
            library.set_pref("select_shape", "rect")
            assert select_shape() == "rect"
        finally:
            library.CONFIG = real
    print("select prefs ok")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main())
