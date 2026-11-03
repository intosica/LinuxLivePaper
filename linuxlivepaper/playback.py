"""Media streams + the live-image canvas with the press-to-play "immersive" transition."""
import math

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Graphene, Gtk  # noqa: E402

# Press transition knobs (tune by feel).
T_IN, T_OUT = 0.55, 0.40   # seconds to go still→video and back
BLUR_PX = 22.0             # peak gaussian blur radius
ZOOM_BLUR = 0.09           # peak radial motion-blur spread (fraction of size)
ZOOM_BLUR_COPIES = 7
HOLD_ZOOM = 0.035          # how much the image pushes in while held


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


def curves(p):
    """Transition state at progress p (0 = still, 1 = fully live): (video mix, blur 0..1, push-in zoom)."""
    return smoothstep(0.25, 0.75, p), math.sin(math.pi * min(max(p, 0.0), 1.0)), 1 + HOLD_ZOOM * smoothstep(0, 1, p)


class LiveCanvas(Gtk.Widget):
    """Draws the still and the motion video. press() runs the iOS-style transition: the still pushes in and smears
    into a radial motion blur, the video fades in through the blur and sharpens. release() reverses it."""

    def __init__(self, still, motion_path, on_idle=None):
        super().__init__(hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN)
        self.still, self.path, self.on_idle = still, motion_path, on_idle
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
            self.stream.set_playing(False)
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
        step = dt / (T_IN if self.target > self.p else T_OUT)
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
        mix, blur, zoom = curves(self.p)
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
    print("playback ok")
