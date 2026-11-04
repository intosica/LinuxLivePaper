"""Media streams + the live-image canvas with the press-to-play "immersive" transition."""
import math
import os

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Graphene, Gtk  # noqa: E402

# Press transition knobs (tune by feel).
T_IN, T_OUT = 0.55, 0.40   # seconds to go still→video and back
BLUR_PX = 22.0             # peak gaussian blur radius
ZOOM_BLUR = 0.09           # peak radial motion-blur spread (fraction of size)
ZOOM_BLUR_COPIES = 7
HOLD_ZOOM = 0.035          # how much the image pushes in while held
# Press styles: id → (label, blur scale, push-in scale, duration scale). "blur" is the iOS-like immersive one.
STYLES = {
    "blur": ("Immersive blur", 1.0, 1.0, 1.0),
    "zoom": ("Zoom", 0.0, 2.0, 0.8),
    "fade": ("Fade", 0.0, 0.0, 0.7),
    "none": ("Instant", 0.0, 0.0, 0.0),
}
DEFAULT_STYLE = "blur"


def media_file(path, muted=True, loop=False):
    """Gtk.MediaFile that really honours `muted`. PipeWire restores the last mute state per app, so after a muted
    hover preview a fresh stream comes up muted at the sink while GTK still reports muted=False (and never re-sends
    it). Re-asserting the state once the pipeline is prepared fixes that."""
    m = Gtk.MediaFile.new_for_filename(path)
    m.set_loop(loop)
    m.set_muted(muted)

    def prepared(s, _):
        if s.is_prepared():
            want = s.get_muted()
            s.set_muted(not want)
            s.set_muted(want)
    m.connect("notify::prepared", prepared)
    return m


GIF_MAX_BYTES = 40_000_000  # GdkPixbuf keeps every decoded frame in RAM: bigger GIFs stay a still


class GifPlay:
    """Plays a GIF into a Gtk.Picture with GdkPixbuf alone — no ffmpeg proxy, no GStreamer pipeline, no GPU decoder.
    Same set_playing/clear surface as a media stream, so release() drops it like one."""

    def __init__(self, anim, picture):
        self.picture, self.it, self.id = picture, anim.get_iter(None), 0
        self._show()

    @staticmethod
    def load(path):
        """GdkPixbuf.PixbufAnimation or None (too big / unreadable). Safe off the main thread."""
        try:
            if os.path.getsize(path) > GIF_MAX_BYTES:
                return None
            return GdkPixbuf.PixbufAnimation.new_from_file(path)
        except (OSError, GLib.Error):
            return None

    def _show(self):
        self.picture.set_paintable(Gdk.Texture.new_for_pixbuf(self.it.get_pixbuf()))
        d = self.it.get_delay_time()
        if d >= 0:  # -1 = a still image
            self.id = GLib.timeout_add(max(20, d), self._next)

    def _next(self):
        self.id = 0
        self.it.advance(None)
        self._show()
        return False

    def set_playing(self, on):
        if not on and self.id:
            GLib.source_remove(self.id)
            self.id = 0

    def clear(self):
        self.set_playing(False)


def release(m):
    """Really free a stream. set_playing(False) only pauses: the GStreamer pipeline (and its NVDEC decoder, VRAM and
    GPU channel) lives until the object dies, and Python closures keep it alive. Leaking these exhausted the NVIDIA
    driver's channels and froze the desktop."""
    if m is not None:
        m.set_playing(False)
        m.clear()


def play_stream(path, picture, muted=True, loop=True):
    """Play path and swap it into picture once the first frame is decoded (no black flash)."""
    m = media_file(path, muted, loop)

    def first_frame(stream):
        stream.disconnect(hid)
        if stream.get_playing():
            picture.set_paintable(stream)
    hid = m.connect("invalidate-contents", first_frame)
    m.play()
    return m


def smoothstep(a, b, x):
    x = min(max((x - a) / (b - a), 0.0), 1.0)
    return x * x * (3 - 2 * x)


def curves(p, style=DEFAULT_STYLE):
    """Transition state at progress p (0 = still, 1 = fully live): (video mix, blur 0..1, push-in zoom)."""
    _, blur, zoom, _ = STYLES.get(style, STYLES[DEFAULT_STYLE])
    mix = smoothstep(0.25, 0.75, p) if blur else smoothstep(0, 1, p)
    return mix, blur * math.sin(math.pi * min(max(p, 0.0), 1.0)), 1 + zoom * HOLD_ZOOM * smoothstep(0, 1, p)


