# LinuxLivePaper

A Linux entertainment app for **live images**: stills that come alive when you press them, the way iPhone
Live Photos do. Browse your live images, videos and GIFs in a good-looking gallery grid, and create new live images
from videos or GIFs on a trimming timeline (effects, text and stickers are coming).
It's inspired by the iOS app **intoLive**. Linux comes first; iPhone compatibility and Windows are planned for later.

> Status: early development. The gallery and the quick creator work. See `docs/ROADMAP.md` and `AGENT-HANDOFF.md`.

## Run
```bash
cd ~/Documents/LinuxLivePaper
/usr/bin/python3 -m linuxlivepaper
```
Requirements: `/usr/bin/python3` with PyGObject (GTK 4 + libadwaita ≥ 1.5), GStreamer (good/bad/libav) and `ffmpeg`/`ffprobe`.

## Using it
- The gallery shows your **LinuxLivePaper folder** (default `~/Videos/LinuxLivePaper`, recursive, newest first).
  Change it in ☰ → *Library Folder…*. Use the filters at the top: **All · Live · Videos · GIFs**.
  Standalone photos are not shown. Use the import button to copy videos/GIFs into the folder.
- Hover over a LIVE, video or GIF tile to play it (muted). Click a tile to open it.
- In the viewer, **press and hold** a live image to play it with sound (Space also plays it). Use ←/→ to move
  between items and Esc to go back.
- Press **+** (or *Make Live Image* in the viewer) to open the editor on a video or GIF. Drag the yellow handles to trim,
  drag inside the selection to move it, and drag **COVER** to pick the still. Click to place the red playhead.
  Scroll to zoom, Shift+scroll to pan, and drag the overview strip to jump around long videos. Space plays the
  selection with sound, ←/→ steps one frame (Shift for 1 s). Saves go to the library folder.

A *live image* is a still and a video that share a name in the same folder (`x.jpg` + `x.mov`).
That means Live Photos copied over from an iPhone show up as LIVE too.

## Docs
- `AGENT-HANDOFF.md`: current state and next steps
- `docs/ARCHITECTURE.md`: stack, live-image format, ffmpeg effect mapping, data model
- `docs/ROADMAP.md`: phased checklist
- `docs/INTOLIVE-ANALYSIS.md`: what the original app does (RE notes); `docs/reference/` has the raw dumps
