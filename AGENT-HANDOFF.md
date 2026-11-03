# AGENT HANDOFF — LinuxLivePaper

Read this first. Update it before you stop (state, files touched, next steps, log line).

## Goal
**LinuxLivePaper** (`/home/montel/Documents/LinuxLivePaper`) is a **Linux-exclusive entertainment app** for
*live images* (stills that play when pressed, iPhone-Live-Photo style), inspired by the iOS app *intoLive*.
1. **Gallery** of the LinuxLivePaper library folder (live images, videos, GIFs; **no standalone photos**), styled like
   **iOS Photos**: perfect-square tiles touching each other; tabs **All · Live · Collections** (no Videos/GIFs tabs).
2. **Creator** with intoLive-style video effects/editing that outputs live images.

The user's decisions:
- iOS/iPhone compatibility doesn't matter for now ("we can worry about the format working outside of the Linux version later").
- The library is ONE user-settable folder, default `~/Videos/LinuxLivePaper`; creations are saved there.
- The creator must be timeline-based (no typing seconds).
- Collections live in `<library>/collection/<Collection Name>/` (folder per collection, named by the collection).
  Icon: the system folder icon unless the user picks a `.png`. Deleting from All/Live must NOT delete the collection copy.
- Right-click on anything → Delete / Add to Collection. Creators have a "Save to collection" option.
- Performance matters ("no throttle, pure optimization"); the user often has YouTube playing while testing.
- Windows comes later. It's not a desktop-wallpaper engine (that would be a separate Rust project).

## Read in this order
1. `README.md`: how to run, what works
2. `docs/ARCHITECTURE.md`: stack, live-image format (still+video pair), effect→ffmpeg table, data model
3. `docs/ROADMAP.md`: phased checklist (tick boxes as you go)
4. `docs/INTOLIVE-ANALYSIS.md`: intoLive feature map, to use as the parity checklist for the creator

## Current state
- ✅ Gallery (`linuxlivepaper/app.py`, `library.py`, `thumbs.py`): iOS-Photos-style grid (`MediaGrid`: `Square` tiles
  1 px apart, oldest first like iOS), tabs All/Live/Collections, badges, hover-to-play, right-click menu (Add to
  Collection ▸ / New Collection…, Delete — or "Remove from Collection" inside a collection), viewer (press-and-hold live with sound, video with controls, GIF, ←/→/Esc/Space),
  import button (copies into the library), ☰ menu: Library Folder… (Adw.PreferencesDialog), Open Library Folder, Rescan.
  Library path persisted in `~/.config/linuxlivepaper/config.json` (`{"library": "..."}`).
- ✅ Timeline editor (`editor.py`: `Timeline` DrawingArea + `EditorPage`), opened by **+** or the viewer's *Make Live Image*:
  overview strip, zoomable filmstrip, trim handles, movable selection, COVER marker, playhead, presets 1.5/3/5 s.
  Output via `livemake.make_live` → `<library>/LLP_*.jpg/.mov`. Video and GIF sources both work.
- Demo items `~/Videos/LinuxLivePaper/LLP_test.*` and `LLP_20261102_224701.*` are made from screencasts. Safe to delete.
- ✅ Press-and-hold transition (`playback.py: LiveCanvas`): the still pushes in and smears into a radial motion blur +
  gaussian blur, the video cross-fades in through it and sharpens (0.55 s); release reverses it (0.40 s). Knobs at the
  top of `playback.py` (`T_IN`, `T_OUT`, `BLUR_PX`, `ZOOM_BLUR`, `HOLD_ZOOM`). Space works as hold too.
  Styles (`playback.STYLES`): Immersive blur (default), Zoom, Fade, Instant. Stored per item in the folder sidecar
  `.linuxlivepaper.json` (`library.get_meta/set_meta`, keyed by stem). Changeable in the viewer header dropdown.
- ✅ Baked-in effects (`effects.py`): Filter (10 looks), Effect (glitch, VHS, dreamy, pulse, shake, rainbow, pixel,
  mirror), Speed 0.5–2×, Direction forward/reverse/bounce. One ffmpeg filter_complex (`effects.graph`) renders both the
  editor preview (`effects.preview`, 540p ultrafast, cached in `~/.cache/linuxlivepaper/previews`) and the saved MOV,
  so the preview is WYSIWYG. Paused frames use `effects.frame_chain` via `thumbs.grab_frame(..., chain)`.
  Editor side panel (Adw.PreferencesPage): Look, Motion, Press effect + "Try it" press-and-hold, Reset effects.
  Cover time is mapped through speed/direction (`effects.out_time`); playhead back via `effects.src_time`.
