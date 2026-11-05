<p align="center">
  <img src="flatpak/io.github.linuxlivepaper.svg" alt="LinuxLivePaper logo" width="128" height="128">
</p>

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

### Flatpak
```bash
./build-flatpak.sh          # builds (GNOME 50 runtime + its own ffmpeg/x264), installs for this user,
flatpak run io.github.linuxlivepaper   # and writes dist/LinuxLivePaper.flatpak
```
The first build compiles x264 + ffmpeg (a few minutes); later builds reuse `.flatpak/`. Manifest: `flatpak/`.

## Using it
- The gallery shows your **LinuxLivePaper folder** (default `~/Videos/LinuxLivePaper`, recursive) as an
  iOS-Photos-style grid: square tiles, edge to edge, oldest at the top and newest at the bottom.
  Change the folder in ☰ → *Preferences…* (library folder, GPU acceleration, preview size). Tabs at the top: **All · Live · Collections**.
- Use the **− / +** buttons in the header to make tiles (or, on the Collections tab, collection icons) smaller or bigger. The two
  sizes are independent and remembered between runs; long lists scroll vertically.
  Standalone photos are not shown. Use the import button to copy videos/GIFs into the folder.
- **Right-click** any tile → *Add to Collection* (existing or new), *Remove from Collection* when it already sits in one
  (it goes back to the library folder), or *Delete* (to the Trash). An item lives in exactly one place: adding moves it.
- **Collections** are folders in `<library>/collection/<Name>/`. Each card shows the system folder icon, or a `.png`
  you pick. Create one from **+** → *Collection…*; right-click a card (or the ✎ button inside it) to rename,
  change the icon or delete it — *Rename Collection…* in the right-click menu is a one-field prompt. Click a card to see its items.
- Live images look like plain stills (no badge). Hover over a live, video or GIF tile to play it (muted). Click a tile to open it.
- **Selecting several**: the checkmark button in the header turns clicks into selection; otherwise hold Ctrl (Shift/Alt in
  *Preferences → Multi-select*) and click or drag. Dragging paints the videos the pointer passes over, or selects
  everything inside a dragged box (*Drag style*); each style can get its own key there too. Right-click the selection for
  *Live Collage with N selected* / *Add N selected to Collection* / *Remove N selected from Collection* / *Delete*.
- *Preferences → Read subfolders* includes videos in subfolders of the library folder (the `collection/` folder is always read).
- In the viewer, **press and hold** a live image to play it with sound (Space also plays it). Use ←/→ to move
  between items and Esc to go back. The dropdown in the header picks the press effect
  (Immersive blur, Zoom, Fade, Instant) for that live image.
- **+** → *Live Image from Video or GIF…* (or *Make Live Image* in the viewer) opens the editor on a video or GIF. Drag the yellow handles to trim,
  drag inside the selection to move it, and drag **COVER** to pick the still. Click to place the red playhead.
  Scroll to zoom, Shift+scroll to pan, and drag the overview strip to jump around long videos. Space plays the
  selection with sound, ←/→ steps one frame (Shift for 1 s). Saves go to the library folder.
  Videos can be MP4, MOV, M4V, WebM, MKV, AVI and DVD/MPEG files (VOB, MPG, TS, M2TS); anamorphic DVD video is shown
  and saved at its real (square-pixel) shape.
- **Double-click anywhere on a timeline** to bring the yellow selection (or a collage cell's window) to that spot —
  much faster than dragging it across. The preview follows the timeline live, including while it plays.
- The editor's side panel sets the **Filter**, **Effect**, **Speed**, **Direction** (forward/reverse/bounce) and
  **Repeat** (how many times the clip plays back to back in the saved live image, 1–10) and
  **Press effect**. Hold *Try it* to preview exactly what will be saved. *Save to collection* also files it in a collection.
- **Text & stickers**: in the *Overlays* group, *Text* adds a caption and *Sticker* adds any image or
  GIF (alpha is kept, and the GIF animates in the preview and in the saved file). Drag an overlay on the preview
  to move it; scroll over the preview to resize the selected one. Pick the overlay in *Selected* to change its text,
  font, bold, size, opacity, rotation, outline width and the stretch of the clip it shows on (*Whole clip* off
  gives *Starts at* / *Ends at*).
- **+** → *Live Collage…*: pick a layout (2–9 cells), shape (9:16, 3:4, 1:1, 16:9), borders. Click a cell to
  fill it with a video, GIF or live video; for the selected cell drag the yellow window on its timeline to choose the
  part that plays (the whole clip by default, up to 30 s; drag its ends to stretch or shrink it, *Fit* resets it), and
  set its **volume**, speed, direction, filter and effect. **The longest cell sets the collage length; shorter cells
  loop to fill it.** **Preview: …** (header) trades preview sharpness for speed; saving is always full quality. **Preferences → GPU acceleration** switches NVENC/CPU encoding. **Match clip lengths** (Longest / Shortest) resizes every clip's window to the longest or shortest one. *Repeat* plays the whole collage 1–10 times in the saved file. Space plays the collage,
  *Try it* previews the saved result (with repeats), and overlays work the same as in the editor (over the whole canvas;
  clicking an empty spot selects the cell under it). Changes re-render while playing (cell passes are cached, so only
  the changed cell is re-encoded).

A *live image* is a still and a video that share a name in the same folder (`x.jpg` + `x.mov`).
That means Live Photos copied over from an iPhone show up as LIVE too.

## Docs
- `AGENT-HANDOFF.md`: current state and next steps
- `docs/ARCHITECTURE.md`: stack, live-image format, ffmpeg effect mapping, data model
- `docs/ROADMAP.md`: phased checklist
- `docs/INTOLIVE-ANALYSIS.md`: what the original app does (RE notes); `docs/reference/` has the raw dumps
