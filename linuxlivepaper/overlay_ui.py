"""Shared overlay UI: the placement layer over a preview plus its side-panel group.

Both the live-image editor and the collage maker put text/stickers over their preview, so the layer and the panel
live here once. A page mixes in `OverlayHost` and provides the few hooks at the bottom of this file."""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gio, Graphene, Gtk, Pango  # noqa: E402

from . import overlays, thumbs  # noqa: E402

SELECT = Gdk.RGBA()
SELECT.parse("#3584e4")
WHITE = Gdk.RGBA()
WHITE.parse("#ffffff")


def rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def _font(o, px):
    fd = Pango.FontDescription()
    fd.set_family(str(o.get("font") or "Sans"))
    fd.set_weight(Pango.Weight.BOLD if o.get("bold", True) else Pango.Weight.NORMAL)
    fd.set_absolute_size(max(6, px) * Pango.SCALE)
    return fd


class OverlayLayer(Gtk.Widget):
    """Draws the overlays over the preview and drags/resizes the selected one."""

    def __init__(self, page):
        super().__init__(hexpand=True, vexpand=True)
        self.page = page
        self._off = (0.0, 0.0)
        self._pan = False
        click = Gtk.GestureClick()
        click.connect("pressed", self._pressed)
        self.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._dragged)
        drag.connect("drag-end", lambda *_a: self._end())
        self.add_controller(drag)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)

    def box(self, o):
        """Unrotated box of the overlay in widget coordinates."""
        ox, oy, vw, vh = self.page.ov_rect()
        if o["kind"] == "text":
            tw, th = self.page.text_layout(o, vh).get_pixel_size()
        else:
            tex = self.page.sticker_texture(o)
            tw = o.get("size", 0.35) * vw
            th = tw / (tex.get_intrinsic_aspect_ratio() or 1) if tex else tw
        return ox + o.get("x", 0.5) * vw - tw / 2, oy + o.get("y", 0.5) * vh - th / 2, tw, th

    def _begin(self, _g, x, y):
        self._off = (0.0, 0.0)
        self._pressed(None, 1, x, y)
        self._pan = not self.page.overlay()
        if self._pan:
            self.page.ov_pan(x, y, 0.0, 0.0)

    def _pressed(self, _g, _n, x, y):
        # ponytail: hit-testing ignores rotation — close enough to grab, exact boxes are not worth the math
        for i in reversed(range(len(self.page.overlays))):
            bx, by, bw, bh = self.box(self.page.overlays[i])
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self.page.select_overlay(i)
                return
        self.page.select_overlay(None)
        self.page.ov_click(x, y)

    def _dragged(self, _g, ox, oy):
        dx, dy = ox - self._off[0], oy - self._off[1]  # drag-update reports the offset since the drag start
        self._off = (ox, oy)
        if self._pan:  # pressed on empty space: pan the picture under it (the collage's cell)
            self.page.ov_pan(0.0, 0.0, dx, dy)
            return
        o = self.page.overlay()
        if not o or not (dx or dy):
            return
        _, _, vw, vh = self.page.ov_rect()
        o["x"] = min(1.0, max(0.0, o.get("x", 0.5) + dx / max(1.0, vw)))
        o["y"] = min(1.0, max(0.0, o.get("y", 0.5) + dy / max(1.0, vh)))
        self.page.ov_changed()

    def _end(self):
        if self._pan:
            self._pan = False
            self.page.ov_pan_end()

    def _scroll(self, _c, _dx, dy):
        o = self.page.overlay()
        if not o:
            return False
        o["size"] = min(2.0, max(0.03, o.get("size", 0.3) * (0.92 if dy > 0 else 1.08)))
        self.page.sync_overlay_ui()
        self.page.ov_changed()
        return True

    def do_snapshot(self, snap):
        _, _, _, vh = self.page.ov_rect()
        for i, o in enumerate(self.page.overlays):
            bx, by, bw, bh = self.box(o)
            if bw < 1 or bh < 1:
                continue
            snap.save()
            snap.translate(Graphene.Point().init(bx + bw / 2, by + bh / 2))
            snap.rotate(float(o.get("rotation") or 0.0))  # Gtk.Snapshot.rotate is in degrees
            snap.translate(Graphene.Point().init(-bw / 2, -bh / 2))
            if o["kind"] == "text":
                snap.append_layout(self.page.text_layout(o, vh), WHITE)
            else:
                tex = self.page.sticker_texture(o)
                if tex:
                    tex.snapshot(snap, bw, bh)
            if i == self.page.ov_sel:
                for e in (rect(-3, -3, bw + 6, 2), rect(-3, bh + 1, bw + 6, 2),
                          rect(-3, -3, 2, bh + 6), rect(bw + 1, -3, 2, bh + 6)):
                    snap.append_color(SELECT, e)
            snap.restore()