- ✅ Collections (`library.Collection`, `app.CollectionsView/CollectionPage`, `widgets.collection_dialog`):
  cards with the themed `folder` icon or the chosen PNG (stored as `.icon.png` inside the collection folder), name,
  item count; click → the collection's grid; right-click → Edit (rename + icon) / Delete (to Trash).
  `add_to_collection` **hard-links** the files (falls back to copy), so deleting from All/Live (trash) leaves the
  collection's copy intact and vice versa. Per-item meta (press style) is copied into the collection's sidecar.
  `library.scan()` skips the top-level `collection/` folder, so collection copies don't show twice in All.
- ✅ Collage maker (`collage.py` model/render + `collage_page.py` UI, **+ → Live Collage…**): 10 layouts (2/3/4/6/9
  cells, mixed), shapes 9:16/3:4/1:1/16:9, length 1.5–5 s, borders none/thin/thick, black/white. Per cell: clip
  (video, GIF or live image `.mov`), start (fixed-length window on the cell timeline, `Timeline(fixed=True)`),
  volume + mute, speed, direction, filter, effect. Cover time + press effect + Save to collection.
  intoLive itself has no layout collage (it has PIP video stickers and "2-Split/3-Split"); this is our version.
- **Collage performance design**: the preview never runs the collage graph. Each cell gets its own cached 360p
  proxy (`effects.preview(..., loop=True)`, parallel pool, ~0.5 s each, only the changed cell re-renders) and
  `CollageCanvas` composites the playing `Gtk.MediaFile`s with GtkSnapshot. A cell switches from its still to video on
  its first decoded frame (no blank flash). Full 1080×1920 ffmpeg render only on save (~2.5–4.5 s for 4 cells under
  load); cells are scaled down to cell size *before* filters, x264 `-preset faster` (measured: NVENC was no faster —
  decode/filters dominate).
- Self-checks: `/usr/bin/python3 -m linuxlivepaper.{library,thumbs,livemake,playback,effects,collage}`.

### Hard-won facts (don't relearn these)
- GTK's GStreamer `Gtk.MediaFile.seek()` while **paused** reports success but doesn't render the frame (black).
  So the editor shows exact **ffmpeg frame grabs** (`thumbs.grab_frame`, ~0.25 s on a 1080p episode) while paused,
  and plays a cached **ffmpeg proxy of exactly the selection** (`thumbs.mp4_proxy(src, s, e, 540)`) for looping playback.
- **GStreamer can't play GIFs** in `Gtk.MediaFile` (no video, no error), and **GdkPixbuf fails on many real GIFs**
  ("Not all frames of the GIF image were loaded"). GIFs go through ffmpeg: thumbnails via `grab_frame`, playback
  via an MP4 proxy in `~/.cache/linuxlivepaper/proxies/`.
- **PipeWire mute bug**: PipeWire remembers the last mute state per app. After a muted hover preview, a new
  `Gtk.MediaFile` comes up muted at the sink while GTK reports `muted=False` (so it never re-sends it). Always create
  streams with `playback.media_file()`, which re-asserts the mute state on `notify::prepared`. Check with
  `pactl -f json list sink-inputs` filtered by `application.process.id`.
- Audio: playback with sound works (verified via `pactl list sink-inputs`). The user's 23 screencasts in ~/Videos/Screencasts
  have **no audio track**, so anything made from them is silent. Grid hover is muted on purpose; the viewer and editor play sound.
- Adding a look/effect = one dict entry in `effects.py` (an ffmpeg chain that keeps the frame size; internal pad
  labels must be unique, prefix them `fx`). The self-check renders every entry and checks size + duration.
  Gotcha: `blend` on yuv420p tints pink/green → `dreamy` converts to `gbrp` first.
- PyGObject: `Gdk.RGBA(red=…)` kwargs are silently ignored (transparent!). Use `rgba.parse("#…")`.
- `Gio.File.trash()` on test files lands in the user's real Trash (`~/.local/share/Trash`); clean up after tests.
- Test video with audio: `/media/montel/Extreme SSD/Yu-Gi-Oh Arc-V Episode 18 English SubbedDubbed Aniwatch.mp4` (21:55, 1080p).
  Test GIFs: `/home/montel/Documents/GifDeck/GIFs/`.

