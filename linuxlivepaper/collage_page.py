"""Collage maker: several videos/GIFs/live images playing at once in a layout, each with its own start,
volume, speed, direction, filter and effect, saved as one live image.

Performance: the preview never runs the full collage graph. Each cell gets its own small cached proxy
(effects.preview at 360p, rendered in parallel, ~0.5 s each) and GTK composites them live, so editing one cell
only re-renders that cell. The full-size ffmpeg collage render happens once, on save."""
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
from .widgets import CollectionRow, HoldButton  # noqa: E402

PROXY_H = 360
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
    """Draws the collage: each cell's paintable (still texture or playing proxy) cover-cropped into its rect."""

    def __init__(self, page):
        super().__init__(hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN)
        self.page = page
        click = Gtk.GestureClick()
        click.connect("released", self._click)
        self.add_controller(click)
        self.set_cursor_from_name("pointer")

    def frame(self):
        """(x, y, scale) of the canvas fitted inside the widget."""
        _, W, H = collage.ASPECTS[self.page.c["aspect"]]
        s = min(self.get_width() / W, self.get_height() / H)
        return (self.get_width() - W * s) / 2, (self.get_height() - H * s) / 2, s

    def _click(self, _g, _n, x, y):
        ox, oy, s = self.frame()
        _, W, H = collage.ASPECTS[self.page.c["aspect"]]
        i = collage.cell_at(self.page.c, (x - ox) / (W * s), (y - oy) / (H * s))
        if i is not None:
            self.page.select(i, open_empty=True)

    def do_snapshot(self, snap):
        c = self.page.c
        ox, oy, s = self.frame()
        _, W, H = collage.ASPECTS[c["aspect"]]
        snap.append_color(rgba(c["bg"]), rect(ox, oy, W * s, H * s))
        for i, (x, y, w, h) in enumerate(collage.rects(c, W, H)):
            r = rect(ox + x * s, oy + y * s, w * s, h * s)
            p = self.page.paint[i]
            snap.push_clip(r)
            if p and p.get_intrinsic_width() > 0:
                pa = p.get_intrinsic_aspect_ratio() or 1
                cw, ch = (r.get_width(), r.get_width() / pa) if pa < r.get_width() / r.get_height() else \
                    (r.get_height() * pa, r.get_height())
                snap.save()
                snap.translate(Graphene.Point().init(r.get_x() + (r.get_width() - cw) / 2,
                                                     r.get_y() + (r.get_height() - ch) / 2))
                p.snapshot(snap, cw, ch)
                snap.restore()
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
            if i == self.page.sel:
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
        self.sel = 0
        self.style = DEFAULT_STYLE
        self.streams, self.playing, self.gen, self.tl = [], False, 0, None
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

        # the scroller caps the panel's natural width so the stage gets the spare room
        self.panel_box = Gtk.ScrolledWindow(width_request=360, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                            vscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=False)
        top = Gtk.Box(spacing=12, vexpand=True)
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
        canvas.add(self._combo("Length", [(d, f"{d:g} s") for d in collage.DURATIONS], c["duration"],
                               lambda v: self._set("duration", v)))
        canvas.add(self._combo("Borders", list(collage.GAPS.items()), c["gap"], lambda v: self._set("gap", v)))
        canvas.add(self._combo("Border color", list(collage.BACKGROUNDS.items()), c["bg"],
                               lambda v: self._set("bg", v)))
        page.add(canvas)

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
        page.add(grp)
        if cell:
            win = Adw.ActionRow(title="Window", subtitle=f"{collage.window(cell, c['duration']):.2f} s of the clip"
                                                        + ("" if cell.get("len") is None else " (custom)"))
            fit = Gtk.Button(label="Fit", valign=Gtk.Align.CENTER, tooltip_text="Use exactly what the collage needs")
            fit.connect("clicked", lambda *_: self._fit_window())
            win.add_suffix(fit)
            grp.add(win)
        self.ov_regroup()
        page.add(self.overlay_group)

        live = Adw.PreferencesGroup(title="Live image")
        cover = Adw.ActionRow(title="Cover", subtitle="Moment used as the still")
        cs = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, c["duration"], 0.05)
        cs.set_value(min(c["cover"], c["duration"]))
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

    # --- edits ----------------------------------------------------------------------------------
    def _set(self, key, value):
        if self.c[key] == value:
            return
        self.c[key] = value
        if key == "duration":
            self.c["cover"] = min(self.c["cover"], value)
            self._rebuild_panel()
            self._rebuild_timeline()
            self.refresh(all_cells=True)
        else:
            self.canvas.queue_draw()

    def _set_layout(self, layout):
        if layout != self.c["layout"]:
            self.stop()
            collage.set_layout(self.c, layout)
            n = len(self.c["cells"])
            self.paint = (self.paint + [None] * n)[:n]
            self.sel = min(self.sel, n - 1)
            self.canvas.queue_draw()
            self._rebuild_timeline()
        self._rebuild_panel()  # also resets the toggle states

    def select(self, i, open_empty=False):
        if open_empty and not self.c["cells"][i]:
            self.sel = i
            self._choose(i)
        elif i != self.sel:
            self.sel = i
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

    def set_clip(self, i, path):
        self.stop()
        dur = thumbs.probe(path)[0] or 1.0
        old = self.c["cells"][i]
        cell = collage.new_cell(path)
        if old:  # keep the cell's look when replacing the clip
            cell.update(volume=old["volume"], opts=old["opts"])
        cell["dur"] = dur
        self.c["cells"][i] = cell
        self.sel = i
        self.paint[i] = None
        self._rebuild_panel()
        self._rebuild_timeline()
        self.refresh([i])

    def _remove(self, i):
        self.stop()
        self.c["cells"][i] = None
        self.paint[i] = None
        self._rebuild_panel()
        self._rebuild_timeline()
        self.canvas.queue_draw()

    def _set_volume(self, v):
        cell = self.c["cells"][self.sel]
        cell["volume"] = round(v, 2)
        if self.playing and self.sel < len(self.streams) and self.streams[self.sel]:
            self.streams[self.sel].set_volume(cell["volume"])

    def _set_opt(self, key, value):
        cell = self.c["cells"][self.sel]
        if cell["opts"][key] == value:
            return
        cell["opts"][key] = value
        if key in ("speed", "direction"):
            self._rebuild_timeline()  # the window length depends on speed/bounce
        self.refresh([self.sel])

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
        need = collage.window(cell, self.c["duration"])
        dur, vw, vh = D, *thumbs.probe(cell["src"])[1:]
        self.tl = Timeline(cell["src"], dur, vw / vh if vh else 16 / 9, fixed=True)
        cell["start"] = min(cell["start"], max(0.0, D - min(need, D)))
        self.tl.s, self.tl.e = cell["start"], min(D, cell["start"] + need)
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
                                    f"{cell['start']:.2f}–{cell['start'] + collage.window(cell, self.c['duration']):.2f} s")

    def _tl_changed(self, tl, what):
        """The yellow window is both movable and stretchable; a shorter window freezes on the last frame."""
        if what not in ("move", "start", "end"):
            return
        cell = self.c["cells"][self.sel]
        need = min(collage.src_length(self.c["duration"], cell["opts"]), cell["dur"])
        length = round(min(tl.e - tl.s, need), 3)
        auto = abs(length - need) < 1e-3
        if abs(cell["start"] - tl.s) < 1e-3 and auto == (cell.get("len") is None):
            return
        cell["start"], cell["len"] = round(tl.s, 3), None if auto else length
        if tl.e - tl.s > length + 1e-3:  # nothing beyond this is ever used: show the real window
            tl.set_selection(cell["start"], cell["start"] + length)
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
        return self.c["duration"]

    def ov_click(self, x, y):
        ox, oy, vw, vh = self.ov_rect()
        if vw <= 0:
            return
        i = collage.cell_at(self.c, (x - ox) / vw, (y - oy) / vh)
        if i is not None:
            self.select(i, open_empty=True)

    def _try_path(self, c):
        """Cached low-res render of the whole collage, for the press preview."""
        import hashlib
        key = hashlib.sha1(repr([[
            ([[k, v] for k, v in sorted(x.items()) if k != "dur"] if x else None) for x in c["cells"]],
            c["layout"], c["aspect"], c["duration"], c["gap"], c["bg"],
            [overlays.payload(o) for o in c["overlays"]]]).encode()).hexdigest()
        out = thumbs.CACHE / "collage" / (key + ".mp4")
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp.mp4")
            collage.render(c, tmp, height=540, fast=True, overlays=c["overlays"])
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
                path = self._try_path(c)
                still = thumbs.grab_frame(path, min(c["cover"], c["duration"] - 0.05))
            except Exception:
                path = still = None
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
        """Show the cover moment of each changed cell (still frames). Playback restarts if it was running."""
        idx = list(range(len(self.c["cells"])) if all_cells or cells is None else cells)
        if self.playing:
            if not quick:  # keep playing: re-render only the cells that changed and swap them in
                self._swap(idx)
            return
        self.gen += 1
        gen = self.gen
        for i in idx:
            cell = self.c["cells"][i]
            if not cell:
                continue
            o = {**effects.DEFAULT, **cell["opts"]}
            need = collage.window(cell, self.c["duration"])
            t = cell["start"] + effects.src_time(min(self.c["cover"], self.c["duration"] - 0.04), need, o)
            t = t % max(cell["dur"], 0.05)  # short clips loop
            pool.submit(self._grab, gen, i, cell["src"], t, effects.frame_chain(o))

    def _grab(self, gen, i, src, t, chain):
        if gen != self.gen:
            return
        pb = thumbs.grab_frame(src, t, PROXY_H, chain)

        def done():
            if gen == self.gen and pb and not self.playing:
                self.paint[i] = Gdk.Texture.new_for_pixbuf(pb)
                self.canvas.queue_draw()
        GLib.idle_add(done)

    def _swap(self, idx):
        """Re-render the proxies of `idx` in place, without interrupting playback of the others."""
        D, cover = self.c["duration"], self.c["cover"]
        for i in idx:
            cell = self.c["cells"][i]
            if not cell:
                continue
            o = {**effects.DEFAULT, **cell["opts"]}
            s = cell["start"]
            fut = pool.submit(effects.preview, cell["src"], s, s + collage.window(cell, D), o,
                              thumbs.CACHE / "previews", PROXY_H, True)

            def done(fut, i=i):
                if not self.playing:
                    return
                try:
                    path = fut.result()
                except Exception:
                    return
                if not path or i >= len(self.streams):
                    return
                old = self.streams[i]
                m = media_file(path, muted=self.c["cells"][i]["volume"] == 0, loop=True)
                m.set_volume(self.c["cells"][i]["volume"])
                m.connect("invalidate-contents", self._frame, i)
                self.streams[i] = m
                m.play()
                if old:
                    old.set_playing(False)
            fut.add_done_callback(lambda f: GLib.idle_add(done, f))

    def toggle_play(self):
        if self.playing:
            return self.stop()
        cells = [(i, cell) for i, cell in enumerate(self.c["cells"]) if cell]
        if not cells:
            return
        self.playing = True
        self.gen += 1
        gen = self.gen
        self.spinner.start()
        self.play_btn.set_icon_name("media-playback-pause-symbolic")
        D = self.c["duration"]
        jobs = {}
        for i, cell in cells:
            o = {**effects.DEFAULT, **cell["opts"]}
            s = cell["start"]
            jobs[i] = pool.submit(effects.preview, cell["src"], s, s + collage.window(cell, D), o,
                                  thumbs.CACHE / "previews", PROXY_H, True)

        def wait():
            paths = {}
            for i, fut in jobs.items():
                try:
                    paths[i] = fut.result()
                except Exception:
                    paths[i] = None
            GLib.idle_add(start, paths)

        def start(paths):
            if gen != self.gen:
                return
            self.spinner.stop()
            self.streams = [None] * len(self.c["cells"])
            for i, path in paths.items():
                if not path:
                    continue
                m = media_file(path, muted=self.c["cells"][i]["volume"] == 0, loop=True)
                m.set_volume(self.c["cells"][i]["volume"])
                m.connect("invalidate-contents", self._frame, i)
                self.streams[i] = m
            # ponytail: each cell loops on its own clock; they're the same length so drift stays tiny.
            # A shared clock (one GStreamer pipeline) is the upgrade if it ever shows.
            for m in self.streams:
                if m:
                    m.play()
        threading.Thread(target=wait, daemon=True).start()

    def _frame(self, m, i):
        """Swap a cell from its still to the playing proxy on the first decoded frame (no blank flash)."""
        if self.playing and i < len(self.streams) and self.streams[i] is m:
            self.paint[i] = m
            self.canvas.queue_draw()

    def stop(self):
        was = self.playing
        self.playing = False
        self._try_done()
        self.gen += 1
        self.spinner.stop()
        self.play_btn.set_icon_name("media-playback-start-symbolic")
        for m in self.streams:
            if m:
                m.set_playing(False)
        self.streams = []
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
                livemake.still(mov, min(c["cover"], c["duration"] - 0.05), jpg)
                library.set_meta(jpg, transition=style)
                if col:
                    library.add_to_collection(library.Item("live", str(jpg), str(mov), 0), col)
                res, err = (jpg, mov), None
            except Exception as ex:
                res, err = None, str(ex)
            GLib.idle_add(self.on_saved, self, res, err)
        threading.Thread(target=work, daemon=True).start()
