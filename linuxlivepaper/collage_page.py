"""Collage maker: several videos/GIFs/live images playing at once in a layout, each with its own start,
volume, speed, direction, filter and effect, saved as one live image.

Length: one pass lasts as long as the longest cell; shorter cells loop. "Repeat" plays the pass n times in the
saved file. Performance: stills come from single-frame grabs; Play / Try it render a 540p collage whose cell passes
are cached (thumbs.CACHE/collage/cells), so editing one cell only re-renders that cell."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk, Pango  # noqa: E402

from . import collage, effects, export, interp, library, livemake, overlays, thumbs  # noqa: E402
from .editor import Timeline  # noqa: E402
from .overlay_ui import OverlayHost  # noqa: E402
from .playback import DEFAULT_STYLE, STYLES, LiveCanvas, media_file, release  # noqa: E402
from .widgets import CollectionRow, FileBrowser, HoldButton, InterpGroup, preview_quality_dropdown  # noqa: E402

PROXY_H = 360
SYNC_TOL = 0.2  # seconds a live cell may be off the shared collage clock before it is seeked back
# Stills are grabbed uncropped (look + effect applied to the whole frame) and cropped at draw time, so dragging and
# scroll-zooming a cell are pure GPU redraws. Effects whose result depends on the crop get an exact cropped grab.
CROP_EFFECTS = {"mirror"}
pool = ThreadPoolExecutor(max_workers=max(2, min(6, (os.cpu_count() or 4) // 2)))


def rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def rgba(s):
    c = Gdk.RGBA()
    c.parse(s)
    return c


ACCENT = rgba("#3584e4")


class LayoutIcon(Gtk.DrawingArea):
    def __init__(self, layout):
        super().__init__(content_width=30, content_height=40)
        self.set_draw_func(lambda _a, cr, w, h: self._draw(cr, w, h, layout))

    @staticmethod
    def _draw(cr, w, h, layout):
        for x, y, cw, ch in collage.LAYOUTS[layout][1]:
            cr.rectangle(x * w + 1.5, y * h + 1.5, cw * w - 3, ch * h - 3)
        cr.set_source_rgba(1, 1, 1, 0.75)
        cr.fill()


class CollageCanvas(Gtk.Widget):
    """Draws the collage: the playing render across the whole canvas, else each cell's still cover-cropped into its
    rect. Dragging a cell pans it inside its frame, scrolling zooms it — both clamped so no border ever shows."""

    def __init__(self, page):
        super().__init__(hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN)
        self.page = page
        self._off = None  # drag offsets below the motion threshold are dropped: the picture never jumps at start
        self._pan0 = (0.5, 0.5)
        click = Gtk.GestureClick()
        click.connect("pressed", self._pressed)
        click.connect("released", self._released)
        self.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", lambda *_a: self._end())
        self.add_controller(drag)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)
        self.set_cursor_from_name("grab")

    def frame(self):
        """(x, y, scale) of the canvas fitted inside the widget."""
        _, W, H = collage.ASPECTS[self.page.c["aspect"]]
        s = min(self.get_width() / W, self.get_height() / H)
        return (self.get_width() - W * s) / 2, (self.get_height() - H * s) / 2, s

    def cell_at(self, x, y):
        ox, oy, s = self.frame()
        _, W, H = collage.ASPECTS[self.page.c["aspect"]]
        return collage.cell_at(self.page.c, (x - ox) / (W * s), (y - oy) / (H * s))

    def slack(self, i):
        """(slack_x, slack_y) in canvas px: how far the cell's picture can still slide without showing a border."""
        c = self.page.c
        _, W, H = collage.ASPECTS[c["aspect"]]
        _, _, w, h = collage.rects(c, W, H)[i]
        zw, zh = collage.fit_size(c["cells"][i], w, h)
        return max(1.0, zw - w), max(1.0, zh - h)

    def _pressed(self, _g, _n, x, y):
        """A plain click selects the cell under the pointer."""
        i = self.cell_at(x, y)
        if i is not None:
            self.page.select(i)

    def _released(self, _g, n, x, y):
        """Tap an empty cell (no drag) to pick a clip for it."""
        i = self.cell_at(x, y)
        if n == 1 and i is not None and not self.page.c["cells"][i]:
            self.page.select(i, open_empty=True)

    def _begin(self, _g, x, y):
        self._off = None
        i = self.cell_at(x, y)
        cell = self.page.c["cells"][i] if i is not None else None
        if self.page.playing:
            self.page.stop()  # playback is a baked clip: drop back to the stills so the drag pans something
        if i is not None:
            self.page.select(i, quiet=True)  # highlight now, rebuild the panel on release: no hitch in the drag
        self.page.drag_cell = i if cell else None
        self._pan0 = collage.clamp_pan(cell) if cell else (0.5, 0.5)
        if cell:
            self.set_cursor_from_name("grabbing")

    def _update(self, _g, ox, oy):
        i = self.page.drag_cell
        cell = self.page.c["cells"][i] if i is not None else None
        if not cell:
            return
        if self._off is None:  # first report is already past the drag threshold: treat it as the origin
            self._off = (ox, oy)
            return
        self._off = (ox, oy)
        _, _, s = self.frame()
        sx, sy = self.slack(i)
        nx = min(1.0, max(0.0, self._pan0[0] - ox / (s * sx)))
        ny = min(1.0, max(0.0, self._pan0[1] - oy / (s * sy)))
        cell["pan"] = [nx, ny]
        # what the clamp let through, in widget px — a cropped still follows the cursor and stops at the edge
        self.page.drag_move = (i, (self._pan0[0] - nx) * sx * s, (self._pan0[1] - ny) * sy * s)
        self.queue_draw()

    def _end(self):
        self.set_cursor_from_name("grab")
        i, self.page.drag_cell = self.page.drag_cell, None
        self.page.drag_move = None
        if i is not None:
            if self.page.cropped[i]:  # only crop-dependent effects need a fresh exact still
                self.page.refresh([i])
            self.queue_draw()
            self.page.select(self.page.sel)  # catches up the side panel a quiet drag skipped

    def _scroll(self, _c, _dx, dy):
        cell = self.page.c["cells"][self.page.sel]
        if not cell:
            return False
        if self.page.playing:
            self.page.stop()
        cell["zoom"] = min(4.0, max(1.0, float(cell.get("zoom") or 1.0) * (0.9 if dy > 0 else 1.1)))
        self.queue_draw()  # instant: the uncropped still is re-framed on the GPU
        self.page.refresh_later([self.page.sel])  # then sharpen it once the wheel rests
        self.page.sync_zoom()
        return True

    def _draw_cell(self, snap, i, r, p, s, cropped):
        """Cover-fit the still into r. Uncropped stills follow the cell's live zoom/pan (dragging = redraw only)."""
        pa = p.get_intrinsic_aspect_ratio() or 1
        x, y, w, h = r.get_x(), r.get_y(), r.get_width(), r.get_height()
        cell = self.page.c["cells"][i]
        if cropped or not cell:
            cw, ch = (w, w / pa) if pa < w / h else (h * pa, h)
            dx = dy = 0.0
            if self.page.drag_move and self.page.drag_move[0] == i:  # cropped still: slide it until the regrab
                dx, dy = self.page.drag_move[1], self.page.drag_move[2]
            ox, oy = x + (w - cw) / 2 + dx, y + (h - ch) / 2 + dy
        else:  # same maths as collage.cell_fit: cover-scale to the zoomed size, crop at pan × slack
            zw, zh = collage.fit_size(cell, w / s, h / s)
            cw, ch = max(zw, zh * pa) * s, max(zh, zw / pa) * s
            px, py = collage.clamp_pan(cell)
            ox, oy = x - (cw - w) * px, y - (ch - h) * py
        snap.save()
        snap.translate(Graphene.Point().init(ox, oy))
        p.snapshot(snap, cw, ch)
        snap.restore()

    def do_snapshot(self, snap):
        c = self.page.c
        ox, oy, s = self.frame()
        _, W, H = collage.ASPECTS[c["aspect"]]
        snap.append_color(rgba(c["bg"]), rect(ox, oy, W * s, H * s))
        for i, (x, y, w, h) in enumerate(collage.rects(c, W, H)):
            r = rect(ox + x * s, oy + y * s, w * s, h * s)
            live = self.page.live_paint(i)
            p, crop = live or (self.page.paint[i], self.page.cropped[i])
            snap.push_clip(r)
            if p and p.get_intrinsic_width() > 0:
                self._draw_cell(snap, i, r, p, s, crop)
            else:
                snap.append_color(rgba("#3a3a3e" if c["bg"] == "black" else "#d0d0d4"), r)
                lay = self.create_pango_layout("+ Add video" if not c["cells"][i] else "…")
                lay.set_font_description(Pango.FontDescription.from_string("Sans Bold 11"))
                tw, th = lay.get_pixel_size()
                snap.save()
                snap.translate(Graphene.Point().init(r.get_x() + (r.get_width() - tw) / 2,
                                                     r.get_y() + (r.get_height() - th) / 2))
                snap.append_layout(lay, rgba("#ffffffb0" if c["bg"] == "black" else "#000000a0"))
                snap.restore()
            snap.pop()
            if i == self.page.drop_cell:  # a file is being dragged over this cell
                snap.append_color(rgba("#3584e466"), r)
            if i == self.page.sel or i == self.page.drop_cell:
                x, y, w, h, b = r.get_x(), r.get_y(), r.get_width(), r.get_height(), 3
                for e in (rect(x, y, w, b), rect(x, y + h - b, w, b), rect(x, y, b, h), rect(x + w - b, y, b, h)):
                    snap.append_color(ACCENT, e)


