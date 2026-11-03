"""Multi-video collage: several clips playing at once in a layout, each with its own start, volume, speed,
direction, filter and effect. Rendered as ONE ffmpeg filter_complex (scale/crop each cell, overlay, amix)."""
import subprocess

from . import effects
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
DURATIONS = (1.5, 2.0, 3.0, 5.0)
FPS = 30


def new(layout="2h", aspect="9:16"):
    return {"layout": layout, "aspect": aspect, "duration": 3.0, "gap": 8, "bg": "black", "cover": 0.0,
            "cells": [None] * len(LAYOUTS[layout][1])}


def new_cell(src):
    return {"src": str(src), "start": 0.0, "len": None, "volume": 1.0, "opts": dict(effects.DEFAULT)}


def set_layout(c, layout):
    """Switch layout, keeping clips in order (extra ones are dropped, new slots are empty)."""
    n = len(LAYOUTS[layout][1])
    c["layout"], c["cells"] = layout, (c["cells"] + [None] * n)[:n]


def src_length(duration, opts):
    """Source seconds a cell needs so its rendered clip lasts `duration`."""
    return duration * opts["speed"] / (2 if opts["direction"] == "bounce" else 1)


def window(cell, duration):
    """Source seconds taken from the clip: the trim length the user set (or dragged on the timeline), else
    exactly what the collage duration needs. Shorter than that freezes on the last frame, which is the point."""
    need = src_length(duration, {**effects.DEFAULT, **cell["opts"]})
    length = cell.get("len")
    return need if not length else max(0.2, min(float(length), need))


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


def command(c, out, height=None, fast=False, extra=(), overlays=()):
    """ffmpeg argv rendering the collage to `out`; `overlays` are text/stickers drawn over the whole collage."""
    W, H = size(c, height)
    D = c["duration"]
    inputs, chains, lays, audio = [], [], [], []
    prev = "bg"
    chains.append(f"color=c={c['bg']}:s={W}x{H}:r={FPS}:d={D:.3f}[bg]")
    cells = [(i, cell) for i, cell in enumerate(c["cells"]) if cell]
    for n, (i, cell) in enumerate(cells):
        x, y, w, h = rects(c, W, H)[i]
        o = {**effects.DEFAULT, **cell["opts"]}
        # loop short sources (GIFs) so every cell fills the whole duration
        inputs += ["-stream_loop", "-1", "-ss", f"{cell['start']:.3f}", "-t", f"{window(cell, D):.3f}",
                   "-i", cell["src"]]
        # scale down to the cell first: every later filter (and reverse's frame buffer) works on cell-size frames
        v = [f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=bicubic", f"crop={w}:{h}", "setsar=1"]
        v += [f for f in (effects.frame_chain(o), f"setpts=(PTS-STARTPTS)/{o['speed']}") if f]
        if o["direction"] == "reverse":
            v.append("reverse")
        elif o["direction"] == "bounce":
            v.append(f"split[c{n}f][c{n}b];[c{n}b]reverse[c{n}r];[c{n}f][c{n}r]concat=n=2:v=1:a=0")
        v += [f"fps={FPS}", f"tpad=stop_mode=clone:stop_duration={D:.3f}"]
        chains.append(f"[{n}:v]{','.join(v)}[c{n}]")
        nxt = f"o{n}"
        lays.append(f"[{prev}][c{n}]overlay={x}:{y}:eof_action=pass[{nxt}]")
        prev = nxt
        if cell["volume"] > 0 and effects.has_audio(cell["src"]):
            a = ["asetpts=PTS-STARTPTS"] + ([f"atempo={o['speed']}"] if o["speed"] != 1 else [])
            if o["direction"] == "reverse":
                a.append("areverse")
            elif o["direction"] == "bounce":
                a.append(f"asplit[a{n}f][a{n}b];[a{n}b]areverse[a{n}r];[a{n}f][a{n}r]concat=n=2:v=0:a=1")
            a += [f"volume={cell['volume']:.2f}", "aresample=48000", f"apad=whole_dur={D:.3f}"]
            chains.append(f"[{n}:a:0]{','.join(a)}[a{n}]")
            audio.append(f"[a{n}]")
    graph = chains + lays + [f"[{prev}]trim=duration={D:.3f},format=yuv420p[vout]"]
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
            "-c:v", "libx264", *(["-preset", "ultrafast", "-crf", "23"] if fast else ["-preset", "faster", "-crf", "18"]),
            "-r", str(FPS), *extra, str(out)]


def render(c, out, height=None, fast=False, extra=(), overlays=()):
    if not any(c["cells"]):
        raise ValueError("Add at least one video to the collage")
    subprocess.run(command(c, out, height, fast, extra, overlays), check=True, capture_output=True)
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
        assert window(new_cell(a), 3.0) == 3.0                     # default: exactly the duration
        assert window({**new_cell(a), "len": 1.0}, 3.0) == 1.0     # a drag shorter than needed sticks
        assert window({**new_cell(a), "len": 9.0}, 3.0) == 3.0     # longer than needed is capped
        c["cells"][0].update(volume=0.5, opts={**effects.DEFAULT, "speed": 2.0, "look": "mono"})
        c["cells"][1]["opts"]["direction"] = "bounce"  # a 1 s GIF in a 3 s collage: loops, then bounces
        out = render(c, Path(d, "c.mp4"), height=360, fast=True)
        j = json.loads(subprocess.run([*probe, str(out)], capture_output=True, text=True).stdout)
        assert [(s["width"], s["height"]) for s in j["streams"] if s["codec_type"] == "video"] == [(360, 360)], j
        assert abs(float(j["format"]["duration"]) - 3.0) < 0.1, j["format"]
        assert any(s["codec_type"] == "audio" for s in j["streams"])
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
    print("collage ok")
