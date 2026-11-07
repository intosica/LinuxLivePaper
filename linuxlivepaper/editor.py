"""Live-image editor: preview + zoomable filmstrip timeline with trim handles and a cover-frame marker.

Preview strategy (GTK's GStreamer seek isn't frame-exact): while paused we show an exact ffmpeg frame;
while playing we loop a small ffmpeg-rendered proxy of exactly the selection, with sound."""
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cairo
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango, PangoCairo  # noqa: E402

from . import effects, export, library, livemake, overlays, thumbs  # noqa: E402
from .overlay_ui import OverlayHost  # noqa: E402
from .widgets import CollectionRow, HoldButton, InterpGroup, SaveProgress, preview_quality_dropdown, speed_row  # noqa: E402
from .playback import DEFAULT_STYLE, STYLES, LiveCanvas, media_file, release  # noqa: E402

YELLOW = (0.96, 0.76, 0.07)
SELECT = Gdk.RGBA()
SELECT.parse("#3584e4")
OVERLAY_WHITE = Gdk.RGBA()
OVERLAY_WHITE.parse("white")
PAD = 16                     # horizontal padding so edge handles stay grabbable
OV_Y, OV_H = 2, 30           # overview filmstrip (whole video) with the zoomed view window
RU_Y, RU_H = 40, 18          # ruler
ST_Y, ST_H = 60, 64          # filmstrip
CV_Y, CV_H = ST_Y + ST_H + 4, 18  # cover knob row
HEIGHT = CV_Y + CV_H + 2
HW = 12                      # trim handle width
MIN_SEL = 0.3
DEFAULT_SEL = 3.0


def direct_ok(opts, overlays, src):
    """Nothing to bake (no look/effect/speed/direction/overlay): the source plays as is."""
    return opts == effects.DEFAULT and not overlays and not src.lower().endswith(".gif")


def fmt(t, fine=True):
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{s:05.2f}" if fine else f"{int(m)}:{int(s):02d}"


_FILM_STRIP = ThreadPoolExecutor(max_workers=3)  # one pool for every timeline: a pool per instance piled threads up
_STRIPS = {}  # src → {t: pixbuf}, shared by every timeline of that clip (the collage rebuilds its timeline a lot)


