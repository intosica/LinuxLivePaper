"""Multi-video collage: several clips playing at once in a layout, each with its own start, volume, speed,
direction, filter and effect. One pass lasts as long as the longest cell; shorter cells loop to fill it. Rendered in two stages: each cell
once at its final size (parallel, cacheable), then one compose pass that loops them, overlays and mixes audio."""
import hashlib
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import effects, thumbs
from .livemake import finish
from . import overlays as ovmod

# id → (label, cells as (x, y, w, h) fractions of the canvas)
LAYOUTS = {
    "2h": ("Side by side", [(0, 0, .5, 1), (.5, 0, .5, 1)]),
    "2v": ("Stacked", [(0, 0, 1, .5), (0, .5, 1, .5)]),
    "3c": ("Three columns", [(0, 0, 1 / 3, 1), (1 / 3, 0, 1 / 3, 1), (2 / 3, 0, 1 / 3, 1)]),
    "3r": ("Three rows", [(0, 0, 1, 1 / 3), (0, 1 / 3, 1, 1 / 3), (0, 2 / 3, 1, 1 / 3)]),
    "1+2": ("Big left", [(0, 0, .5, 1), (.5, 0, .5, .5), (.5, .5, .5, .5)]),
    "2+1": ("Big bottom", [(0, 0, .5, .5), (.5, 0, .5, .5), (0, .5, 1, .5)]),
    "4": ("Grid of 4", [(0, 0, .5, .5), (.5, 0, .5, .5), (0, .5, .5, .5), (.5, .5, .5, .5)]),
    "1+3": ("Big top", [(0, 0, 1, 2 / 3), (0, 2 / 3, 1 / 3, 1 / 3), (1 / 3, 2 / 3, 1 / 3, 1 / 3),
                        (2 / 3, 2 / 3, 1 / 3, 1 / 3)]),
    "6": ("Grid of 6", [(x / 2, y / 3, .5, 1 / 3) for y in range(3) for x in range(2)]),
    "9": ("Grid of 9", [(x / 3, y / 3, 1 / 3, 1 / 3) for y in range(3) for x in range(3)]),
}
ASPECTS = {"9:16": ("Portrait 9:16", 1080, 1920), "3:4": ("Portrait 3:4", 1080, 1440),
           "1:1": ("Square", 1080, 1080), "16:9": ("Landscape 16:9", 1920, 1080)}
GAPS = {0: "None", 8: "Thin", 24: "Thick"}  # px at full size
BACKGROUNDS = {"black": "Black", "white": "White"}
REPEATS = range(1, 11)  # how many times the whole collage plays back to back in the saved file
MAX_WINDOW = 30.0  # untrimmed clips use at most this much (drag the window on the timeline for more)
FPS = 30


def new(layout="2h", aspect="9:16"):
    return {"layout": layout, "aspect": aspect, "repeat": 1, "gap": 8, "bg": "black", "cover": 0.0,
            "cells": [None] * len(LAYOUTS[layout][1])}


def new_cell(src):
    dur, w, h = thumbs.probe(str(src))
    return {"src": str(src), "start": 0.0, "len": None, "volume": 1.0, "opts": dict(effects.DEFAULT),
            "zoom": 1.0, "pan": [0.5, 0.5], "dur": dur or 1.0, "ar": w / h if w and h else 16 / 9}


def clamp_pan(cell):
    """Pan as 0..1 of the slack inside the zoomed cell: 0 is flush left/top, 1 flush right/bottom."""
    pan = cell.get("pan") or (0.5, 0.5)
    return (min(1.0, max(0.0, float(pan[0]))), min(1.0, max(0.0, float(pan[1]))))


