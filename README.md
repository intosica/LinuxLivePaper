# LinuxLivePaper

A Linux entertainment app for **live images**: stills that come alive when you press them, the way iPhone
Live Photos do. Browse your live images, videos and GIFs in a good-looking gallery grid, and create new live images
from videos or GIFs on a trimming timeline, with filters, effects, speed and boomerang (text and stickers are coming).
It's inspired by the iOS app **intoLive**. Linux comes first; iPhone compatibility and Windows are planned for later.

> Status: early development. The gallery and the quick creator work. See `docs/ROADMAP.md` and `AGENT-HANDOFF.md`.

## Run
```bash
cd ~/Documents/LinuxLivePaper
/usr/bin/python3 -m linuxlivepaper
```
Requirements: `/usr/bin/python3` with PyGObject (GTK 4 + libadwaita ≥ 1.5), GStreamer (good/bad/libav) and `ffmpeg`/`ffprobe`.

## Using it
- The gallery shows your **LinuxLivePaper folder** (default `~/Videos/LinuxLivePaper`, recursive) as an
  iOS-Photos-style grid: square tiles, edge to edge, oldest at the top and newest at the bottom.
  Change the folder in ☰ → *Library Folder…*. Tabs at the top: **All · Live · Collections**.
  Standalone photos are not shown. Use the import button to copy videos/GIFs into the folder.
- **Right-click** any tile → *Add to Collection* (existing or new) or *Delete* (to the Trash). Deleting from All/Live
  never removes the collection copy; inside a collection, *Remove from Collection* removes only that copy.
- **Collections** are folders in `<library>/collection/<Name>/`. Each card shows the system folder icon, or a `.png`
  you pick. Create one from **+** → *Collection…*; right-click a card (or the ✎ button inside it) to rename,
  change the icon or delete it. Click a card to see its items.
- Hover over a LIVE, video or GIF tile to play it (muted). Click a tile to open it.
- In the viewer, **press and hold** a live image to play it with sound (Space also plays it). Use ←/→ to move
  between items and Esc to go back. The dropdown in the header picks the press effect
  (Immersive blur, Zoom, Fade, Instant) for that live image.
- **+** → *Live Image from Video or GIF…* (or *Make Live Image* in the viewer) opens the editor on a video or GIF. Drag the yellow handles to trim,
  drag inside the selection to move it, and drag **COVER** to pick the still. Click to place the red playhead.
  Scroll to zoom, Shift+scroll to pan, and drag the overview strip to jump around long videos. Space plays the
  selection with sound, ←/→ steps one frame (Shift for 1 s). Saves go to the library folder.
- **Double-click anywhere on a timeline** to bring the yellow selection (or a collage cell's window) to that spot —
  much faster than dragging it across. The preview follows the timeline live, including while it plays.
- The editor's side panel sets the **Filter**, **Effect**, **Speed**, **Direction** (forward/reverse/bounce) and
  **Press effect**. Hold *Try it* to preview exactly what will be saved. *Save to collection* also files it in a collection.
- **Text & stickers**: in the *Overlays* group, *Text* adds a caption and *Sticker* adds any image or
  GIF (alpha is kept, and the GIF animates in the preview and in the saved file). Drag an overlay on the preview
  to move it; scroll over the preview to resize the selected one. Pick the overlay in *Selected* to change its text,
  font, bold, size, opacity, rotation, outline width and the stretch of the clip it shows on (*Whole clip* off
  gives *Starts at* / *Ends at*).
- **+** → *Live Collage…*: pick a layout (2–9 cells), shape (9:16, 3:4, 1:1, 16:9), length, borders. Click a cell to
  fill it with a video, GIF or live video; for the selected cell drag the yellow window on its timeline to choose the
  part that plays (drag its ends to stretch or shrink it — a window shorter than the clip needs freezes on the last
  frame; *Fit* resets it), and set its **volume**, speed, direction, filter and effect. Space plays the whole collage,
  *Try it* previews the saved result, and overlays work the same as in the editor (over the whole canvas; clicking an
  empty spot selects the cell under it). Changes show up live, while playing too.

A *live image* is a still and a video that share a name in the same folder (`x.jpg` + `x.mov`).
That means Live Photos copied over from an iPhone show up as LIVE too.

## Docs
- `AGENT-HANDOFF.md`: current state and next steps
- `docs/ARCHITECTURE.md`: stack, live-image format, ffmpeg effect mapping, data model
- `docs/ROADMAP.md`: phased checklist
- `docs/INTOLIVE-ANALYSIS.md`: what the original app does (RE notes); `docs/reference/` has the raw dumps