class Timeline(Gtk.DrawingArea):
    """Emits changed(what) with what in start|end|move|cover|scrub. Times are absolute seconds."""
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, src, duration, aspect, fixed=False):
        """fixed: no cover marker (the collage uses the whole window); the handles still stretch the selection."""
        super().__init__(content_height=HEIGHT, hexpand=True, focusable=False)
        self.src, self.D, self.aspect, self.fixed = src, max(duration, 0.1), aspect, fixed
        self.s, self.e = 0.0, min(DEFAULT_SEL, self.D)
        self.k = self.p = 0.0
        self.k = (self.s + self.e) / 2
        span = self.D if self.D <= 60 else 30.0
        self.v0, self.v1 = 0.0, span
        self.frames = _STRIPS.pop(src, None) or {}
        _STRIPS[src] = self.frames  # most recent last
        while len(_STRIPS) > 6:
            _STRIPS.pop(next(iter(_STRIPS)))
        self.wanted = set()
        self.fgen, self.static = 0, (None, None)  # frames-arrived counter; cached (key, surface) of all but the playhead
        # bookmarks live in the clip's own .linuxlivepaper.json sidecar, so they survive a restart
        self.marks = sorted(float(m) for m in (library.get_meta(src).get("bookmarks") or [])
                            if isinstance(m, (int, float)) and 0 <= m <= self.D)
        self.pool = _FILM_STRIP
        self.mode, self.mx = None, 0.0
        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", lambda *_: setattr(self, "mode", None))
        self.add_controller(drag)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        self.add_controller(motion)
        double = Gtk.GestureClick()
        double.connect("pressed", self._double)
        self.add_controller(double)
        mark = Gtk.GestureClick(button=3)  # right click on the ruler: add / remove a bookmark
        mark.connect("pressed", lambda _g, _n, x, _y: self.toggle_mark(min(max(self.t(x), 0.0), self.D)))
        self.add_controller(mark)

    # --- geometry -------------------------------------------------------------------------------
    def _w(self):
        return max(1, self.get_width() - 2 * PAD)

    def x(self, t):
        return PAD + (t - self.v0) / (self.v1 - self.v0) * self._w()

    def t(self, x):
        return self.v0 + (x - PAD) / self._w() * (self.v1 - self.v0)

    def ox(self, t):
        return PAD + t / self.D * self._w()

    def set_view(self, v0, span):
        span = min(self.D, max(min(self.D, 1.0), span))
        v0 = min(max(0.0, v0), self.D - span)
        self.v0, self.v1 = v0, v0 + span
        self.queue_draw()

    def zoom(self, factor, around=None):
        around = (self.v0 + self.v1) / 2 if around is None else around
        span = (self.v1 - self.v0) * factor
        frac = (around - self.v0) / (self.v1 - self.v0)
        self.set_view(around - frac * span, span)

    def zoom_to_selection(self):
        span = (self.e - self.s) * 3
        self.set_view(self.s - (span - (self.e - self.s)) / 2, span)

    def set_selection(self, s, e, what="move"):
        s = min(max(0.0, s), max(0.0, self.D - MIN_SEL))
        self.s, self.e = s, min(self.D, max(s + MIN_SEL, e))
        self.k = min(max(self.k, s), e)
        self.queue_draw()
        self.emit("changed", what)

    # --- bookmarks ------------------------------------------------------------------------------
    def toggle_mark(self, t=None):
        """Add (or remove) a bookmark at t (default: the playhead) and save it with the clip."""
        t = self.p if t is None else t
        near = next((m for m in self.marks if abs(m - t) < 0.3), None)
        if near is not None:
            self.marks.remove(near)
        else:
            self.marks = sorted([*self.marks, round(t, 3)])
        try:
            library.set_meta(self.src, bookmarks=self.marks)
        except OSError:
            pass
        self.queue_draw()

    def jump_mark(self, step):
        """Playhead to the next (1) or previous (-1) bookmark; False when there is none that way."""
        cands = [m for m in self.marks if (m > self.p + 0.01 if step > 0 else m < self.p - 0.01)]
        if not cands:
            return False
        self.p = min(cands) if step > 0 else max(cands)
        self.queue_draw()
        self.emit("changed", "scrub")
        return True

    # --- input ----------------------------------------------------------------------------------
    def _hit(self, x, y):
        h = self._hit_any(x, y)
        return "move" if self.fixed and h == "cover" else h

    def _hit_any(self, x, y):
        if y < RU_Y:
            return "overview"
        if y >= CV_Y and abs(x - self.x(self.k)) < 28:
            return "cover"
        xs, xe = self.x(self.s), self.x(self.e)
        if ST_Y <= y < CV_Y or RU_Y <= y < ST_Y:
            ds, de = abs(x - (xs - HW / 2)), abs(x - (xe + HW / 2))
            if min(ds, de) <= HW:
                return "start" if ds < de else "end"
        if ST_Y <= y < ST_Y + ST_H and xs < x < xe:
            return "move"
        return "scrub"

    def _motion(self, _c, x, y):
        self.mx = x
        cursor = {"start": "ew-resize", "end": "ew-resize", "move": "grab", "cover": "grab",
                  "overview": "pointer"}.get(self._hit(x, y), "text")
        self.set_cursor_from_name(cursor)

    def _double(self, _g, n, x, _y):
        """Double click: centre the selection on the clicked time (a preset length is kept)."""
        if n != 2:
            return
        t = min(max(self.t(x), 0.0), self.D)
        length = min(max(MIN_SEL, self.e - self.s), self.D)
        s = min(max(t - length / 2, 0.0), max(0.0, self.D - length))
        self.s, self.e = s, min(self.D, s + length)
        self.k = min(max(self.k, self.s), self.e)
        self.queue_draw()
        self.emit("changed", "move")

    def _begin(self, _g, x, y):
        self.mode = self._hit(x, y)
        self.x0, self.grab = x, self.t(x)
        self.sel0, self.k0 = (self.s, self.e), self.k
        self._apply(x)

    def _update(self, _g, dx, _dy):
        if self.mode:
            self._apply(self.x0 + dx)

    def _apply(self, x):
        t = min(max(self.t(x), 0.0), self.D)
        m = self.mode
        if m == "overview":
            ot = (x - PAD) / self._w() * self.D
            span = self.v1 - self.v0
            self.set_view(ot - span / 2, span)
            return
        if m == "start":
            self.s = min(t, self.e - MIN_SEL)
        elif m == "end":
            self.e = max(t, self.s + MIN_SEL)
        elif m == "move":
            length = self.sel0[1] - self.sel0[0]
            self.s = min(max(self.sel0[0] + (t - self.grab), 0.0), self.D - length)
            self.e = self.s + length
            self.k = self.k0 + (self.s - self.sel0[0])
        elif m == "cover":
            self.k = t
        else:
            self.p = t
        self.k = min(max(self.k, self.s), self.e)
        self.queue_draw()
        self.emit("changed", m)

    def _scroll(self, ctrl, dx, dy):
        state = ctrl.get_current_event_state()
        span = self.v1 - self.v0
        if dx or state & Gdk.ModifierType.SHIFT_MASK:
            self.set_view(self.v0 + (dx or dy) * span * 0.08, span)
        else:
            self.zoom(1.2 ** dy, self.t(self.mx))
        return True

    # --- filmstrip frames -----------------------------------------------------------------------
    def _frame(self, t):
        pb = self.frames.get(t)
        if pb is None and t not in self.wanted:
            self.wanted.add(t)
            self.pool.submit(self._load, t)
        return pb

    def _load(self, t):
        if t not in self.wanted:  # scrolled away before we got to it
            return
        pb = thumbs.grab_frame(self.src, t, ST_H * 2, key=True)

        def done():
            self.wanted.discard(t)
            if pb:
                self.frames[t] = pb
                self.fgen += 1
                if len(self.frames) > 600:
                    self.frames.pop(next(iter(self.frames)))
                self.queue_draw()
        GLib.idle_add(done)

    def _nearest(self, t):
        if not self.frames:
            return None
        return self.frames[min(self.frames, key=lambda k: abs(k - t))]

    # --- drawing --------------------------------------------------------------------------------
    @staticmethod
    def _rrect(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _text(self, cr, x, y, text, size=8, rgba=(1, 1, 1, 0.7), bold=False, center=False):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription(f"Sans {'Bold ' if bold else ''}{size}"))
        layout.set_text(text, -1)
        w = layout.get_pixel_size()[0]
        cr.set_source_rgba(*rgba)
        cr.move_to(x - (w / 2 if center else 0), y)
        PangoCairo.show_layout(cr, layout)

    def _strip(self, cr, y, h, v0, v1, xf):
        """Filmstrip of [v0, v1] into rows y..y+h, xf maps time → x. Frames sit on a power-of-2 time grid,
        so they're reused across zoom levels; missing ones fall back to the nearest cached frame."""
        pps = self._w() / (v1 - v0)
        fstep = 2 ** math.ceil(math.log2(max(h * self.aspect / pps, 1 / 64)))
        k = int(v0 // fstep)
        while k * fstep < min(v1, self.D):
            t = k * fstep
            pb = self._frame(round(t, 4)) or self._nearest(t)
            if pb:
                x, cw = xf(t), fstep * pps
                sc = max(cw / pb.get_width(), h / pb.get_height())
                cr.save()
                cr.rectangle(x, y, cw + 0.5, h)
                cr.clip()
                cr.translate(x + (cw - pb.get_width() * sc) / 2, y + (h - pb.get_height() * sc) / 2)
                cr.scale(sc, sc)
                Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
                cr.paint()
                cr.restore()
            k += 1

    def _draw(self, _a, cr, W, H):
        """Everything but the playhead and the length pill is drawn once into a cached surface: while playing only the
        red line moves 25x a second, and redrawing the filmstrip (pixbuf -> surface per frame) cost ~4 ms each time."""
        key = (W, H, self.v0, self.v1, self.s, self.e, self.k, tuple(self.marks), self.fgen, self.D)
        if self.static[0] != key:
            surf = cr.get_target().create_similar(cairo.CONTENT_COLOR_ALPHA, W, H)
            self._draw_static(cairo.Context(surf), W)
            self.static = (key, surf)
        cr.set_source_surface(self.static[1], 0, 0)
        cr.paint()
        xs, xe = self.x(self.s), self.x(self.e)
        # playhead
        xp = self.x(self.p)
        if PAD - 1 <= xp <= W - PAD + 1:
            cr.set_source_rgb(0.9, 0.2, 0.2)
            cr.rectangle(xp - 1, RU_Y + 6, 2, ST_Y + ST_H - RU_Y - 6)
            cr.fill()
            cr.move_to(xp - 5, RU_Y + 4)
            cr.line_to(xp + 5, RU_Y + 4)
            cr.line_to(xp, RU_Y + 10)
            cr.fill()
        # selection length pill above the selection
        label = f"{self.e - self.s:.1f}s"
        self._rrect(cr, (xs + xe) / 2 - 22, ST_Y - 18, 44, 16, 8)
        cr.set_source_rgb(*YELLOW)
        cr.fill()
        self._text(cr, (xs + xe) / 2, ST_Y - 17, label, 8, (0, 0, 0, 0.9), True, True)

    def _draw_static(self, cr, W):
        w = self._w()
        span = self.v1 - self.v0
        # overview: whole video, dimmed except the zoomed view window; selection as a yellow bar
        cr.save()
        self._rrect(cr, PAD, OV_Y, w, OV_H, 6)
        cr.clip()
        cr.set_source_rgba(1, 1, 1, 0.06)
        cr.paint()
        self._strip(cr, OV_Y, OV_H, 0.0, self.D, self.ox)
        ov0, ov1 = self.ox(self.v0), max(self.ox(self.v1), self.ox(self.v0) + 6)
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.rectangle(0, OV_Y, ov0, OV_H)
        cr.rectangle(ov1, OV_Y, W, OV_H)
        cr.fill()
        cr.set_source_rgb(*YELLOW)
        cr.rectangle(self.ox(self.s), OV_Y + OV_H - 5, max(3, self.ox(self.e) - self.ox(self.s)), 5)
        cr.fill()
        cr.restore()
        self._rrect(cr, ov0, OV_Y, ov1 - ov0, OV_H, 4)
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.set_line_width(2)
        cr.stroke()
        # ruler
        pps = w / span
        step = next((s for s in (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600) if s * pps >= 80), 1200)
        t = math.ceil(self.v0 / step) * step
        while t <= self.v1:
            x = self.x(t)
            cr.set_source_rgba(1, 1, 1, 0.3)
            cr.rectangle(x, RU_Y + 12, 1, 6)
            cr.fill()
            self._text(cr, x + 3, RU_Y, fmt(t, step < 1))
            t += step
        # filmstrip
        cr.save()
        self._rrect(cr, PAD, ST_Y, w, ST_H, 8)
        cr.clip()
        cr.set_source_rgba(1, 1, 1, 0.06)
        cr.paint()
        self._strip(cr, ST_Y, ST_H, self.v0, self.v1, self.x)
        # dim outside selection; past the end of the video
        xs, xe = self.x(self.s), self.x(self.e)
        cr.set_source_rgba(0, 0, 0, 0.62)
        cr.rectangle(0, ST_Y, max(0, xs), ST_H)
        cr.rectangle(xe, ST_Y, max(0, W - xe), ST_H)
        cr.fill()
        cr.restore()
        # selection frame + handles
        cr.set_source_rgb(*YELLOW)
        cr.rectangle(xs, ST_Y, xe - xs, 3)
        cr.rectangle(xs, ST_Y + ST_H - 3, xe - xs, 3)
        cr.fill()
        for hx in (xs - HW, xe):
            self._rrect(cr, hx, ST_Y, HW, ST_H, 4)
            cr.set_source_rgb(*YELLOW)
            cr.fill()
            cr.set_source_rgba(0, 0, 0, 0.6)
            cr.rectangle(hx + HW / 2 - 1, ST_Y + ST_H / 2 - 10, 2, 20)
            cr.fill()
        # cover marker
        xk = -1000 if self.fixed else self.x(self.k)
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.rectangle(xk - 1, ST_Y + 3, 2, ST_H - 6)
        cr.fill()
        self._rrect(cr, xk - 24, CV_Y, 48, CV_H, CV_H / 2)
        cr.fill()
        self._text(cr, xk, CV_Y + 2, "COVER", 7, (0, 0, 0, 0.85), True, True)
        # bookmarks: a tick in the ruler plus a small flag at the top of the filmstrip
        cr.set_source_rgb(0.35, 0.75, 1.0)
        for m in self.marks:
            if not self.v0 <= m <= self.v1:
                continue
            xm = self.x(m)
            cr.rectangle(xm - 1, RU_Y + 2, 2, RU_H - 4)
            cr.fill()
            cr.move_to(xm - 4, ST_Y + 1)
            cr.line_to(xm + 4, ST_Y + 1)
            cr.line_to(xm, ST_Y + 9)
            cr.fill()


class EditorPage(OverlayHost, Adw.NavigationPage):
    """Pick a stretch of a video/GIF and save it as a live image."""
    SAVE_LABEL = "Save Live Image"

    def __init__(self, src, on_saved):
        super().__init__(title="Create Live Image")
        self.src, self.on_saved = src, on_saved
        dur, vw, vh = thumbs.probe(src)
        self.D = dur or 1.0
        self.preview_pool = ThreadPoolExecutor(max_workers=1)
        self.req = self.play_req = 0
        self.stream, self.tick, self.proxies = None, 0, set()
        # The source, opened (prerolled, paused) up front so a direct Play only has to seek: building a fresh
        # pipeline on Play (demuxer + GPU decoder start-up) took 1–3 s. Kept after Stop for the next Play.
        self.warm = None if src.lower().endswith(".gif") else media_file(src, muted=False, loop=True)
        self.opts = dict(effects.DEFAULT)
        self.style = DEFAULT_STYLE
        self.repeat = 1  # times the clip plays back to back in the saved file
        self.aspect = vw / vh if vh else 16 / 9
        self.canvas, self.trying = None, False
        self.ov_layer_on = True
        self._try_gen = 0

        self.tl = Timeline(src, self.D, vw / vh if vh else 16 / 9)
        self.tl.connect("changed", self._changed)

        self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, vexpand=True, hexpand=True)
        frame = Gtk.Overlay(css_classes=["viewer", "card"], overflow=Gtk.Overflow.HIDDEN, child=self.pic, hexpand=True)
        self.frame = frame
        self.spinner = Gtk.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=32,
                                   height_request=32)
        self.ov_init()
        frame.add_overlay(self.layer)
        frame.add_overlay(self.spinner)

        self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play selection (Space)",
                                   css_classes=["circular", "suggested-action"])
        self.play_btn.connect("clicked", lambda *_: self.toggle_play())
        self.time_lbl = Gtk.Label(css_classes=["numeric", "dim-label"], width_chars=16, xalign=0)
        presets = Gtk.Box(css_classes=["linked"])
        for secs in (1.5, 3, 5):
            b = Gtk.Button(label=f"{secs:g}s", tooltip_text=f"Make the selection {secs:g} seconds long")
            b.connect("clicked", lambda _b, n=secs: self._set_length(n))
            presets.append(b)
        here = Gtk.Button(label="Start at playhead", tooltip_text="Move the selection so it starts at the red playhead")
        here.connect("clicked", lambda *_: self._start_at_playhead())
        cover = Gtk.Button(label="Cover = playhead", tooltip_text="Use the frame under the red playhead as the still")
        cover.connect("clicked", lambda *_: self._cover_at_playhead())
        mark = Gtk.Button(icon_name="user-bookmarks-symbolic",
                          tooltip_text="Bookmark this frame (B) · right-click the timeline to add or remove one")
        mark.connect("clicked", lambda *_: self.tl.toggle_mark())
        zoom = Gtk.Box(css_classes=["linked"])
        for icon, tip, fn in (("zoom-out-symbolic", "Zoom out", lambda: self.tl.zoom(1.6)),
                              ("zoom-fit-best-symbolic", "Zoom to selection", self.tl.zoom_to_selection),
                              ("zoom-in-symbolic", "Zoom in", lambda: self.tl.zoom(1 / 1.6))):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.connect("clicked", lambda _b, f=fn: f())
            zoom.append(b)
        controls = Gtk.Box(spacing=8)
        for wdg in (self.play_btn, self.time_lbl, Gtk.Box(hexpand=True), here, cover, mark, presets, zoom):
            controls.append(wdg)

        hint = Gtk.Label(wrap=True, css_classes=["dim-label", "caption"], label=(
            "Drag the yellow handles to trim · drag inside the selection to move it · drag COVER to pick the still · "
            "click to move the playhead · B or right-click to bookmark, , and . jump · scroll to zoom, "
            "Shift+scroll to pan · drag the top bar to jump around"))

        menu = Gio.Menu()
        menu.append("Save as GIF…", "export.gif")
        menu.append("Save as WebP…", "export.webp")
        self.save_btn = Adw.SplitButton(label=self.SAVE_LABEL, menu_model=menu, css_classes=["suggested-action"],
                                        tooltip_text="Save as a live image · arrow: GIF / WebP for sharing")
        self.save_btn.connect("clicked", self._save)
        group = Gio.SimpleActionGroup()
        for fmt in ("gif", "webp"):
            act = Gio.SimpleAction(name=fmt)
            act.connect("activate", lambda *_, f=fmt: self._export(f))
            group.add_action(act)
        self.insert_action_group("export", group)
        collage_btn = Gtk.Button(label="Create Collage", tooltip_text="Open a 4-video collage with this clip in every cell")
        collage_btn.connect("clicked", lambda *_: self._collage())
        header = Adw.HeaderBar()
        self.progress = SaveProgress(header)
        header.pack_end(self.save_btn)
        header.pack_end(collage_btn)
        header.pack_start(preview_quality_dropdown())

        top = Gtk.Box(spacing=12, vexpand=True)
        top.append(frame)
        top.append(self._side_panel())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18, margin_end=18,
                      margin_top=12, margin_bottom=18)
        for wdg in (top, controls, self.tl, hint):
            box.append(wdg)
        tv = Adw.ToolbarView(content=box)
        tv.add_top_bar(header)
        self.set_child(tv)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self.connect("hidden", lambda *_: self._cleanup())
        self._show_frame(self.tl.k)
        self._update_label()

    # --- effects panel --------------------------------------------------------------------------
    def _combo(self, title, subtitle, choices, current, on_change):
        """Adw.ComboRow over (id, label) choices."""
        ids = [c[0] for c in choices]
        row = Adw.ComboRow(title=title, subtitle=subtitle, model=Gtk.StringList.new([c[1] for c in choices]),
                           selected=ids.index(current))
        row.connect("notify::selected", lambda r, _: on_change(ids[r.get_selected()]))
        return row

    def _side_panel(self):
        page = Adw.PreferencesPage(width_request=330, hexpand=False, vexpand=True)
        look = Adw.PreferencesGroup(title="Look")
        look.add(self._combo("Filter", "Color grade", [(k, v[0]) for k, v in effects.LOOKS.items()],
                             self.opts["look"], lambda v: self._set_opt("look", v)))
        look.add(self._combo("Effect", "Stylised motion", [(k, v[0]) for k, v in effects.EFFECTS.items()],
                             self.opts["effect"], lambda v: self._set_opt("effect", v)))
        page.add(look)
        motion = Adw.PreferencesGroup(title="Motion")
        motion.add(speed_row(self.opts["speed"], lambda v: self._set_opt("speed", v)))
        motion.add(self._combo("Direction", "Bounce plays forward, then back", list(effects.DIRECTIONS.items()),
                               self.opts["direction"], lambda v: self._set_opt("direction", v)))
        rp = Adw.SpinRow.new_with_range(1, 10, 1)
        rp.set_title("Repeat")
        rp.set_subtitle("Times the clip plays in the saved file")
        rp.set_value(self.repeat)
        rp.connect("notify::value", lambda r, _: setattr(self, "repeat", int(r.get_value())))
        motion.add(rp)
        page.add(motion)
        press = Adw.PreferencesGroup(title="Press effect",
                                     description="How the still turns into motion when you press and hold it.")
        press.add(self._combo("Transition", None, [(k, v[0]) for k, v in STYLES.items()], self.style,
                              self._set_style))
        try_row = Adw.ActionRow(title="Try it", subtitle="Press and hold to preview the saved result")
        self.try_btn = HoldButton(label="Hold", tooltip_text="Press and hold to see the saved result",
                                  on_press=self._try_press, on_release=self._try_release)
        try_row.add_suffix(self.try_btn)
        press.add(try_row)
        page.add(press)
        page.add(self.overlay_group)
        self.interp_group = InterpGroup()
        page.add(self.interp_group)
        save = Adw.PreferencesGroup(title="Save")
        self.collection_row = CollectionRow()
        save.add(self.collection_row)
        page.add(save)
        reset = Gtk.Button(label="Reset effects", halign=Gtk.Align.CENTER, css_classes=["flat"])
        reset.connect("clicked", lambda *_: self._reset())
        grp = Adw.PreferencesGroup()
        grp.add(reset)
        page.add(grp)
        self.panel = page
        self.sync_overlay_ui()
        return page

    # --- overlay hooks (the panel itself lives in overlay_ui) ------------------------------------
    def ov_rect(self):
        w, h = self.layer.get_width(), self.layer.get_height()
        a = self.aspect
        vw, vh = (w, w / a) if w / a <= h else (h * a, h)
        return (w - vw) / 2, (h - vh) / 2, vw, vh

    def ov_duration(self):
        return max(0.1, self.tl.e - self.tl.s)

    def ov_changed(self):
        super().ov_changed()
        if self.stream:  # re-bake the looping preview so the change shows while playing
            self._rebake()

    def _rebake(self):
        """Debounced restart of the baked preview after an overlay/effect edit during playback."""
        if getattr(self, "_rebake_id", 0):
            GLib.source_remove(self._rebake_id)
        self._rebake_id = GLib.timeout_add(400, self._do_rebake)

    def _do_rebake(self):
        self._rebake_id = 0
        if self.stream:
            self.stop()
            self.toggle_play()
        return False

    def _reset(self):
        parent = self.panel.get_parent()
        parent.remove(self.panel)
        self.opts, self.style, self.repeat = dict(effects.DEFAULT), DEFAULT_STYLE, 1
        self.ov_reset()
        parent.append(self._side_panel())
        self._changed(None, "cover")

    def _set_opt(self, key, value):
        if self.opts[key] == value:
            return
        self.opts[key] = value
        was_playing = self.stream is not None
        self.stop()
        if was_playing or key in ("speed", "direction"):
            self.toggle_play()  # motion settings only make sense in motion
        else:
            self._show_frame(self.tl.k if self.tl.s <= self.tl.p <= self.tl.e else self.tl.p)

    def _set_style(self, style):
        self.style = style

    def _try_press(self):
        """Render the selection like it will be saved, then run the real press transition over the cover frame."""
        if self.trying:
            return
        self.trying = True
        self._try_gen += 1
        gen = self._try_gen
        self.stop()
        s, e, k = self.tl.s, self.tl.e, self.tl.k
        opts, n = dict(self.opts), self.repeat
        self.spinner.start()

        def work():
            try:
                path = self._preview_path(s, e, opts)
                if n > 1:  # Try it shows every repeat, like the saved file
                    path = str(thumbs.cached(Path(f"{path[:-4]}.x{n}.mp4"), lambda tmp: thumbs.repeat(path, tmp, n)))
                still = thumbs.grab_frame(path, min(effects.out_time(k - s, e - s, opts),
                                                    effects.out_length(e - s, opts) - 0.05))
            except Exception:
                import traceback
                traceback.print_exc()
                path = still = None
            GLib.idle_add(start, path, still)

        def start(path, still):
            if gen != self._try_gen:  # released and pressed again meanwhile
                return
            self.spinner.stop()
            if not (path and still):
                self.trying = False
                return
            self.proxies.add(path)
            self.ov_layer_on = False
            self.layer.set_visible(False)
            self.canvas = LiveCanvas(Gdk.Texture.new_for_pixbuf(still), path, style=self.style)
            self.frame.set_child(self.canvas)
            if self.trying:
                self.canvas.press()
            else:  # released while rendering: show the still a moment, then go back
                GLib.timeout_add(600, self._try_done)
        threading.Thread(target=work, daemon=True).start()

    def _try_release(self):
        self.trying = False
        canvas = self.canvas
        if canvas:
            canvas.on_idle = self._try_done
            canvas.release()
        elif self.spinner.get_spinning():
            self._try_gen += 1  # abandon the pending render
            self.spinner.stop()

    def _try_done(self):
        if self.canvas:
            self.canvas.stop()
            self.canvas = None
            self.frame.set_child(self.pic)
        self.ov_layer_on = True
        self.layer.set_visible(bool(self.overlays))
        return False

    # --- preview --------------------------------------------------------------------------------
    def _preview_path(self, s, e, opts):
        if self.overlays:
            return overlays.preview(self.src, s, e, opts, thumbs.CACHE / "previews", height=thumbs.preview_height(),
                                    overlay_list=[dict(o) for o in self.overlays])
        return effects.preview(self.src, s, e, opts, thumbs.CACHE / "previews", height=thumbs.preview_height())

    def _show_frame(self, t):
        self.req += 1
        req = self.req
        self.preview_pool.submit(self._grab, t, req)

    def _grab(self, t, req):
        if req != self.req:  # a newer request superseded this one
            return
        pb = thumbs.grab_frame(self.src, t, thumbs.preview_height() and 720, effects.frame_chain(self.opts))  # stills are cheap

        def done():
            if req == self.req and pb and self.stream is None:
                self.pic.set_paintable(Gdk.Texture.new_for_pixbuf(pb))
        GLib.idle_add(done)

    def toggle_play(self):
        if self.stream or self.spinner.get_spinning():
            self.stop()
            return
        self._try_done()
        s, e, opts = self.tl.s, self.tl.e, dict(self.opts)
        # start at the red playhead when it sits in the yellow selection (it defaults to the selection start); the
        # bake and the loop always cover the whole selection
        at = self.tl.p if s <= self.tl.p <= e - 0.05 else s
        # where in the stream that moment is: the source itself, or its place in the rendered clip
        t0 = at if direct_ok(opts, self.overlays, self.src) else effects.out_time(at - s, e - s, {**effects.DEFAULT, **opts})
        self.spinner.start()
        self.play_btn.set_icon_name("media-playback-pause-symbolic")
        self.req += 1
        req = self.play_req = self.play_req + 1
        # Nothing to bake (no look/effect/speed/direction/overlay): play the source itself — instant, hardware-
        # decoded by GStreamer, no ffmpeg at all. The tick loops it inside the selection.
        direct = direct_ok(opts, self.overlays, self.src)

        def work():
            with thumbs.BAKE_LOCK:
                if req != self.play_req:  # superseded (stopped or re-baked) while queued: don't render
                    return
                try:
                    path = self._preview_path(s, e, opts)
                except Exception:
                    path = None
            GLib.idle_add(start, path)

        def start(path):
            if req != self.play_req:  # stale: a newer play (or a stop) owns the UI now
                return
            self.spinner.stop()
            if not path:
                return self.stop()
            if not direct:
                self.proxies.add(path)  # cleaned up on close — never the source itself
            self.ov_layer_on = False
            self.layer.set_visible(False)
            st, self.warm = (self.warm, None) if direct and self.warm else (media_file(path, muted=False, loop=True), self.warm)
            self.stream, self.direct, self.shown = st, direct, False

            def first_frame(st):
                if t0 > 0.5 and (st.is_seeking() or st.get_timestamp() / 1e6 < t0 - 0.5):
                    return  # still on frame 0, before the seek to the start point landed
                st.disconnect(hid)
                self.shown = True
                self.pic.set_paintable(st)
            hid = st.connect("invalidate-contents", first_frame)
            if st.is_prepared():
                if direct or t0 > 0.01:
                    st.seek(int(t0 * 1e6))  # a warm stream sits wherever the last play stopped
            elif t0 > 0.01:
                st.connect("notify::prepared", lambda st, _: st.is_prepared() and st.seek(int(t0 * 1e6)))
            st.play()
            self.tick = GLib.timeout_add(40, self._on_tick, s, e - s, opts, direct)
        if direct:
            return start(self.src)
        threading.Thread(target=work, daemon=True).start()

    def _on_tick(self, s, length, opts, direct=False):
        if not self.stream:
            self.tick = 0
            return False
        st = self.stream
        if not self.shown:
            return True  # the playhead stays put until the first frame at the start point is up
        if direct:
            ts = st.get_timestamp() / 1e6
            if st.is_prepared() and not st.is_seeking() and (ts >= s + length or ts < s - 0.25):
                st.seek(int(s * 1e6))  # loop the selection
            self.tl.p = min(max(ts, s), s + length)
        else:
            self.tl.p = s + effects.src_time(st.get_timestamp() / 1e6, length, opts)
        self.tl.queue_draw()
        self._update_label()
        return True

    def stop(self):
        self._try_done()
        self.req += 1
        self.play_req += 1
        self.spinner.stop()
        self.play_btn.set_icon_name("media-playback-start-symbolic")
        if self.tick:
            GLib.source_remove(self.tick)
            self.tick = 0
        if self.stream:
            if self.direct and not self.warm:
                self.stream.set_playing(False)
                self.warm = self.stream
            else:
                release(self.stream)
            self.stream = None
            self._show_frame(self.tl.p)
        self.ov_layer_on = True
        self.layer.set_visible(bool(self.overlays))

    def _cleanup(self):
        self.stop()
        if self.warm:
            self.warm.clear()
            self.warm = None
        for p in self.proxies:
            try:
                os.unlink(p)
            except OSError:
                pass

    # --- edits ----------------------------------------------------------------------------------
    def _changed(self, _tl, what):
        if what in ("start", "end", "move"):  # a trimmed selection parks the playhead at its start...
            self.tl.p = self.tl.s
        if self.stream:  # keep the preview running and just re-bake it over the new selection
            self._rebake()
        else:
            self.stop()
            # ...and every other move (scrub, bookmark jump) previews what the playhead points at
            self._show_frame(self.tl.k if what == "cover" else self.tl.p)
        self._update_label()

    def _update_label(self):
        self.time_lbl.set_label(f"{fmt(self.tl.p)} / {fmt(self.D, False)}")

    def _set_length(self, n):
        s = min(self.tl.s, max(0.0, self.D - n))
        self.tl.set_selection(s, min(self.D, s + n), "move")
        self.tl.zoom_to_selection()

    def _start_at_playhead(self):
        length = self.tl.e - self.tl.s
        s = min(self.tl.p, max(0.0, self.D - length))
        self.tl.k += s - self.tl.s
        self.tl.set_selection(s, s + length, "move")

    def _cover_at_playhead(self):
        if not (self.tl.s <= self.tl.p <= self.tl.e):
            self._start_at_playhead()
        self.tl.k = self.tl.p
        self.tl.queue_draw()
        self._show_frame(self.tl.k)

    def _key(self, _c, keyval, _code, state):
        fps_step = 1.0 if state & Gdk.ModifierType.SHIFT_MASK else 1 / 24
        if keyval == Gdk.KEY_space:
            self.toggle_play()
        elif keyval == Gdk.KEY_b:
            self.tl.toggle_mark()
        elif keyval in (Gdk.KEY_comma, Gdk.KEY_period):
            if not self.tl.jump_mark(1 if keyval == Gdk.KEY_period else -1):
                return False
            if not self.tl.v0 <= self.tl.p <= self.tl.v1:
                self.tl.set_view(self.tl.p - (self.tl.v1 - self.tl.v0) / 2, self.tl.v1 - self.tl.v0)
        elif keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            self.stop()
            self.tl.p = min(max(0.0, self.tl.p + (fps_step if keyval == Gdk.KEY_Right else -fps_step)), self.D)
            if not self.tl.v0 <= self.tl.p <= self.tl.v1:
                self.tl.set_view(self.tl.p - (self.tl.v1 - self.tl.v0) / 2, self.tl.v1 - self.tl.v0)
            self.tl.queue_draw()
            self._show_frame(self.tl.p)
            self._update_label()
        else:
            return False
        return True

    # --- collage + GIF/WebP export ---------------------------------------------------------------
    def _collage(self):
        from .collage_page import CollagePage  # collage_page imports this module
        self.stop()
        page = CollagePage(self.on_saved)
        page.fill_all(self.src, self.tl.s, self.tl.e - self.tl.s, self.opts, self.style)
        self.get_ancestor(Adw.NavigationView).push(page)

    def _export(self, fmt):
        """GIF/WebP of the clip exactly as it would be saved (looks, effects, overlays)."""
        self.stop()
        a, b, k, opts = self.tl.s, self.tl.e, self.tl.k, dict(self.opts)
        ovs = [dict(o) for o in self.overlays]
        export.save_as(self, fmt, self.src, lambda tmp: livemake.make_live(
            self.src, a, b, k - a, out_dir=tmp, opts=opts, overlay_list=ovs)[1], self._busy)

    def _busy(self, on):
        self.save_btn.set_sensitive(not on)
        self.save_btn.set_label("Exporting…" if on else self.SAVE_LABEL)

    # --- save -----------------------------------------------------------------------------------
    def _save(self, _b):
        self.stop()
        self.save_btn.set_sensitive(False)
        self.save_btn.set_label("Saving…")
        self.progress.begin()
        s, e, k, opts, style, n = self.tl.s, self.tl.e, self.tl.k, dict(self.opts), self.style, self.repeat
        ovs = [dict(o) for o in self.overlays]
        col = self.collection_row.collection()
        smooth = self.interp_group.settings()

        def work():
            try:
                # straight into the collection folder when one is picked: one copy on disk, All reads the subfolders
                res, err = livemake.make_live(self.src, s, e, k - s, out_dir=col.path if col else None, opts=opts,
                                              overlay_list=ovs, repeat=n, interpolate=smooth,
                                              progress=self.progress.report), None
                library.set_meta(res[0], transition=style)
            except thumbs.Cancelled:  # the user stopped it: make_live removed its partial files
                thumbs.log("save cancelled")
                res, err = None, None
            except Exception as ex:  # surfaced as a toast
                res, err = None, str(ex)
            GLib.idle_add(self.on_saved, self, res, err)
        threading.Thread(target=work, daemon=True).start()
