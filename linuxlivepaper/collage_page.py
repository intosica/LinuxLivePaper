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

from . import collage, effects, library, livemake, overlays, thumbs  # noqa: E402
from .editor import Timeline  # noqa: E402
from .overlay_ui import OverlayHost  # noqa: E402
from .playback import DEFAULT_STYLE, STYLES, LiveCanvas, media_file  # noqa: E402
from .widgets import CollectionRow, FileBrowser, HoldButton  # noqa: E402

PROXY_H = 360
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

    def _draw_cell(self, snap, i, r, p, s):
        """Cover-fit the still into r. Uncropped stills follow the cell's live zoom/pan (dragging = redraw only)."""
        pa = p.get_intrinsic_aspect_ratio() or 1
        x, y, w, h = r.get_x(), r.get_y(), r.get_width(), r.get_height()
        cell = self.page.c["cells"][i]
        if self.page.cropped[i] or not cell:
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
        vid = self.page.video
        if vid and vid.get_intrinsic_width() > 0:  # the baked render already holds every cell
            pa = vid.get_intrinsic_aspect_ratio() or W / H
            cw, ch = (W * s, W * s / pa) if pa < W / H else (H * s * pa, H * s)
            snap.save()
            snap.translate(Graphene.Point().init(ox + (W * s - cw) / 2, oy + (H * s - ch) / 2))
            vid.snapshot(snap, cw, ch)
            snap.restore()
            return
        for i, (x, y, w, h) in enumerate(collage.rects(c, W, H)):
            r = rect(ox + x * s, oy + y * s, w * s, h * s)
            p = self.page.paint[i]
            snap.push_clip(r)
            if p and p.get_intrinsic_width() > 0:
                self._draw_cell(snap, i, r, p, s)
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
        self.stream, self.video, self.playing, self.gen, self.tl = None, None, False, 0, None
        self.drag_cell, self.drag_move, self._rebake_id = None, None, 0
        self._panel_stale = False
        self.zoom_scale, self.framing_lbl, self._zoom_sync = None, None, False
        self.tick = 0
        self.canvas_live = None
        self.canvas_widget, self.trying, self._try_gen = None, False, 0

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

        self.save_btn = Gtk.Button(label=self.SAVE_LABEL, css_classes=["suggested-action"])
        self.save_btn.connect("clicked", self._save)
        header = Adw.HeaderBar()
        header.pack_end(self.save_btn)
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

    def _set_repeat(self, n):
        self.c["repeat"] = n  # only the saved file / Try it repeat; Play loops anyway
        self._length_changed()

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
        if self.playing:  # one stream holds the whole mix, so the level only lands on the next bake
            self._rebake()

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
        key = hashlib.sha1(repr([[
            ([[k, v] for k, v in sorted(x.items()) if k not in ("dur", "ar")] if x else None) for x in c["cells"]],
            c["layout"], c["aspect"], repeat, c["gap"], c["bg"],
            [overlays.payload(o) for o in c["overlays"]]]).encode()).hexdigest()
        out = thumbs.CACHE / "collage" / (key + ".mp4")
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp.mp4")
            collage.render(c, tmp, height=540, fast=True, overlays=c["overlays"], repeat=repeat,
                           cache=thumbs.CACHE / "collage" / "cells")
            out.unlink(missing_ok=True)
            tmp.rename(out)
        return str(out)

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
            if not quick:  # quick is a drag in progress: re-baking on every motion would stutter
                self._rebake()
            return
        W, H = collage.size(self.c, PROXY_H)
        cover = min(self.c["cover"], collage.duration(self.c) - 0.04)
        for i in idx:
            cell = self.c["cells"][i]
            if not cell:
                continue
            o = {**effects.DEFAULT, **cell["opts"]}
            t = collage.cell_time(cell, cover)  # short clips loop
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
        gen = self.gen
        c = self._collage_copy()
        self.spinner.start()
        self.play_btn.set_icon_name("media-playback-pause-symbolic")
        self.clip_lbl.set_label("Rendering preview…")

        def work():
            try:
                path, err = self._try_path(c), None
            except Exception as ex:
                thumbs.log("collage play failed:", ex)
                path, err = None, str(ex)
            GLib.idle_add(start, path, err)

        def start(path, err):
            if gen != self.gen:
                return
            self.spinner.stop()
            if not path:
                self.playing = False
                self.play_btn.set_icon_name("media-playback-start-symbolic")
                self.clip_lbl.set_label(f"Preview failed — {err}")
                return
            self._label()
            self._play(path)
            self.tick = GLib.timeout_add(40, self._on_tick)
        threading.Thread(target=work, daemon=True).start()

    def _play(self, path):
        """One stream for the whole collage: the same GStreamer path a single clip preview uses."""
        m = media_file(path, muted=False, loop=True)

        def frame(stream):
            self.video = stream
        m.connect("invalidate-contents", lambda s: (frame(s), self.canvas.queue_draw()))
        m.play()
        self.stream = m

    def _rebake(self):
        """An edit while playing: re-render the baked collage (debounced) and swap it in at the same position."""
        if self._rebake_id:
            GLib.source_remove(self._rebake_id)
        self._rebake_id = GLib.timeout_add(500, self._do_rebake)

    def _do_rebake(self):
        self._rebake_id = 0
        if not self.playing:
            return False
        gen, c = self.gen, self._collage_copy()

        def work():
            try:
                path = self._try_path(c)
            except Exception as ex:
                thumbs.log("collage re-bake failed:", ex)
                return
            GLib.idle_add(swap, path)

        def swap(path):
            if gen != self.gen or not self.playing:
                return
            old = self.stream
            ts = old.get_timestamp() if old else 0
            self._play(path)
            if self.stream.is_seekable() and ts:
                self.stream.seek(ts)
            if old:
                old.set_playing(False)
        threading.Thread(target=work, daemon=True).start()
        return False

    def _on_tick(self):
        """Move the playhead along the selected cell's window while the collage plays."""
        if not self.playing or not self.stream:
            self.tick = 0
            return False
        cell = self.c["cells"][self.sel]
        if cell and self.tl:
            t = self.stream.get_timestamp() / 1e6 % max(collage.duration(self.c), 0.1)
            self.tl.p = collage.cell_time(cell, t)
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
        if self.stream:
            self.stream.set_playing(False)
            self.stream = None
        self.video = None
        self.canvas.queue_draw()
        if was:
            self.refresh(all_cells=True)

    # --- save -----------------------------------------------------------------------------------
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

        def work():
            try:
                jpg, mov = livemake.out_paths()
                collage.render(c, mov, extra=livemake.live_tags(), overlays=[dict(o) for o in self.overlays])
                livemake.still(mov, min(c["cover"], collage.duration(c) - 0.05), jpg)
                library.set_meta(jpg, transition=style)
                if col:
                    library.add_to_collection(library.Item("live", str(jpg), str(mov), 0), col)
                res, err = (jpg, mov), None
            except Exception as ex:
                thumbs.log("collage save failed:", ex)
                res, err = None, str(ex)
            GLib.idle_add(self.on_saved, self, res, err)
        threading.Thread(target=work, daemon=True).start()