class OverlayGroup(Adw.PreferencesGroup):
    """The overlay controls: add, pick, size, opacity, rotation, style, time window, remove."""

    def __init__(self, host, title="Overlays"):
        super().__init__(title=title, description="Drag to move, scroll to resize.")
        self.host = host
        self._syncing = False
        buttons = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER)  # beside the title, clear of the list below
        for label, icon, fn in (("Text", "insert-text-symbolic", host.add_text),
                                ("Sticker", "insert-image-symbolic", host.add_sticker)):
            b = Gtk.Button(child=Adw.ButtonContent(label=label, icon_name=icon))
            b.connect("clicked", lambda _b, f=fn: f())
            buttons.append(b)
        self.set_header_suffix(buttons)

        self.combo = Adw.ComboRow(title="Selected")
        self.combo.connect("notify::selected", lambda r, _: self._pick(r.get_selected()))
        self.add(self.combo)

        self.size_row, self.size = self._scale("Size", 0.03, 1.5, 0.01, "size")
        self.opacity_row, self.opacity = self._scale("Opacity", 0.05, 1.0, 0.01, "opacity")
        self.rot_row, self.rot = self._scale("Rotation", -180, 180, 1, "rotation")
        self.outline_row, self.outline = self._scale("Outline", 0.0, 0.02, 0.0005, "outline")

        self.text_row = Adw.EntryRow(title="Text")
        self.text_row.connect("changed", lambda r: self._set("text", r.get_text()))
        self.add(self.text_row)
        self.font_row = Adw.EntryRow(title="Font")
        self.font_row.connect("changed", lambda r: self._set("font", r.get_text() or "Sans"))
        self.add(self.font_row)
        self.bold_row = Adw.SwitchRow(title="Bold")
        self.bold_row.connect("notify::active", lambda r, _: self._set("bold", r.get_active()))
        self.add(self.bold_row)

        self.whole_row = Adw.SwitchRow(title="Whole clip", subtitle="Off: show it only for part of the clip")
        self.whole_row.connect("notify::active", lambda r, _: self._whole(r.get_active()))
        self.add(self.whole_row)
        self.start_row = Adw.SpinRow(title="Starts at", digits=2,
                                    adjustment=Gtk.Adjustment(value=0, lower=0, upper=3600, step_increment=0.1))
        self.start_row.connect("notify::value", lambda r, _: self._edge("start", r.get_value()))
        self.add(self.start_row)
        self.end_row = Adw.SpinRow(title="Ends at", digits=2,
                                  adjustment=Gtk.Adjustment(value=0, lower=0, upper=3600, step_increment=0.1))
        self.end_row.connect("notify::value", lambda r, _: self._edge("end", r.get_value()))
        self.add(self.end_row)

        self.rm = Gtk.Button(label="Remove overlay", halign=Gtk.Align.CENTER, margin_top=18, margin_bottom=6,
                             css_classes=["destructive-action", "pill"])
        self.rm.connect("clicked", lambda *_: host.remove_overlay())
        self.add(self.rm)

    def _scale(self, title, low, high, step, key):
        row = Adw.ActionRow(title=title)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, step)
        scale.set_hexpand(True)
        scale.set_size_request(120, -1)
        scale.set_draw_value(False)
        scale.connect("value-changed", lambda sc: self._set(key, sc.get_value()))
        row.add_suffix(scale)
        self.add(row)
        return row, scale

    def _set(self, key, value):
        if self._syncing:
            return
        self.host.set_overlay(key, value)

    def _pick(self, i):
        if self._syncing:
            return
        self.host.select_overlay(i if self.host.overlays else None)

    def _whole(self, whole):
        if self._syncing:
            return
        o = self.host.overlay()
        if not o:
            return
        if whole:
            o["start"] = o["end"] = None
        else:
            o["start"], o["end"] = 0.0, round(self.host.ov_duration(), 2)
        self.sync()
        self.host.ov_changed()

    def _edge(self, key, value):
        if self._syncing:
            return
        o = self.host.overlay()
        if not o:
            return
        value = max(0.0, round(value, 2))
        if key == "start":
            o["start"] = value
            if o.get("end") is not None and o["end"] <= value:
                o["end"] = round(value + 0.3, 2)
        else:
            o["end"] = value
            if value <= (o.get("start") or 0.0):
                o["start"] = max(0.0, round(value - 0.3, 2))
        self.sync()
        self.host.ov_changed()

    def sync(self):
        """Rebuild the picker and fill every row from the selected overlay."""
        ovs = self.host.overlays
        self._syncing = True
        names = [("Text: " + (o.get("text") or "")[:16]) if o["kind"] == "text"
                 else "Sticker: " + os.path.basename(o.get("path") or "")[:16] for o in ovs]
        self.combo.set_model(Gtk.StringList.new(names or ["No overlays yet"]))
        self.combo.set_sensitive(bool(ovs))
        if self.host.ov_sel is not None:
            self.combo.set_selected(self.host.ov_sel)
        self._syncing = False
        o = self.host.overlay()
        self.rm.set_visible(bool(o))
        is_text = bool(o) and o["kind"] == "text"
        for row in (self.size_row, self.opacity_row, self.rot_row, self.outline_row, self.whole_row):
            row.set_visible(bool(o))
        for row in (self.text_row, self.font_row, self.bold_row):
            row.set_visible(is_text)
        if not o:
            self.start_row.set_visible(False)
            self.end_row.set_visible(False)
            return
        whole = o.get("start") is None and o.get("end") is None
        self.size.set_value(o.get("size", 0.3))
        self.opacity.set_value(o.get("opacity", 1.0))
        self.rot.set_value(float(o.get("rotation") or 0))
        self.text_row.set_text(o.get("text") or "")
        self.font_row.set_text(o.get("font") or "Sans")
        self.bold_row.set_active(bool(o.get("bold", True)))
        self.outline.set_value(0.0035 if o.get("outline") is True else float(o.get("outline") or 0))
        self.whole_row.set_active(whole)
        dur = max(0.1, self.host.ov_duration())
        self.start_row.set_visible(not whole)
        self.end_row.set_visible(not whole)
        self.start_row.set_value(min(dur, o.get("start") or 0.0))
        self.end_row.set_value(min(dur, o.get("end") if o.get("end") is not None else dur))


