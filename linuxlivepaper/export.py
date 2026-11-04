"""Export a rendered clip as an animated GIF or WebP for sharing (not for live viewing): ffmpeg only, with the
knobs that decide file size — width, frame rate, palette size + dithering (GIF), quality (WebP)."""
import os
import subprocess
import tempfile
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import library, thumbs  # noqa: E402

WIDTHS = [("Original size", 0), ("1080 px", 1080), ("720 px", 720), ("480 px", 480), ("360 px", 360), ("240 px", 240)]
DITHERS = {"bayer:bayer_scale=3": "Ordered (smaller)", "sierra2_4a": "Smooth (Sierra)",
           "floyd_steinberg": "Floyd–Steinberg", "none": "None (smallest, banding)"}
DEFAULTS = {"width": 480, "fps": 15, "colors": 256, "dither": "bayer:bayer_scale=3", "quality": 75}


def encode(mov, dest, fmt, width=480, fps=15, colors=256, dither="bayer:bayer_scale=3", quality=75):
    """Write mov as dest (fmt "gif" or "webp"). width 0 keeps the source size (never upscales). Returns bytes."""
    scale = f",scale='min({width}\\,iw)':-2:flags=lanczos" if width else ""
    vf = f"fps={fps}{scale}"
    if fmt == "gif":
        vf += (f",split[a][b];[a]palettegen=max_colors={int(colors)}:stats_mode=diff[p];"
               f"[b][p]paletteuse=dither={dither}:diff_mode=rectangle")
        enc = ["-loop", "0"]
    else:
        enc = ["-c:v", "libwebp_anim", "-quality", str(int(quality)), "-compression_level", "6", "-loop", "0"]
    dest = Path(dest)
    tmp = dest.with_name(dest.name + ".part")
    r = subprocess.run([*thumbs.NICE, "ffmpeg", "-v", "error", "-y", "-i", str(mov), "-an", "-vf", vf, *enc, "-f", fmt, str(tmp)],
                       capture_output=True, text=True)
    if r.returncode:
        tmp.unlink(missing_ok=True)
        err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "ffmpeg failed"
        if "libwebp_anim" in r.stderr:
            err = "this ffmpeg has no WebP encoder (needs libwebp)"
        raise RuntimeError(err)
    os.replace(tmp, dest)
    return dest.stat().st_size


def settings():
    return {**DEFAULTS, **library.load_config().get("export", {})}


class ExportDialog(Adw.Dialog):
    """Options for one export; "Choose file…" calls on_go(settings dict) and closes."""

    def __init__(self, fmt, on_go):
        super().__init__(title=f"Save as {fmt.upper()}", content_width=420)
        s = settings()
        group = Adw.PreferencesGroup(description="For sharing outside the app — these files can't be used as live "
                                                 "wallpapers. Smaller width, fewer frames and fewer colors all "
                                                 "shrink the file.")
        self.width = Adw.ComboRow(title="Width", model=Gtk.StringList.new([n for n, _ in WIDTHS]),
                                  selected=next((i for i, (_, v) in enumerate(WIDTHS) if v == s["width"]), 3))
        self.fps = Adw.SpinRow(title="Frame rate", subtitle="Frames per second",
                               adjustment=Gtk.Adjustment(value=s["fps"], lower=5, upper=30, step_increment=1))
        group.add(self.width)
        group.add(self.fps)
        if fmt == "gif":
            self.colors = Adw.SpinRow(title="Colors", subtitle="2–256 (8-bit = 256); fewer is smaller",
                                      adjustment=Gtk.Adjustment(value=s["colors"], lower=2, upper=256,
                                                                step_increment=8))
            self.dither = Adw.ComboRow(title="Dithering", model=Gtk.StringList.new(list(DITHERS.values())),
                                       selected=list(DITHERS).index(s["dither"]) if s["dither"] in DITHERS else 0)
            group.add(self.colors)
            group.add(self.dither)
        else:
            self.quality = Adw.SpinRow(title="Quality", subtitle="0–100; lower is smaller",
                                       adjustment=Gtk.Adjustment(value=s["quality"], lower=0, upper=100,
                                                                 step_increment=5))
            group.add(self.quality)
        go = Gtk.Button(label="Choose file…", halign=Gtk.Align.CENTER, css_classes=["suggested-action", "pill"],
                        margin_top=6)
        go.connect("clicked", lambda *_: self._go(fmt, on_go))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12, margin_bottom=18,
                      margin_start=18, margin_end=18)
        box.append(group)
        box.append(go)
        tv = Adw.ToolbarView(content=box)
        tv.add_top_bar(Adw.HeaderBar())
        self.set_child(tv)

    def _go(self, fmt, on_go):
        s = {"width": WIDTHS[self.width.get_selected()][1], "fps": int(self.fps.get_value())}
        if fmt == "gif":
            s.update(colors=int(self.colors.get_value()), dither=list(DITHERS)[self.dither.get_selected()])
        else:
            s["quality"] = int(self.quality.get_value())
        library.set_pref("export", {**settings(), **s})
        self.close()
        on_go(s)


def save_as(parent, fmt, name, render, busy=None):
    """The whole GIF/WebP flow shared by the editor, the collage maker and the gallery menu: options dialog →
    file picker → render(tmpdir) -> clip path (in a thread) → encode → toast. busy(bool) greys the caller's button."""
    win = parent if isinstance(parent, Gtk.Window) else parent.get_root()

    def go(s):
        dlg = Gtk.FileDialog(title=f"Save as {fmt.upper()}", initial_name=f"{Path(name).stem}.{fmt}",
                             initial_folder=Gio.File.new_for_path(str(Path.home() / "Pictures")))

        def picked(d, res):
            try:
                dest = d.save_finish(res).get_path()
            except GLib.Error:
                return
            busy and busy(True)

            def work():
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        size = encode(render(tmp), dest, fmt, **s)
                    msg = f"Saved {Path(dest).name} ({size / 1e6:.1f} MB)"
                except Exception as ex:
                    thumbs.log("export failed:", ex)
                    msg = f"Export failed: {ex}"
                GLib.idle_add(done, msg)

            def done(msg):
                busy and busy(False)
                win.toast(msg)
            threading.Thread(target=work, daemon=True).start()
        dlg.save(win, None, picked)
    ExportDialog(fmt, go).present(win)


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        mov = Path(d, "a.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2", str(mov)],
                       check=True)
        big, small = Path(d, "big.gif"), Path(d, "small.gif")
        a = encode(mov, big, "gif", width=0, fps=15, colors=256)
        b = encode(mov, small, "gif", width=160, fps=8, colors=16, dither="none")
        assert big.read_bytes()[:6] == b"GIF89a" and b < a, (a, b)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width", "-of", "csv=p=0", str(small)],
                               capture_output=True, text=True).stdout.strip()
        assert probe == "160", probe
        if "libwebp_anim" in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True,
                                            text=True).stdout:
            w1, w2 = Path(d, "q90.webp"), Path(d, "q10.webp")
            assert encode(mov, w1, "webp", quality=90) > encode(mov, w2, "webp", quality=10)
            assert w1.read_bytes()[8:12] == b"WEBP"
        assert not list(Path(d).glob("*.part"))
    print("export ok")