class CollagePage(OverlayHost, Adw.NavigationPage):
    SAVE_LABEL = "Save Live Collage"

    def __init__(self, on_saved):
        super().__init__(title="Create Collage")
        self.on_saved = on_saved
        self.c = collage.new("2h", "9:16")
        self.paint = [None] * len(self.c["cells"])
        self.cropped = [False] * len(self.c["cells"])
        self.want = [None] * len(self.c["cells"])
        self._later_id, self._later = 0, set()
        self.sel = 0
        self.style = DEFAULT_STYLE
        self.playing, self.gen, self.tl = False, 0, None
        self.drag_cell, self.drag_move, self._rebake_id = None, None, 0
        self._panel_stale = False
        self.zoom_scale, self.framing_lbl, self._zoom_sync = None, None, False
        self.tick, self._resync = 0, set()
        self.live = [None] * 9  # per cell while playing: {"spec", "stream", "shown", "win", "old"}
        self.clock0 = None  # shared collage clock while playing (monotonic µs at collage time 0)
        self.bake_tok, self.pending = [0] * 9, set()
        self.at_start = False  # after a pause every cell shows the first frame of its window, in sync
        self.canvas_live = None
        self.canvas_widget, self.trying, self._try_gen = None, False, 0

        pool.submit(lambda: [thumbs.prune(thumbs.CACHE / d, 1.5e9) for d in ("collage", "previews")]
                    + [thumbs.nvenc_available()])  # warm the encoder probe so the first Play doesn't pay for it
        self.canvas = CollageCanvas(self)
        self.spinner = Gtk.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=32,
                                   height_request=32)
        stage = Gtk.Overlay(child=self.canvas, css_classes=["viewer", "card"], overflow=Gtk.Overflow.HIDDEN,
                            hexpand=True)
        self.ov_init()
        stage.add_overlay(self.layer)
        stage.add_overlay(self.spinner)
        self.stage = stage

        self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play (Space)",
                                   css_classes=["circular", "suggested-action"])
        self.play_btn.connect("clicked", lambda *_: self.toggle_play())
        self.clip_lbl = Gtk.Label(css_classes=["dim-label"], xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        controls = Gtk.Box(spacing=8)
        controls.append(self.play_btn)
        controls.append(self.clip_lbl)
        self.tl_box = Gtk.Box()
        hint = Gtk.Label(wrap=True, css_classes=["dim-label", "caption"], label=(
            "Click a cell to select it (or fill it) · drag the yellow window on the timeline to choose which part of "
            "the selected clip plays · every cell has its own volume, speed, direction, filter and effect"))

        menu = Gio.Menu()  # same split button + export flow as the single-clip editor
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
        header = Adw.HeaderBar()
        header.pack_end(self.save_btn)
        header.pack_start(preview_quality_dropdown())
        self.play_btn.set_tooltip_text("Play the collage (Space)")

        # the scroller caps the panel's natural width so the stage gets the spare room
        self.panel_box = Gtk.ScrolledWindow(width_request=360, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC, hexpand=False, vexpand=True)
        # left edge: an arrow that slides out a folder browser; tiles click into the selected cell or drag onto any
        self.browser = None
        self.browse_btn = Gtk.ToggleButton(icon_name="go-next-symbolic", tooltip_text="Browse files (drag onto a cell)",
                                           css_classes=["flat", "browse-handle"], valign=Gtk.Align.CENTER)
        self.browse = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT, transition_duration=220,
                                  hexpand=False)  # keep the stage the one that grows
        self.browse_btn.connect("toggled", self._toggle_browser)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("motion", self._drop_motion)
        drop.connect("leave", lambda *_: self._drop_hover(None))
        drop.connect("drop", self._dropped)
        stage.add_controller(drop)
        self.drop_cell = None
        top = Gtk.Box(spacing=12, vexpand=True)
        top.append(self.browse_btn)
        top.append(self.browse)
        top.append(stage)
        top.append(self.panel_box)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18, margin_end=18,
                      margin_top=12, margin_bottom=18)
        for wdg in (top, controls, self.tl_box, hint):
            box.append(wdg)
        tv = Adw.ToolbarView(content=box)
        tv.add_top_bar(header)
        self.set_child(tv)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, kv, *_: kv == Gdk.KEY_space and (self.toggle_play() or True))
        self.add_controller(keys)
        self.connect("hidden", lambda *_: self.stop())
        self._rebuild_panel()
        self._rebuild_timeline()

    # --- panel ----------------------------------------------------------------------------------
    @staticmethod
    def _combo(title, choices, current, on_change, subtitle=None):
        ids = [k for k, _ in choices]
        row = Adw.ComboRow(title=title, subtitle=subtitle, model=Gtk.StringList.new([v for _, v in choices]),
                           selected=ids.index(current) if current in ids else 0)
        row.connect("notify::selected", lambda r, _: on_change(ids[r.get_selected()]))
        return row

    def _rebuild_panel(self):
        old = getattr(self, "panel_page", None)
        col = old.collection_row.collection() if old else None
        self.zoom_scale = self.framing_lbl = None
        keep = CollectionRow(select=col.name if col else None)
        page = Adw.PreferencesPage(width_request=360)
        c = self.c

        layout = Adw.PreferencesGroup(title="Layout")
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5, min_children_per_line=5,
                           column_spacing=4, row_spacing=4, homogeneous=True)
        for lid, (label, _) in collage.LAYOUTS.items():
            b = Gtk.ToggleButton(child=LayoutIcon(lid), tooltip_text=label, active=lid == c["layout"])
            b.connect("clicked", lambda _b, x=lid: self._set_layout(x))
            flow.append(b)
        layout.add(flow)
        page.add(layout)

        canvas = Adw.PreferencesGroup(title="Canvas")
        canvas.add(self._combo("Shape", [(k, v[0]) for k, v in collage.ASPECTS.items()], c["aspect"],
                               lambda v: self._set("aspect", v)))
        canvas.add(self._combo("Borders", list(collage.GAPS.items()), c["gap"], lambda v: self._set("gap", v)))
        canvas.add(self._combo("Border color", list(collage.BACKGROUNDS.items()), c["bg"],
                               lambda v: self._set("bg", v)))
        page.add(canvas)

        timing = Adw.PreferencesGroup(title="Length")
        self.len_row = Adw.ActionRow(title="One pass", tooltip_text="The longest cell sets the length; shorter cells "
                                                                    "loop to fill it")
        timing.add(self.len_row)
        rp = Adw.SpinRow.new_with_range(1, max(collage.REPEATS), 1)
        rp.set_title("Repeat")
        rp.set_subtitle("Times the collage plays in the saved file")
        rp.set_value(c["repeat"])
        rp.connect("notify::value", lambda r, _: self._set_repeat(int(r.get_value())))
        timing.add(rp)
        match = Adw.ActionRow(title="Match clip lengths", subtitle="Resize every clip's window to the longest or "
                                                                    "shortest one")
        for label, longest, tip in (("Longest", True, "Stretch shorter clips to the longest clip"),
                                    ("Shortest", False, "Trim longer clips to the shortest clip")):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER, tooltip_text=tip)
            b.connect("clicked", lambda _b, lg=longest: self._match(lg))
            match.add_suffix(b)
        timing.add(match)
        page.add(timing)

        cell = c["cells"][self.sel]
        grp = Adw.PreferencesGroup(title=f"Cell {self.sel + 1}",
                                   description=None if cell else "Click the cell or Choose… to add a video, GIF or live image.")
        src_row = Adw.ActionRow(title=Path(cell["src"]).name if cell else "Empty",
                                subtitle="Clip" if cell else None, title_lines=1)
        choose = Gtk.Button(label="Replace…" if cell else "Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *_: self._choose(self.sel))
        src_row.add_suffix(choose)
        if cell:
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                            tooltip_text="Remove this clip")
            rm.connect("clicked", lambda *_: self._remove(self.sel))
            src_row.add_suffix(rm)
        grp.add(src_row)
        if cell:
            vol = Adw.ActionRow(title="Volume")
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.05)
            scale.set_value(cell["volume"])
            scale.set_hexpand(True)
            scale.set_size_request(150, -1)
            scale.connect("value-changed", lambda s: self._set_volume(s.get_value()))
            mute = Gtk.Button(icon_name="audio-volume-muted-symbolic" if cell["volume"] == 0 else
                              "audio-volume-high-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                              tooltip_text="Mute / unmute")
            mute.connect("clicked", lambda *_: scale.set_value(0 if scale.get_value() else 1))
            scale.connect("value-changed", lambda s: mute.set_icon_name(
                "audio-volume-muted-symbolic" if s.get_value() == 0 else "audio-volume-high-symbolic"))
            vol.add_suffix(scale)
            vol.add_suffix(mute)
            grp.add(vol)
            o = cell["opts"]
            grp.add(self._combo("Speed", [(v, f"{v:g}×") for v in effects.SPEEDS], o["speed"],
                                lambda v: self._set_opt("speed", v)))
            grp.add(self._combo("Direction", list(effects.DIRECTIONS.items()), o["direction"],
                                lambda v: self._set_opt("direction", v)))
            grp.add(self._combo("Filter", [(k, v[0]) for k, v in effects.LOOKS.items()], o["look"],
                                lambda v: self._set_opt("look", v)))
            grp.add(self._combo("Effect", [(k, v[0]) for k, v in effects.EFFECTS.items()], o["effect"],
                                lambda v: self._set_opt("effect", v)))
            self.framing_lbl = Adw.ActionRow(title="Framing", subtitle=f"Zoom {float(cell.get('zoom') or 1):.2f}×",
                                             tooltip_text="Drag the picture in the preview to move it, scroll to zoom")
            z = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 4, 0.05)
            z.set_value(float(cell.get("zoom") or 1))
            z.set_hexpand(True)
            z.set_size_request(120, -1)
            z.connect("value-changed", lambda s: self._set_zoom(s.get_value()))
            self.zoom_scale = z
            self.framing_lbl.add_suffix(z)
            reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                               tooltip_text="Reset framing")
            reset.connect("clicked", lambda *_: self._reset_framing())
            self.framing_lbl.add_suffix(reset)
            grp.add(self.framing_lbl)
        page.add(grp)
        if cell:
            win = Adw.ActionRow(title="Window")
            self.win_row = win
            fit = Gtk.Button(label="Fit", valign=Gtk.Align.CENTER, tooltip_text="Play the whole clip")
            fit.connect("clicked", lambda *_: self._fit_window())
            win.add_suffix(fit)
            grp.add(win)
        self.ov_regroup()
        page.add(self.overlay_group)

        live = Adw.PreferencesGroup(title="Live image")
        cover = Adw.ActionRow(title="Cover", subtitle="Moment used as the still")
        D = collage.duration(c)
        cs = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, D, 0.05)
        cs.set_value(min(c["cover"], D))
        self.cover_scale = cs
        cs.set_hexpand(True)
        cs.set_size_request(150, -1)
        cs.connect("value-changed", lambda s: self._set_cover(s.get_value()))
        cover.add_suffix(cs)
        live.add(cover)
        live.add(self._combo("Press effect", [(k, v[0]) for k, v in STYLES.items()], self.style,
                             lambda v: setattr(self, "style", v)))
        try_row = Adw.ActionRow(title="Try it", subtitle="Press and hold to see the collage as it will be saved")
        self.try_btn = HoldButton(label="Hold", on_press=self._try_press, on_release=self._try_release)
        try_row.add_suffix(self.try_btn)
        live.add(try_row)
        live.add(keep)
        page.add(live)
        page.interp_group = InterpGroup()
        page.add(page.interp_group)
        page.collection_row = keep
        self.panel_box.set_child(page)
        self.panel_page = page
        self._length_changed()

    def _length_changed(self):
        """Cells, windows, speed and bounce all move the collage length: keep the rows that show it honest."""
        c, D = self.c, collage.duration(self.c)
        n = c["repeat"]
        self.len_row.set_subtitle(f"{D:.2f} s" + (f" × {n} = {D * n:.2f} s saved" if n > 1 else "")
                                  + " · longest clip, shorter ones loop")
        cell = c["cells"][self.sel]
        if cell and getattr(self, "win_row", None):
            self.win_row.set_subtitle(f"{collage.window(cell):.2f} s of the clip"
                                      + ("" if cell.get("len") is None else " (custom)"))
        cs = self.cover_scale
        if abs(cs.get_adjustment().get_upper() - D) > 1e-3:
            cs.set_range(0, D)
            cs.set_value(min(c["cover"], D))

    # --- edits ----------------------------------------------------------------------------------
    def _set(self, key, value):
        if self.c[key] == value:
            return
        self.c[key] = value
        self.canvas.queue_draw()
        self.refresh(all_cells=True)  # aspect/gaps/background change every cell's framing

    def _match(self, longest):
        self.stop()
        collage.match(self.c, longest)
        self._rebuild_timeline()
        self._rebuild_panel()
        self.refresh(all_cells=True)

    def _set_repeat(self, n):
        self.c["repeat"] = n  # only the saved file / Try it repeat; Play loops anyway
        self._length_changed()

    def fill_all(self, src, start, length, opts, style):
        """Grid of 4 with the same clip (trimmed to start/length, same look) in every cell."""
        self._set_layout("4")
        for i in range(len(self.c["cells"])):
            self.c["cells"][i] = {**collage.new_cell(src), "start": start, "len": length, "opts": dict(opts)}
        self.style, self.sel = style, 0
        self.paint, self.want = [None] * 4, [None] * 4
        self._rebuild_panel()
        self._rebuild_timeline()
        self.refresh(all_cells=True)

    def _set_layout(self, layout):
        if layout != self.c["layout"]:
            self.stop()
            collage.set_layout(self.c, layout)
            n = len(self.c["cells"])
            self.paint = (self.paint + [None] * n)[:n]
            self.cropped = (self.cropped + [False] * n)[:n]
            self.want = [None] * n
            self.sel = min(self.sel, n - 1)
            self.canvas.queue_draw()
            self._rebuild_timeline()
        self._rebuild_panel()  # also resets the toggle states

    def select(self, i, open_empty=False, quiet=False):
        """Select cell i. quiet: only move the highlight — the panel and timeline catch up on the next plain select
        (a drag calls this on every press, so rebuilding them there is what made dragging feel rough)."""
        if open_empty and not self.c["cells"][i]:
            self.sel = i
            self._panel_stale = True
            self._choose(i)
        elif i != self.sel:
            self.sel = i
            if quiet:
                self._panel_stale = True
            else:
                self._rebuild_panel()
                self._rebuild_timeline()
        elif self._panel_stale and not quiet:
            self._panel_stale = False
            self._rebuild_panel()
            self._rebuild_timeline()
        self.canvas.queue_draw()

    def _choose(self, i):
        f = Gtk.FileFilter(name="Videos, GIFs and live images")
        for m in ("video/*", "image/gif"):
            f.add_mime_type(m)
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.append(f)
        dlg = Gtk.FileDialog(title="Choose a clip", filters=filters,
                             initial_folder=Gio.File.new_for_path(str(library.library_dir())))

        def picked(d, res):
            try:
                path = d.open_finish(res).get_path()
            except GLib.Error:
                return
            self.set_clip(i, path)
        dlg.open(self.get_root(), None, picked)

    # --- file browser + drag and drop ------------------------------------------------------------
    def _toggle_browser(self, b):
        if b.get_active() and not self.browser:  # built on first open: scanning a folder isn't free
            self.browser = FileBrowser(library.library_dir(), self._browser_pick)
            self.browse.set_child(self.browser)
        self.browse.set_reveal_child(b.get_active())
        b.set_icon_name("go-previous-symbolic" if b.get_active() else "go-next-symbolic")

    def _browser_pick(self, path):
        cells = self.c["cells"]
        i = self.sel if not cells[self.sel] else next((k for k, x in enumerate(cells) if not x), self.sel)
        self.set_clip(i, path)
        self.canvas.queue_draw()

    def _drop_hover(self, i):
        if i != self.drop_cell:
            self.drop_cell = i
            self.canvas.queue_draw()

    def _drop_motion(self, _t, x, y):
        self._drop_hover(self.canvas.cell_at(x, y))
        return Gdk.DragAction.COPY if self.drop_cell is not None else 0

    def _dropped(self, _t, files, x, y):
        """Files from the browser or a file manager: the first fills the cell under the pointer, the rest fill the
        following empty cells. Live stills bring their video; anything else unplayable is ignored."""
        self._drop_hover(None)
        i = self.canvas.cell_at(x, y)
        clips = [m for m in (library.motion_for(f.get_path()) for f in files.get_files() if f.get_path()) if m]
        if i is None or not clips:
            return False
        n = len(self.c["cells"])
        targets = [i] + [k for k in list(range(i + 1, n)) + list(range(i)) if not self.c["cells"][k]]
        for k, path in zip(targets, clips):
            self.set_clip(k, path)
        self.select(i)
        return True

    def set_clip(self, i, path):
        self.stop()
        old = self.c["cells"][i]
        cell = collage.new_cell(path)
        if old:  # keep the cell's look when replacing the clip
            cell.update(volume=old["volume"], opts=old["opts"])
        self.c["cells"][i] = cell
        self.sel = i
        self.paint[i], self.want[i] = None, None
        self._rebuild_panel()
        self._rebuild_timeline()
        self.refresh([i])

    def _remove(self, i):
        self.stop()
        self.c["cells"][i] = None
        self.paint[i], self.want[i] = None, None
        self._rebuild_panel()
        self._rebuild_timeline()
        self.canvas.queue_draw()

    def _set_volume(self, v):
        cell = self.c["cells"][self.sel]
        cell["volume"] = round(v, 2)
        self._apply_volume(self.sel)

    def _set_opt(self, key, value):
        cell = self.c["cells"][self.sel]
        if cell["opts"][key] == value:
            return
        cell["opts"][key] = value
        self.refresh([self.sel])

    def _set_zoom(self, v):
        cell = self.c["cells"][self.sel]
        if not cell or getattr(self, "_zoom_sync", False):
            return
        cell["zoom"] = round(v, 2)
        self.canvas.queue_draw()
        self.refresh_later([self.sel])
        self._rebuild_panel_if_framing()

    def _reset_framing(self):
        cell = self.c["cells"][self.sel]
        if not cell:
            return
        cell["zoom"], cell["pan"] = 1.0, [0.5, 0.5]
        self.canvas.queue_draw()
        self.refresh([self.sel])
        self._rebuild_panel()

    # --- canvas hooks: drag on empty space pans the cell under it ---------------------------------
    def ov_pan(self, x, y, dx, dy):
        """A drag on the overlay layer that missed every overlay: pan the collage cell under it."""
        cv = self.canvas
        if not (dx or dy):
            cv._begin(None, x, y)
            return
        ox, oy = cv._off or (0.0, 0.0)
        cv._update(None, ox + dx, oy + dy)

    def ov_pan_end(self):
        self.canvas._end()

    def _set_cover(self, t):
        self.c["cover"] = t
        self.at_start = False
        if not self.playing:
            self.refresh(all_cells=True)

    # --- timeline for the selected cell ---------------------------------------------------------
    def _rebuild_timeline(self):
        if self.tl:
            self.tl_box.remove(self.tl)
            self.tl = None
        cell = self.c["cells"][self.sel]
        if not cell:
            self.clip_lbl.set_label(f"Cell {self.sel + 1} is empty")
            return
        D = cell["dur"]
        self.tl = Timeline(cell["src"], D, cell.get("ar") or 16 / 9, fixed=True)
        need = collage.window(cell)
        cell["start"] = min(cell["start"], max(0.0, D - min(need, D)))
        self.tl.s, self.tl.e = cell["start"], min(D, cell["start"] + collage.window(cell))
        self.tl.p = cell["start"]
        if D > 60:
            self.tl.zoom_to_selection()
        self.tl.connect("changed", self._tl_changed)
        self.tl_box.append(self.tl)
        self._label()

    def _label(self):
        cell = self.c["cells"][self.sel]
        if cell:
            self.clip_lbl.set_label(f"Cell {self.sel + 1}: {Path(cell['src']).name} · "
                                    f"{cell['start']:.2f}–{cell['start'] + collage.window(cell):.2f} s")

    def _tl_changed(self, tl, what):
        """The yellow window is both movable and stretchable; it is what the cell plays (and loops)."""
        if what not in ("move", "start", "end"):
            return
        cell = self.c["cells"][self.sel]
        length = round(tl.e - tl.s, 3)
        auto = abs(length - min(cell["dur"] - tl.s, collage.MAX_WINDOW)) < 1e-3
        if abs(cell["start"] - tl.s) < 1e-3 and (auto and cell.get("len") is None
                                                 or abs(length - (cell.get("len") or 0)) < 1e-3):
            return
        cell["start"], cell["len"] = round(tl.s, 3), None if auto else length
        self._label()
        self.refresh([self.sel])

    def _fit_window(self):
        cell = self.c["cells"][self.sel]
        cell["len"] = None
        self._rebuild_timeline()
        self._rebuild_panel()
        self.refresh([self.sel])

    # --- overlays + try it ----------------------------------------------------------------------
    def ov_rect(self):
        _, W, H = collage.ASPECTS[self.c["aspect"]]
        w, h = self.canvas.get_width(), self.canvas.get_height()
        s = min(w / W, h / H) if w and h else 0.0
        return (w - W * s) / 2, (h - H * s) / 2, W * s, H * s

    def ov_duration(self):
        return collage.duration(self.c)

    def ov_click(self, x, y):
        ox, oy, vw, vh = self.ov_rect()
        if vw <= 0:
            return
        i = collage.cell_at(self.c, (x - ox) / vw, (y - oy) / vh)
        if i is not None:
            self.select(i, open_empty=True)

    def _collage_copy(self):
        """A detached snapshot of what is on screen, for a render thread."""
        return {**self.c, "cells": [dict(x) if x else None for x in self.c["cells"]],
                "overlays": [dict(o) for o in self.overlays]}

    def _try_path(self, c, repeat=1):
        """Cached low-res render of the whole collage: Play loops one pass, the press preview plays all repeats."""
        import hashlib
        height = thumbs.preview_height()
        key = hashlib.sha1(repr([[
            ([[k, v] for k, v in sorted(x.items()) if k not in ("dur", "ar")] if x else None) for x in c["cells"]],
            c["layout"], c["aspect"], repeat, c["gap"], c["bg"],
            [overlays.payload(o) for o in c["overlays"]]]).encode()).hexdigest()
        out = thumbs.CACHE / "collage" / (key + f"-{height or 0}.mp4")
        return str(thumbs.cached(out, lambda tmp: collage.render(
            c, tmp, height=height, fast=True, overlays=c["overlays"], repeat=repeat,
            cache=thumbs.CACHE / "collage" / "cells")))

    def _try_press(self):
        if self.trying or not any(self.c["cells"]):
            return
        self.stop()
        self.trying = True
        self._try_gen += 1
        gen = self._try_gen
        c = {**self.c, "cells": [dict(x) if x else None for x in self.c["cells"]],
             "overlays": [dict(o) for o in self.overlays]}
        self.spinner.start()
        self.ov_layer_on = False
        self.layer.set_visible(False)

        def work():
            try:
                path = self._try_path(c, c["repeat"])
                still = thumbs.grab_frame(path, min(c["cover"], collage.duration(c) - 0.05))
            except Exception as ex:
                thumbs.log("collage preview failed:", ex)
                path = still = None
                GLib.idle_add(lambda: self.clip_lbl.set_label(f"Preview failed — {ex}"))
            GLib.idle_add(start, path, still)

        def start(path, still):
            if gen != self._try_gen:  # released and pressed again meanwhile
                return
            self.spinner.stop()
            if not (path and still):
                self.trying = False
                return
            self.canvas_live = LiveCanvas(Gdk.Texture.new_for_pixbuf(still), path, style=self.style)
            self.stage.set_child(self.canvas_live)
            if self.trying:
                self.canvas_live.press()
            else:  # released while rendering: show the still a moment, then go back
                GLib.timeout_add(600, self._try_done)
        threading.Thread(target=work, daemon=True).start()

    def _try_release(self):
        self.trying = False
        cv = getattr(self, "canvas_live", None)
        if cv:
            cv.on_idle = self._try_done
            cv.release()
        elif self.spinner.get_spinning():
            self._try_gen += 1
            self.spinner.stop()

    def _try_done(self):
        cv = getattr(self, "canvas_live", None)
        if cv:
            cv.stop()
            self.canvas_live = None
            self.stage.set_child(self.canvas)
        self.ov_layer_on = True
        self.layer.set_visible(bool(self.overlays))
        return False

    # --- preview --------------------------------------------------------------------------------
    def refresh(self, cells=None, all_cells=False, quick=False):
        """Show the cover moment of each changed cell (still frames). Playback re-bakes if it was running."""
        idx = list(range(len(self.c["cells"])) if all_cells or cells is None else cells)
        self._length_changed()
        if self.playing:
            self.canvas.queue_draw()  # zoom/pan of a direct cell shows on the live frame right away
            self._resync_later(idx)
            return
        W, H = collage.size(self.c, PROXY_H)
        cover = min(self.c["cover"], collage.duration(self.c) - 0.04)
        for i in idx:
            cell = self.c["cells"][i]
            if not cell:
                continue
            o = {**effects.DEFAULT, **cell["opts"]}
            t = cell["start"] if self.at_start else collage.cell_time(cell, cover)  # short clips loop
            x, y, w, h = collage.rects(self.c, W, H)[i]
            crop = o["effect"] in CROP_EFFECTS
            if crop:
                chain, height = ",".join(collage.cell_fit(cell, w, h) + [effects.frame_chain(o)]), PROXY_H
            else:  # whole frame, tall enough to stay sharp at the cell's zoom on a large window
                _, zh = collage.fit_size(cell, w, h)
                chain, height = effects.frame_chain(o), min(1080, max(PROXY_H, round(zh * 2.2) // 2 * 2))
            key = (cell["src"], round(t, 3), chain, height)
            if key == self.want[i] and self.paint[i] is not None and self.cropped[i] == crop:
                continue  # same still already showing (e.g. a zoom that didn't need more pixels)
            self.want[i] = key
            pool.submit(self._grab, i, key, crop)

    def refresh_later(self, cells, ms=250):
        """Debounced refresh for continuous input (scroll-zoom)."""
        self._later.update(cells)
        if self._later_id:
            GLib.source_remove(self._later_id)
        self._later_id = GLib.timeout_add(ms, self._refresh_now)

    def _refresh_now(self):
        self._later_id = 0
        cells, self._later = list(self._later), set()
        self.refresh(cells)
        return False

    def _grab(self, i, key, crop):
        if self.want[i] != key:
            return
        src, t, chain, height = key
        pb = thumbs.grab_frame(src, t, height, chain)

        def done():
            if self.want[i] == key and pb and not self.playing:
                self.paint[i], self.cropped[i] = Gdk.Texture.new_for_pixbuf(pb), crop
                self.canvas.queue_draw()
        GLib.idle_add(done)

    def sync_zoom(self):
        """Reflect a scroll-zoom in the panel slider without rebuilding it."""
        cell = self.c["cells"][self.sel]
        if cell and getattr(self, "zoom_scale", None):
            with self._zooming():
                self.zoom_scale.set_value(float(cell.get("zoom") or 1.0))

    def _zooming(self):
        page = self

        class _Guard:
            def __enter__(self):
                page._zoom_sync = True

            def __exit__(self, *_a):
                page._zoom_sync = False
        return _Guard()

    def _rebuild_panel_if_framing(self):
        """A pan/zoom change alters the framing row's text; keep it honest without a full rebuild."""
        if getattr(self, "framing_lbl", None):
            cell = self.c["cells"][self.sel]
            self.framing_lbl.set_title("Framing")
            self.framing_lbl.set_subtitle(f"Zoom {float(cell.get('zoom') or 1):.2f}×" if cell else "")

    def toggle_play(self):
        if self.playing:
            return self.stop()
        if not any(self.c["cells"]):
            return
        self.stop()
        self.playing = True
        self.gen += 1
        self.play_btn.set_icon_name("media-playback-pause-symbolic")
        self.clock0 = None  # shared collage clock (µs), started by the first cell that shows a frame
        for i in range(len(self.c["cells"])):
            self._sync_cell(i)
        self.tick = GLib.timeout_add(40, self._on_tick)
        self._label()

    # Live preview: every cell is its own GStreamer stream (hardware-decoded where the GPU can), drawn into the
    # canvas — nothing is composited by ffmpeg while you edit. A plain cell plays its source directly, so volume,
    # zoom/pan and trim changes land on the next frame. Cells with a look/effect/speed/direction (or a GIF, which
    # GTK can't play) render just that cell once (cached) and loop it; editing one cell never touches the others.
    # One shared clock (like the render): collage time T loops every collage.duration; each cell should show
    # T % its pass, so short cells restart together with the longest one. _on_tick seeks a cell back onto the clock
    # when it is more than SYNC_TOL off (its own loop end, a late start, a re-sync after an edit).
    # ponytail: correction by seeking, so cells can be up to SYNC_TOL apart; Save renders exactly.
    def _live_spec(self, i):
        cell = self.c["cells"][i]
        if not cell:
            return None
        o = {**effects.DEFAULT, **cell["opts"]}
        if o == effects.DEFAULT and not cell["src"].lower().endswith(".gif"):
            return ("direct", cell["src"])
        W, H = collage.size(self.c, thumbs.preview_height())
        _, _, w, h = collage.rects(self.c, W, H)[i]
        bake = {k: v for k, v in cell.items() if k not in ("dur", "ar")}
        bake["volume"] = 1.0  # the level is applied live on the stream
        return ("bake", repr(sorted(bake.items())), w, h)

    def _sync_cell(self, i):
        """Make cell i's live stream match the cell; the old stream keeps playing until a new one is ready."""
        spec, cur = self._live_spec(i), self.live[i]
        if cur and cur["spec"] == spec:
            cell = self.c["cells"][i]
            if spec[0] == "direct" and cur["win"] != (cell["start"], collage.window(cell)):
                cur["win"] = (cell["start"], collage.window(cell))
                cur["stream"].seek(int(cell["start"] * 1e6))
            self._apply_volume(i)
            return
        if not spec:
            return self._drop_live(i)
        gen = self.gen
        if spec[0] == "direct":
            return self._start_live(i, spec, spec[1])
        self.bake_tok[i] += 1
        tok, cell = self.bake_tok[i], dict(self.c["cells"][i], volume=1.0)
        self.pending.add(i)
        self.spinner.start()

        def work():
            with thumbs.BAKE_LOCK:
                if gen != self.gen or tok != self.bake_tok[i]:  # superseded while queued
                    return
                try:
                    path, err = collage._cell_file(cell, spec[2], spec[3], True, thumbs.CACHE / "collage" / "cells")[0], None
                except Exception as ex:
                    thumbs.log("collage cell bake failed:", ex)
                    path, err = None, str(ex)
            GLib.idle_add(land, path, err)

        def land(path, err):
            if gen != self.gen or tok != self.bake_tok[i]:
                return
            self.pending.discard(i)
            if not self.pending:
                self.spinner.stop()
            if path:
                self._start_live(i, spec, path)
            else:
                self.clip_lbl.set_label(f"Preview failed — {err}")
        threading.Thread(target=work, daemon=True).start()

    def _start_live(self, i, spec, path):
        cell = self.c["cells"][i]
        m = media_file(path, muted=cell["volume"] <= 0, loop=True)
        entry = {"spec": spec, "stream": m, "shown": False, "win": (cell["start"], collage.window(cell))}

        def prepared(s, _):
            if s.is_prepared() and spec[0] == "direct" and cell["start"] > 0:
                s.seek(int(entry["win"][0] * 1e6))

        def frame(_s):
            if self.live[i] is not entry:
                return
            if spec[0] == "direct" and (m.is_seeking() or m.get_timestamp() / 1e6 < entry["win"][0] - 0.5):
                return  # frame 0 before the seek to the window landed: keep showing the still
            if not entry["shown"]:
                entry["shown"] = True
                if self.clock0 is None:
                    self.clock0 = GLib.get_monotonic_time() - int(self._stream_pos(entry) * 1e6)
                old = entry.pop("old", None)
                if old:
                    release(old["stream"])
            self.canvas.queue_draw()
        m.connect("notify::prepared", prepared)
        m.connect("invalidate-contents", frame)
        m.set_volume(max(0.0, min(1.0, cell["volume"])))
        cur = self.live[i]  # keep showing whatever is on screen until the new stream's first frame
        if cur and cur["shown"]:
            entry["old"] = cur
            if cur.get("old"):  # never chain: at most one stale stream (and its decoder) behind the new one
                release(cur.pop("old")["stream"])
        elif cur:
            entry["old"] = cur.pop("old", None)
            release(cur["stream"])
        self.live[i] = entry
        m.play()

    def _drop_live(self, i):
        e, self.live[i] = self.live[i], None
        if e:
            release(e["stream"])
            if e.get("old"):
                release(e["old"]["stream"])
        self.canvas.queue_draw()

    def live_paint(self, i):
        """(paintable, cropped) to draw for cell i while playing, or None to fall back to the still."""
        e = self.live[i] if self.playing else None
        if e and not e["shown"]:
            e = e.get("old")
        if not e or e["stream"].get_intrinsic_width() <= 0:
            return None
        return e["stream"], e["spec"][0] == "bake"

    def _apply_volume(self, i):
        e, cell = self.live[i], self.c["cells"][i]
        if e and cell:
            e["stream"].set_muted(cell["volume"] <= 0)
            e["stream"].set_volume(max(0.0, min(1.0, cell["volume"])))

    def _resync_later(self, cells):
        """Debounced re-sync for edits while playing (each bake is a render; a drag must not start dozens)."""
        self._resync.update(cells)
        if self._rebake_id:
            GLib.source_remove(self._rebake_id)
        self._rebake_id = GLib.timeout_add(300, self._do_resync)

    def _do_resync(self):
        self._rebake_id = 0
        cells, self._resync = list(self._resync), set()
        if self.playing:
            for i in cells:
                if i < len(self.live):
                    self._sync_cell(i)
        return False

    def _stream_pos(self, e):
        """Collage-pass seconds cell i's stream is at (a direct stream plays the source, offset by the window)."""
        ts = e["stream"].get_timestamp() / 1e6
        return (ts - e["win"][0]) % max(e["win"][1], 0.05) if e["spec"][0] == "direct" else ts

    def _on_tick(self):
        """Keep every cell on the shared collage clock and move the playhead of the selected one."""
        if not self.playing:
            self.tick = 0
            return False
        now = GLib.get_monotonic_time()
        if self.clock0 is not None:
            T = (now - self.clock0) / 1e6 % max(collage.duration(self.c), 0.05)
            for i, (e, cell) in enumerate(zip(self.live, self.c["cells"])):
                st = e and e["stream"]
                if not (st and cell and e["shown"] and st.is_prepared()) or st.is_seeking() or now < e.get("hold", 0):
                    continue
                want = T % max(collage.cell_length(cell), 0.05)
                pos = st.get_timestamp() / 1e6
                target = collage.cell_time(cell, want) if e["spec"][0] == "direct" else want
                if abs(pos - target) > SYNC_TOL:
                    st.seek(int(target * 1e6))
                    e["hold"] = now + 500_000  # let the seek land before judging it again
        e, cell = self.live[self.sel] if self.sel < len(self.live) else None, self.c["cells"][self.sel]
        if e and cell and self.tl:
            ts = e["stream"].get_timestamp() / 1e6
            self.tl.p = ts if e["spec"][0] == "direct" else collage.cell_time(cell, ts)
            self.tl.queue_draw()
        return True

    def stop(self):
        was = self.playing
        self.playing = False
        self.gen += 1
        self._try_done()
        self.spinner.stop()
        self.play_btn.set_icon_name("media-playback-start-symbolic")
        if self.tick:
            GLib.source_remove(self.tick)
            self.tick = 0
        for i in range(len(self.live)):
            self._drop_live(i)
        self.pending.clear()
        self.canvas.queue_draw()
        if was:  # pause = back to the start of the yellow window, for every cell
            self.at_start = True
            if self.tl and self.c["cells"][self.sel]:
                self.tl.p = self.c["cells"][self.sel]["start"]
                self.tl.queue_draw()
            self.refresh(all_cells=True)

    # --- save -----------------------------------------------------------------------------------
    def _export(self, fmt):
        """GIF/WebP of one pass of the collage as Save would render it (a GIF/WebP loops by itself)."""
        if not any(self.c["cells"]):
            return self.get_root().toast("Add at least one video to the collage")
        self.stop()
        c = {**self.c, "cells": [dict(x) if x else None for x in self.c["cells"]]}
        ovs = [dict(o) for o in self.overlays]
        export.save_as(self, fmt, "Collage", lambda tmp: collage.render(c, str(Path(tmp, "collage.mp4")), overlays=ovs,
                                                                        repeat=1), self._busy)

    def _busy(self, on):
        self.save_btn.set_sensitive(not on)
        self.save_btn.set_label("Exporting…" if on else self.SAVE_LABEL)

    def _save(self, _b):
        if not any(self.c["cells"]):
            self.on_saved(self, None, "Add at least one video to the collage")
            return
        self.stop()
        self.save_btn.set_sensitive(False)
        self.save_btn.set_label("Saving…")
        c = {**self.c, "cells": [dict(x) if x else None for x in self.c["cells"]]}
        style = self.style
        col = self.panel_page.collection_row.collection()
        smooth = self.panel_page.interp_group.settings()

        def work():
            try:
                jpg, mov = livemake.out_paths(col.path if col else None)
                collage.render(c, mov, extra=livemake.live_tags(), overlays=[dict(o) for o in self.overlays],
                               interpolate=smooth)
                f = interp.stretch(smooth)  # slow motion stretches the timeline, the cover moment with it
                livemake.still(mov, min(c["cover"] * f, collage.duration(c) * f - 0.05), jpg)
                library.set_meta(jpg, transition=style)
                res, err = (jpg, mov), None
            except Exception as ex:
                thumbs.log("collage save failed:", ex)
                res, err = None, str(ex)
            GLib.idle_add(self.on_saved, self, res, err)
        threading.Thread(target=work, daemon=True).start()