class LiveCanvas(Gtk.Widget):
    """Draws the still and the motion video. press() runs the iOS-style transition: the still pushes in and smears
    into a radial motion blur, the video fades in through the blur and sharpens. release() reverses it."""

    def __init__(self, still, motion_path, on_idle=None, style=DEFAULT_STYLE):
        super().__init__(hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN)
        self.still, self.path, self.on_idle, self.style = still, motion_path, on_idle, style
        self.stream = None
        self.p, self.target = 0.0, 0.0
        self.tick_id = 0
        self.last = None

    # --- control --------------------------------------------------------------------------------
    def press(self):
        if self.stream is None or self.stream.get_ended():
            self._drop_stream()
            self.stream = media_file(self.path, muted=False)
            self.stream.connect("invalidate-contents", lambda *_: self.queue_draw())
            self.stream.connect("notify::ended", lambda s, _: s.get_ended() and self.release())
            self.stream.play()
        self._animate_to(1.0)

    def release(self):
        self._animate_to(0.0)

    def is_live(self):
        return self.target > 0

    def stop(self):
        self.p = self.target = 0.0
        self._drop_stream()
        self.queue_draw()

    def _drop_stream(self):
        if self.stream:
            release(self.stream)
            self.stream = None

    def _animate_to(self, target):
        self.target = target
        if not self.tick_id:
            self.last = None
            self.tick_id = self.add_tick_callback(self._tick)

    def _tick(self, _w, clock):
        now = clock.get_frame_time() / 1e6
        dt = 0.0 if self.last is None else min(now - self.last, 0.05)
        self.last = now
        dur = (T_IN if self.target > self.p else T_OUT) * STYLES.get(self.style, STYLES[DEFAULT_STYLE])[3]
        step = dt / dur if dur else 1.0
        self.p = min(self.p + step, self.target) if self.target > self.p else max(self.p - step, self.target)
        self.queue_draw()
        if self.p != self.target:
            return True
        self.tick_id = 0
        if self.p == 0.0:
            self._drop_stream()
            if self.on_idle:
                self.on_idle()
        return False

    # --- drawing --------------------------------------------------------------------------------
    def _video_ready(self):
        return self.stream is not None and self.stream.get_intrinsic_width() > 0

    def do_snapshot(self, snap):
        W, H = self.get_width(), self.get_height()
        iw, ih = self.still.get_intrinsic_width(), self.still.get_intrinsic_height()
        if not (W and H and iw and ih):
            return
        mix, blur, zoom = curves(self.p, self.style)
        if not self._video_ready():
            mix = 0.0
        fit = min(W / iw, H / ih)
        w, h = iw * fit, ih * fit

        snap.push_clip(Graphene.Rect().init(0, 0, W, H))
        radius = BLUR_PX * blur
        if radius > 0.5:
            snap.push_blur(radius)
        for paintable, alpha in ((self.still, 1.0), (self.stream, mix)):
            if alpha <= 0.01:
                continue
            snap.push_opacity(alpha)
            # Radial motion blur: copies scaled progressively outward, averaged (copy k drawn at 1/(k+1) opacity).
            copies = ZOOM_BLUR_COPIES if blur > 0.02 else 1
            for k in range(copies):
                s = zoom * (1 + ZOOM_BLUR * blur * k / max(1, copies - 1))
                snap.push_opacity(1.0 / (k + 1))
                snap.save()
                snap.translate(Graphene.Point().init(W / 2, H / 2))
                snap.scale(s, s)
                snap.translate(Graphene.Point().init(-w / 2, -h / 2))
                paintable.snapshot(snap, w, h)
                snap.restore()
                snap.pop()
            snap.pop()
        if radius > 0.5:
            snap.pop()
        snap.pop()


if __name__ == "__main__":
    assert curves(0) == (0.0, 0.0, 1.0)
    mix, blur, zoom = curves(1)
    assert mix == 1.0 and blur < 1e-9 and abs(zoom - (1 + HOLD_ZOOM)) < 1e-9
    mix, blur, _ = curves(0.5)
    assert mix == 0.5 and blur == 1.0  # peak smear right at the crossover
    assert curves(0.5, "fade")[1:] == (0.0, 1.0) and curves(1, "zoom")[2] > 1 + HOLD_ZOOM
    print("playback ok")