## How to test the UI headlessly (without capturing the user's real screen!)
```bash
cd ~/Documents/LinuxLivePaper
cat > /tmp/shot.py <<'EOF'
import gi, sys; gi.require_version('Gdk','3.0')
from gi.repository import Gdk
w=Gdk.get_default_root_window(); Gdk.pixbuf_get_from_window(w,0,0,w.get_width(),w.get_height()).savev(sys.argv[1],'png',[],[])
EOF
# point the app at a throwaway library so tests don't touch the user's folder
mkdir -p /tmp/llpcfg/linuxlivepaper /tmp/llplib && echo '{"library": "/tmp/llplib"}' > /tmp/llpcfg/linuxlivepaper/config.json
XDG_CONFIG_HOME=/tmp/llpcfg xvfb-run -a -s "-screen 0 1400x900x24" bash -c '/usr/bin/python3 -m linuxlivepaper & P=$!; sleep 10;
  xdotool mousemove 130 200; sleep 2; /usr/bin/python3 /tmp/shot.py /tmp/shot.png; kill $P'
```
The app is a unique GApplication: if the user has it open, a test launch just raises THEIR window. Test scripts
must add `Gio.ApplicationFlags.NON_UNIQUE`. To open the editor directly, run a small script that calls `App().run()` and, from a `GLib.timeout_add`, calls
`app.props.active_window.open_editor(path)`.
Ignore the DRI/EGL warnings under Xvfb. Never screenshot the user's real `:0` display; it shows their private browsing.

## Environment gotchas
- Use **`/usr/bin/python3`** (3.12, GTK 4.14, libadwaita 1.5). The miniconda `python3` on PATH has no `gi`.
- GNOME 46 on X11. Installed: ffmpeg 6.1.1, GStreamer 1.24 (libav, avdec_gif, h265 decode), exiftool, Xvfb, xdotool.
  NVENC is available (`h264_nvenc`, `hevc_nvenc`).
- No new dependencies unless unavoidable (stdlib + PyGObject + ffmpeg CLI).
- Ghidra MCP (pyghidra-mcp) is its own empty project, not the user's CodeBrowser. Not needed now. Only revisit it for
  iPhone-compat details (`LiveVideoWriter`, `LiveKeyPhotoWriter`).

## Rules
- Don't copy intoLive assets (videos, fonts, LUTs) into the repo.
- Every non-trivial module gets one runnable self-check (`if __name__ == "__main__":` asserts).

## Next steps (suggested order)
1. Text overlays (drawtext) and stickers (image/GIF overlay, PIP) in the editor and collage cells; crop/rotate.
2. Filter intensity slider + .cube LUT import (`lut3d`).
3. Gallery polish: date sections, scroll to newest on open, Gio.FileMonitor live reload, multi-select, fullscreen.
4. Collage: drag to swap cells, per-cell pan/zoom inside its frame, shared clock if cell drift ever shows.
5. Package it: a `.desktop` file + icon.

## Log
- 2026-11-02: Phase 0. Static IPA analysis.
- 2026-11-02: Scope v2: iPhone Live Photo creator (superseded).
- 2026-11-02: Scope v3 (current): a Linux-exclusive gallery + creator; iOS compat deferred. Built the gallery MVP + quick creator; docs rewritten.
- 2026-11-02: Fixed GIF import/viewing (ffmpeg thumbs + MP4 proxies). Removed photos. Single library folder (default ~/Videos/LinuxLivePaper, settable).
  Replaced the seconds-typing CreateDialog with the timeline editor (`editor.py`). Audio checked: it works; the screencasts are silent.
- 2026-11-02: Fixed live audio being muted after hover previews (PipeWire mute restore). Added the immersive press transition (`playback.py`).
- 2026-11-03: Collage maker (per-cell volume/speed/direction/filter/effect), Collections (folder per collection, custom PNG icon), iOS-Photos grid, tabs All/Live/Collections, right-click Delete/Add to Collection, Save to collection in creators.
- 2026-11-02: Baked-in effects (`effects.py`: looks, effects, speed, reverse/bounce) in an editor side panel; press transition selectable per item (blur/zoom/fade/instant).
