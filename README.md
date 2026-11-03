# LinuxLivePaper

A Linux entertainment app for **live images**: stills that come alive when you press them, the way iPhone
Live Photos do. Browse your own photos, videos, GIFs and live images in a good-looking gallery grid, and
create new live images from your videos (effects, text and stickers are coming).
It's inspired by the iOS app **intoLive**. Linux comes first; iPhone compatibility and Windows are planned for later.

> Status: early development. The gallery and the quick creator work. See `docs/ROADMAP.md` and `AGENT-HANDOFF.md`.

## Run
```bash
cd ~/Documents/LinuxLivePaper
/usr/bin/python3 -m linuxlivepaper
```
Requirements: `/usr/bin/python3` with PyGObject (GTK 4 + libadwaita ≥ 1.5), GStreamer (good/bad/libav) and `ffmpeg`/`ffprobe`.

## Using it
- The gallery shows `~/Pictures`, `~/Videos` and `~/Pictures/LinuxLivePaper` (recursive, newest first).
  Use the filters at the top: **All · Live · Videos · Photos · GIFs**.
- Hover over a LIVE, video or GIF tile to play it (muted). Click a tile to open it.
- In the viewer, **press and hold** a live image to play it with sound (Space also plays it). Use ←/→ to move
  between items and Esc to go back.
- Press **+** to pick a video, choose the start, length and cover frame, and save it as a live image.

A *live image* is a still and a video that share a name in the same folder (`x.jpg` + `x.mov`).
That means Live Photos copied over from an iPhone show up as LIVE too.

## Docs
- `AGENT-HANDOFF.md`: current state and next steps
- `docs/ARCHITECTURE.md`: stack, live-image format, ffmpeg effect mapping, data model
- `docs/ROADMAP.md`: phased checklist
- `docs/INTOLIVE-ANALYSIS.md`: what the original app does (RE notes); `docs/reference/` has the raw dumps