class OverlayHost:
    """Mixin giving a page the overlays list, its placement layer and the panel group.

    The page must call `ov_init()` with the Gtk.Overlay that holds its preview, and implement the hooks below."""

    overlays = ()
    ov_sel = None

    def ov_init(self):
        self.layer = OverlayLayer(self)
        self.layer.set_can_target(False)
        self.ov_layer_on = True
        self.ov_reset()

    def ov_reset(self):
        self.overlays, self.ov_sel = [], None
        self._textures, self._next_id = {}, 0
        self.ov_regroup()

    def ov_regroup(self):
        """A fresh panel group — pages that rebuild their side panel need an unparented widget."""
        self.overlay_group = OverlayGroup(self)
        self.sync_overlay_ui()

    # --- hook the page overrides ---------------------------------------------------------------
    def ov_rect(self):
        """(x, y, w, h) of the video rect inside the layer — the preview letterboxes."""
        raise NotImplementedError

    def ov_duration(self):
        """Length in seconds of what is being edited, for the whole-clip window default."""
        return 3.0

    def ov_changed(self):
        """Called after any overlay edit; overrides must call super()."""
        self.layer.queue_draw()

    def ov_click(self, x, y):
        """A press on the layer that missed every overlay (the collage uses it to pick a cell)."""

    def ov_pan(self, x, y, dx, dy):
        """A press/drag on the layer that missed every overlay (the collage pans the cell under it)."""

    def ov_pan_end(self):
        """End of the miss-drag: re-render what was panned."""

    # --- shared behaviour ----------------------------------------------------------------------
    def overlay(self):
        return self.overlays[self.ov_sel] if self.ov_sel is not None else None

    def ov_repaint(self):
        self.layer.queue_draw()
        if self.overlays:
            self.layer.set_can_target(True)
        self.layer.set_visible(bool(self.overlays) and getattr(self, "ov_layer_on", True))

    def text_layout(self, o, height_px):
        lay = self.create_pango_layout()
        lay.set_text(o.get("text") or "", -1)
        lay.set_font_description(_font(o, max(6, height_px * o.get("size", 0.09))))
        attrs = Pango.AttrList()
        c = Gdk.RGBA()
        c.parse(o.get("color", "#ffffff"))
        attrs.insert(Pango.attr_foreground_new(int(c.red * 65535), int(c.green * 65535), int(c.blue * 65535)))
        attrs.insert(Pango.attr_foreground_alpha_new(int(c.alpha * 65535 * o.get("opacity", 1.0))))
        lay.set_attributes(attrs)
        return lay

    def sticker_texture(self, o):
        key = o.get("id") or o.get("path")
        if key not in self._textures or self._textures[key] is None:
            try:
                self._textures[key] = Gdk.Texture.new_from_filename(str(thumbs.alpha_frame(o["path"])))
            except (GLib.Error, OSError):
                self._textures[key] = None
        return self._textures[key]

    def add_text(self):
        self._next_id += 1
        self.overlays.append(overlays.text("Your text", id=self._next_id))
        self.select_overlay(len(self.overlays) - 1)

    def add_sticker(self, path=None):
        if path is None:
            f = Gtk.FileFilter(name="Images and GIFs")
            f.add_mime_type("image/*")
            store = Gio.ListStore(item_type=Gtk.FileFilter)
            store.append(f)

            def picked(d, res):
                try:
                    self.add_sticker(d.open_finish(res).get_path())
                except GLib.Error:
                    pass
            Gtk.FileDialog(title="Choose a sticker", filters=store).open(self.get_root(), None, picked)
            return
        self._next_id += 1
        self.overlays.append(overlays.sticker(path, id=self._next_id))
        self.select_overlay(len(self.overlays) - 1)

    def remove_overlay(self):
        if self.ov_sel is None:
            return
        self.overlays.pop(self.ov_sel)
        self.select_overlay(None if not self.overlays else min(self.ov_sel, len(self.overlays) - 1))

    def set_overlay(self, key, value):
        o = self.overlay()
        if not o:
            return
        o[key] = value
        self._textures.pop(o.get("id") or o.get("path"), None)
        self.ov_changed()

    def select_overlay(self, i):
        self.ov_sel = None if i is None or not self.overlays else min(i, len(self.overlays) - 1)
        self.sync_overlay_ui()
        self.ov_changed()

    def sync_overlay_ui(self):
        self.overlay_group.sync()
        self.layer.set_can_target(bool(self.overlays))
        self.ov_repaint()

    def overlay_payload(self):
        return [dict(o) for o in self.overlays]