def fit_size(cell, w, h, ar=None):
    """(zoomed width, zoomed height) of a cell's source after the cover scale — both at least w×h."""
    z = max(1.0, float(cell.get("zoom") or 1.0))
    ar = ar or cell.get("ar") or 16 / 9
    tw, th = max(2, round(w * z) // 2 * 2), max(2, round(h * z) // 2 * 2)
    return max(tw, round(th * ar) // 2 * 2), max(th, round(tw / ar) // 2 * 2)


def cell_fit(cell, w, h, ar=None):
    """Filters filling a w×h cell with no black bars, honouring the cell's zoom and pan (crop stays inside)."""
    zw, zh = fit_size(cell, w, h, ar)
    px, py = clamp_pan(cell)
    return [f"scale={zw}:{zh}:force_original_aspect_ratio=increase:flags=bicubic",
            f"crop={w}:{h}:(iw-ow)*{px:.4f}:(ih-oh)*{py:.4f}", "setsar=1"]


def set_layout(c, layout):
    """Switch layout, keeping clips in order (extra ones are dropped, new slots are empty)."""
    n = len(LAYOUTS[layout][1])
    c["layout"], c["cells"] = layout, (c["cells"] + [None] * n)[:n]


def window(cell):
    """Source seconds the cell plays: the trim the user dragged on the timeline, else the rest of the clip."""
    n = max(0.2, float(cell["len"])) if cell.get("len") else \
        max(0.2, min(float(cell.get("dur") or 1.0) - cell["start"], MAX_WINDOW))
    # reverse/bounce hold every frame in RAM: an hour-long window would eat it all
    return min(n, MAX_WINDOW) if cell["opts"].get("direction", "forward") != "forward" else n


def cell_length(cell):
    """Rendered seconds of one pass of the cell (after speed and bounce)."""
    return effects.out_length(window(cell), {**effects.DEFAULT, **cell["opts"]})


def match(c, longest=True):
    """Make every clip's rendered pass as long as the longest (or shortest) one by resizing its window; a window
    that would run past the end of its clip slides earlier (and wraps/loops only if the clip is shorter still)."""
    cells = [x for x in c["cells"] if x]
    if not cells:
        return
    target = (max if longest else min)(cell_length(x) for x in cells)
    for x in cells:
        o = {**effects.DEFAULT, **x["opts"]}
        n = max(0.2, round(target * o["speed"] / (2 if o["direction"] == "bounce" else 1), 3))
        if abs(cell_length(x) - target) < 0.01:
            continue
        x["len"] = n
        x["start"] = round(max(0.0, min(x["start"], float(x.get("dur") or n) - n)), 3)


HEAD = 4.0  # seconds of the collage a preview renders first, while the rest renders behind it


def head(c, secs=HEAD):
    """Copy of c cut to its first `secs` seconds, so a preview can start long before the whole collage is rendered;
    None when that wouldn't save much or can't be cut (reverse/bounce need their whole window)."""
    cells = [x for x in c["cells"] if x]
    if duration(c) < secs * 1.5 or any(x["opts"].get("direction", "forward") != "forward" for x in cells):
        return None
    cut = [None if not x else dict(x, len=round(secs * x["opts"].get("speed", 1.0), 3))
           if cell_length(x) > secs else x for x in c["cells"]]
    return {**c, "cells": cut}


def duration(c):
    """One pass of the collage = its longest cell; shorter cells loop to fill it."""
    return max((cell_length(x) for x in c["cells"] if x), default=1.0)


def cell_time(cell, t):
    """Source time the cell shows at collage time t (short cells loop)."""
    o = {**effects.DEFAULT, **cell["opts"]}
    t = cell["start"] + effects.src_time(t % max(cell_length(cell), 0.01), window(cell), o)
    return t % max(float(cell.get("dur") or 1.0), 0.05)  # a window running past the end wraps around


def size(c, height=None):
    _, w, h = ASPECTS[c["aspect"]]
    return effects.out_size(w, h, height)


def rects(c, w, h):
    """Pixel rect (x, y, w, h) per cell: gap is uniform between cells and at the edges; sizes are even."""
    g = c["gap"] * w / ASPECTS[c["aspect"]][1]
    out = []
    for fx, fy, fw, fh in LAYOUTS[c["layout"]][1]:
        x, y = round(g + fx * (w - g)), round(g + fy * (h - g))
        x1, y1 = round(g + (fx + fw) * (w - g)) - round(g), round(g + (fy + fh) * (h - g)) - round(g)
        out.append((x, y, max(2, (x1 - x) // 2 * 2), max(2, (y1 - y) // 2 * 2)))
    return out


def cell_at(c, fx, fy):
    """Index of the cell containing the canvas point (fx, fy) in 0..1, or None."""
    for i, (x, y, w, h) in enumerate(LAYOUTS[c["layout"]][1]):
        if x <= fx < x + w and y <= fy < y + h:
            return i
    return None


def _cell_command(cell, w, h, out, fast):
    """One pass of a cell at its final size: trim → frame → effects → speed/direction, video + PCM audio."""
    o = {**effects.DEFAULT, **cell["opts"]}
    L = cell_length(cell)
    v = [thumbs.SQUARE] + cell_fit(cell, w, h)  # cell-size frames first: later filters and reverse stay cheap
    v += [f for f in (effects.frame_chain(o), f"setpts=(PTS-STARTPTS)/{o['speed']}") if f]
    if o["direction"] == "reverse":
        v.append("reverse")
    elif o["direction"] == "bounce":
        v.append("split[f][b];[b]reverse[r];[f][r]concat=n=2:v=1:a=0")
    v += [f"fps={FPS}", "tpad=stop_mode=clone:stop_duration=1", f"trim=duration={L:.3f}", "setpts=PTS-STARTPTS",
          "format=yuv420p"]
    graph, amap = [f"[0:v]{','.join(v)}[v]"], []
    if cell["volume"] > 0 and effects.has_audio(cell["src"]):
        a = ["asetpts=PTS-STARTPTS"] + ([f"atempo={o['speed']}"] if o["speed"] != 1 else [])
        if o["direction"] == "reverse":
            a.append("areverse")
        elif o["direction"] == "bounce":
            a.append("asplit[af][ab];[ab]areverse[ar];[af][ar]concat=n=2:v=0:a=1")
        a += [f"volume={cell['volume']:.2f}", "aresample=48000", "aformat=channel_layouts=stereo", "apad",
              f"atrim=duration={L:.3f}"]
        graph.append(f"[0:a:0]{','.join(a)}[a]")
        amap = ["-map", "[a]", "-c:a", "pcm_s16le"]
    # loop short sources (GIFs, a window past the end) so the window is always full
    zw, zh = fit_size(cell, w, h)
    low = ["-lowres", str(n)] if fast and (n := thumbs.lowres(cell["src"], zw, zh)) else []
    return ["ffmpeg", "-v", "error", "-y", "-stream_loop", "-1", "-ss", f"{cell['start']:.3f}",
            "-t", f"{window(cell):.3f}", *(low or thumbs.hwdec(cell["src"], (zw, zh))), "-i", cell["src"], "-filter_complex", ";".join(graph), "-map", "[v]", *amap,
            *thumbs.h264(True, (w, h), 20 if fast else 12), "-r", str(FPS), str(out)]


def _cell_file(cell, w, h, fast, d):
    """Render (or reuse from cache dir d) one cell pass; returns (path, has_audio)."""
    key = hashlib.sha1(repr(([[k, v] for k, v in sorted(cell.items()) if k not in ("dur", "ar")],
                             os.stat(cell["src"]).st_mtime_ns, w, h, fast)).encode()).hexdigest()
    out = thumbs.cached(Path(d, key + ".mkv"), lambda tmp: thumbs.run(_cell_command(cell, w, h, tmp, fast)))
    return str(out), cell["volume"] > 0 and effects.has_audio(cell["src"])  # same test _cell_command used


def command(c, cells, out, height=None, fast=False, extra=(), overlays=()):
    """ffmpeg argv composing pre-rendered cell passes [(index, path, has_audio)] into one pass of the collage."""
    W, H = size(c, height)
    D = duration(c)
    inputs, chains, audio, prev = [], [f"color=c={c['bg']}:s={W}x{H}:r={FPS}:d={D:.3f}[bg]"], [], "bg"
    for n, (i, path, has_a) in enumerate(cells):
        x, y, _, _ = rects(c, W, H)[i]
        inputs += ["-stream_loop", "-1", "-i", path]  # shorter cells loop until the longest one ends
        chains.append(f"[{prev}][{n}:v]overlay={x}:{y}:eof_action=pass[o{n}]")
        prev = f"o{n}"
        if has_a:
            audio.append(f"[{n}:a:0]")
    graph = chains + [f"[{prev}]trim=duration={D:.3f},format=yuv420p[vout]"]
    vmap = ["-map", "[vout]"]
    if overlays:
        ov_in, steps, label = ovmod.graph(overlays, W, H, D, first_input=len(cells))
        inputs += ov_in
        graph += steps + [f"[{label}]format=yuv420p[fin]"]
        vmap = ["-map", "[fin]"]
    amap = []
    if audio:
        graph.append(f"{''.join(audio)}amix=inputs={len(audio)}:normalize=0:duration=longest,"
                     f"atrim=duration={D:.3f}[aout]")
        amap = ["-map", "[aout]", "-c:a", "aac"]
    return ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(graph), *vmap, *amap,
            *thumbs.h264(fast, (W, H)),
            "-r", str(FPS), "-t", f"{D:.3f}", *extra, str(out)]


def render(c, out, height=None, fast=False, extra=(), overlays=(), cache=None, repeat=None, interpolate=None):
    """Render the collage to `out`: one pass of the longest cell, played `repeat` (default c["repeat"]) times.
    cache: directory to keep cell passes in, so editing one cell only re-renders that cell.
    interpolate: interp settings applied to the one pass before repeating (saved files only, never previews)."""
    if not any(c["cells"]):
        raise ValueError("Add at least one video to the collage")
    repeat = max(1, int(repeat or c.get("repeat") or 1))
    W, H = size(c, height)
    with tempfile.TemporaryDirectory() as tmp:
        d = cache or tmp
        Path(d).mkdir(parents=True, exist_ok=True)
        todo = [(i, x, *rects(c, W, H)[i][2:]) for i, x in enumerate(c["cells"]) if x]
        with ThreadPoolExecutor(max_workers=max(1, min(4, os.cpu_count() or 2))) as ex:
            files = list(ex.map(lambda t: (t[0], *_cell_file(t[1], t[2], t[3], fast, d)), todo))
        # a hung ffmpeg must never wedge the preview thread pool
        finish(lambda o, ex: thumbs.run(command(c, files, o, height, fast, ex, overlays)), out, tmp, repeat,
               interpolate, list(extra))
    return out


if __name__ == "__main__":
    import json
    import tempfile
    from pathlib import Path
    probe = ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json"]
    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d, "a.mp4"), Path(d, "b.gif")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=4", "-f", "lavfi",
                        "-i", "sine=d=4", "-shortest", str(a)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=100x80:d=1", str(b)], check=True)
        assert cell_at(new(), .25, .5) == 0 and cell_at(new(), .75, .5) == 1
        c = new("1+2", "1:1")
        r = rects(c, 1080, 1080)
        assert r[0][0] == 8 and r[1][0] + r[1][2] == 1072 and r[1][1] + r[1][3] + 8 + r[2][3] == 1072, r
        c["cells"] = [new_cell(a), new_cell(b), None]
        assert abs(window(new_cell(a)) - 4.0) < 0.1                # default: the whole clip
        assert window({**new_cell(a), "len": 1.0}) == 1.0           # a dragged trim sticks
        assert window({**new_cell(a), "start": 3.0}) < 1.1          # the rest of the clip after the start
        c["cells"][0].update(volume=0.5, opts={**effects.DEFAULT, "look": "mono"})
        c["cells"][1]["opts"]["direction"] = "bounce"  # a 1 s GIF bouncing (2 s) loops twice in the 4 s collage
        assert abs(duration(c) - 4.0) < 0.1 and abs(cell_length(c["cells"][1]) - 2.0) < 0.1
        assert abs(cell_time(c["cells"][1], 2.5) - 0.5) < 0.05     # second loop of the short cell
        out = render(c, Path(d, "c.mp4"), height=360, fast=True, cache=Path(d, "cells"))
        j = json.loads(subprocess.run([*probe, str(out)], capture_output=True, text=True).stdout)
        assert [(s["width"], s["height"]) for s in j["streams"] if s["codec_type"] == "video"] == [(360, 360)], j
        assert abs(float(j["format"]["duration"]) - 4.0) < 0.15, j["format"]
        assert any(s["codec_type"] == "audio" for s in j["streams"])
        assert len(list(Path(d, "cells").glob("*.mkv"))) == 2
        c["cells"][0]["opts"]["speed"] = 2.0  # now both last 2 s; played 3 times back to back
        c["repeat"] = 3
        j = json.loads(subprocess.run([*probe, str(render(c, Path(d, "r.mp4"), height=360, fast=True,
                                                          cache=Path(d, "cells")))], capture_output=True, text=True).stdout)
        assert abs(float(j["format"]["duration"]) - 6.0) < 0.3, j["format"]
        assert len(list(Path(d, "cells").glob("*.mkv"))) == 3  # the unchanged GIF cell came from the cache
        assert head(c, 1.0) is None  # the bouncing cell can't be cut
        c["cells"][1]["opts"]["direction"] = "forward"
        h = head(c, 1.0)  # the 2x cell lasts 2 s: its head keeps 2 source seconds = 1 s of output
        assert h and abs(duration(h) - 1.0) < 0.02 and abs(h["cells"][0]["len"] - 2.0) < 0.01, h["cells"]
        assert head(c, 1.9) is None and c["cells"][0]["len"] is None
        c["cells"][1]["opts"]["direction"] = "bounce"
        c["repeat"] = 1
        set_layout(c, "2h")
        assert len(c["cells"]) == 2 and c["cells"][1]["src"] == str(b)
        c["cells"][0]["volume"] = 0  # muted cells add no audio track
        c["cells"][0]["start"] = 3.5  # starts near the end: loops around
        j = json.loads(subprocess.run([*probe, str(render(c, Path(d, "m.mp4"), height=240, fast=True))],
                                      capture_output=True, text=True).stdout)
        assert [s["codec_type"] for s in j["streams"]] == ["video"], j
        # overlays over the whole collage
        ovs = [ovmod.text("Collage", size=0.12), ovmod.text("Turned", rotation=20, y=0.3)]
        j = json.loads(subprocess.run([*probe, str(render(c, Path(d, "o.mp4"), height=240, fast=True, overlays=ovs))],
                                      capture_output=True, text=True).stdout)
        assert [(s["width"], s["height"]) for s in j["streams"] if s["codec_type"] == "video"] == [(240, 240)], j
        from . import interp  # AI slow motion (only when a PyTorch Python + RIFE model are set up here)
        sl = {**interp.DEFAULTS, "engine": "rife", "factor": 2, "mode": "slow"}
        if interp.python(sl) and interp.installed(sl):
            j = json.loads(subprocess.run([*probe, str(render(c, Path(d, "i.mp4"), height=240, repeat=2,
                                                              interpolate=sl))], capture_output=True, text=True).stdout)
            assert abs(float(j["format"]["duration"]) - 4 * duration(c)) < 0.3, j["format"]
        # zoom/pan: the crop always fills the cell, whatever the pan is
        zw, zh = fit_size({"zoom": 2.0}, 360, 640, 16 / 9)
        assert zw >= 720 and zh >= 640, (zw, zh)
        assert clamp_pan({"pan": (-3, 9)}) == (0.0, 1.0)
        assert clamp_pan({}) == (0.5, 0.5)
        z = {"cells": [dict(c["cells"][0], zoom=2.5, pan=[1.0, 0.0]), None]}
        z.update({k: v for k, v in c.items() if k != "cells"})
        j = json.loads(subprocess.run([*probe, str(render(z, Path(d, "z.mp4"), height=240, fast=True))],
                                      capture_output=True, text=True).stdout)
        assert [(s["width"], s["height"]) for s in j["streams"] if s["codec_type"] == "video"] == [(240, 240)], j
        # match lengths: 4 s clip vs 1 s bounce (2 s) vs 10 s clip at 2x (5 s)
        m = new("3c")
        m["cells"] = [new_cell(a), new_cell(a), new_cell(a)]
        m["cells"][1].update(len=1.0, opts={**effects.DEFAULT, "direction": "bounce"})
        m["cells"][2]["opts"] = {**effects.DEFAULT, "speed": 2.0}
        match(m, longest=True)
        assert all(abs(cell_length(x) - 4.0) < 0.02 for x in m["cells"]), [cell_length(x) for x in m["cells"]]
        m["cells"][0].update(len=1.0)
        match(m, longest=False)
        assert all(abs(cell_length(x) - 1.0) < 0.02 for x in m["cells"]), [cell_length(x) for x in m["cells"]]
    print("collage ok")
