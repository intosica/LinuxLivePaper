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

## intoLive reverse-engineering files (where the "decompiled" stuff lives)
There is **no decompiled C/pseudocode anywhere** — every finding so far is static (strings + Swift reflection metadata).
- IPA: `~/Documents/intoLive-Live-Wallpapers-IPAOMTK.com.ipa`. Extract: `unzip -oq <ipa> 'Payload/intolive.app/*' -d /tmp/intolive`
  (main arm64 Swift binary: `/tmp/intolive/Payload/intolive.app/intolive`; `/tmp` is wiped on reboot).
- `~/Documents/intoLive-Live-Wallpapers-IPAOMTK.com_malimite/`: only the same raw `intolive` Mach-O + an **empty**
  Malimite database (0 rows) + a screenshot. Nothing decompiled there.
- **Swift type layouts**: `docs/reference/intolive-re/swift_fields.txt` (2873 types, `Type | field: FieldType`, from
  `swift_fields.py`, which parses `__swift5_fieldmd` in a second). This is the best map of intoLive's data model —
  grep it before reaching for Ghidra. Summary in `docs/INTOLIVE-ANALYSIS.md` → "Swift type layouts".
- Older static dumps: `docs/reference/intolive-*.txt` (class names, English UI strings, nibs, bundled assets).
- Ghidra: the user's project `/media/montel/Extreme SSD/Games/Resident Evil 6/ghidra project/IntoLive.gpr` has all
  IPA binaries imported, but auto-analysis of `intolive` takes very long and was still incomplete on 2026-11-03.
  pyghidra-mcp (port 8000) can open it but symbol search fails with "Analysis incomplete". Only worth it for real
  algorithm details (e.g. `LiveVideoWriter` for iPhone compatibility). The RE6/BH6 chromadb on that drive is unrelated.
- **Headless decompile** (no GUI): `docs/reference/intolive-re/ghidra/decompile.sh [binary] [outdir] [regex]` →
  `~/Documents/intoLive-decomp/src/*.c` (one file per class/namespace, `_global/` for `FUN_*`, `index.tsv`).
  `FastAnalysis.java` turns off the slow analyzers; `ExportDecomp.java` decompiles in parallel. Re-runs reuse the
  analyzed project. Binary is already decrypted (cryptid 0) but stripped (6k symbols): ObjC methods get names, Swift
  funcs are mostly `FUN_*` — map them with `swift_fields.txt` + strings. Tested on SDKConfigurations (30 s); main
  binary (35 MB `__text`) needs hours and ~12 GB heap — stop pyghidra-mcp first.

## Current state
- ✅ Gallery (`linuxlivepaper/app.py`, `library.py`, `thumbs.py`): iOS-Photos-style grid (`MediaGrid`: `Square` tiles
  1 px apart, oldest first like iOS), tabs All/Live/Collections, badges, hover-to-play, right-click menu (Add to
  Collection ▸ / New Collection…, Delete — or "Remove from Collection" inside a collection), viewer (press-and-hold live with sound, no on-screen hint, video with controls, GIF, ←/→/Esc/Space),
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
  cards with the themed `folder` icon or the chosen PNG/JPG/WebP/GIF, animated ones play while on screen (stored as `.icon<ext>` inside the collection folder; `library.ICON_EXT`, `widgets.icon_widget` uses GdkPixbuf animations, ffmpeg first frame if GdkPixbuf fails), name,
  item count; click → the collection's grid; right-click → Edit (rename + icon) / Delete (to Trash).
- ✅ Zoom (2026-11-03): header [−][+] buttons. Tabs All/Live (and the collection page) zoom the tile grid (`app.TILE_SIZES`, `Square.size`/`Square.zoom`,
  config key `grid_zoom`, default 170); the Collections tab zooms its cards independently (`ICON_SIZES`, `CollectionsView.zoom`, key `collections_zoom`, default 72).
  Both saved immediately via `library.set_pref`. Both views are in ScrolledWindows (vertical scroll when content overflows; headless-tested with 300 mock items / 80 mock collections).
  `add_to_collection` **hard-links** the files (falls back to copy), so deleting from All/Live (trash) leaves the
  collection's copy intact and vice versa. Per-item meta (press style) is copied into the collection's sidecar.
  `library.scan()` skips the top-level `collection/` folder, so collection copies don't show twice in All.
- ✅ Collage maker (`collage.py` model/render + `collage_page.py` UI, **+ → Live Collage…**): 10 layouts (2/3/4/6/9
  cells, mixed), shapes 9:16/3:4/1:1/16:9, borders none/thin/thick, black/white. **Length = the longest cell's pass**
  (`collage.duration`); shorter cells **loop** to fill it (user's spec: "the longest clip is the collage length, the
  other one repeats enough just to fit"). **Repeat** 1–10 (`c["repeat"]`) plays the whole pass n times in the saved
  file (stream copy via `thumbs.repeat`). Per cell: clip (video, GIF or live image `.mov`), a source window on the
  cell timeline (`Timeline(fixed=True)`; default = rest of the clip after start, capped at `collage.MAX_WINDOW` 30 s;
  dragging sets `cell["len"]`; *Fit* resets; double-click jumps it), volume + mute, speed,
  direction, filter, effect. Canvas-level overlays, press effect + *Try it*, Save to collection, and clicking an
  empty canvas spot selects the cell under it (`ov_click` → `collage.cell_at`).
  intoLive itself has no layout collage (it has PIP video stickers and "2-Split/3-Split"); this is our version.
- **Collage render (2026-11-03 rewrite)**: two stages in `collage.render(c, out, height, fast, extra, overlays,
  cache, repeat)`: (1) each cell's single pass at its final cell size (`_cell_command`: SQUARE → cover-crop → look/
  effect → speed → reverse/bounce → fps 30 → trim to `cell_length`, PCM audio padded to the same length) in parallel,
  cached by a hash of the cell in `cache` (UI: `~/.cache/linuxlivepaper/collage/cells`); (2) one compose pass
  `-stream_loop -1 -i cellN.mkv` → overlay on the background, amix, overlays, `-t duration`. Then `thumbs.repeat` if
  repeat > 1. Time mapping for stills/playhead: `collage.cell_time(cell, t)`. Paused stills are ffmpeg grabs per cell;
  Play/Try it play the 540p collage render (`CollagePage._try_path`, Play = 1 pass looping, Try it = all repeats).
  The page keeps `len_row`/`win_row`/`cover_scale` in sync via `_length_changed()` (called from `refresh`).
- ✅ Overlays (`overlays.py` + `overlay_ui.py`): text and image/GIF stickers baked into the live image, in the
  editor *and* the collage maker. `overlays.text(...)`/`overlays.sticker(path)` are plain dicts (x/y/size as canvas
  fractions, opacity, rotation in degrees, `start`/`end` window, text `font`/`bold`/`outline`); `overlays.graph()`
  appends `drawtext` / `overlay` to the effects chain, `render()` and `preview()` mirror `effects.render/preview`
  with `overlays=`, and `livemake.make_live(..., overlay_list=…)` bakes them in (cover still is grabbed from the MOV,
  so it carries them). `collage.command(..., overlays=…)` does the same for collages. **GIF alpha works**:
  `format=rgba` before scaling is what keeps transparency (a pal8 GIF scaled first loses it); `-stream_loop -1`
  keeps GIFs animating on long clips. Rotation: text is drawn centred on a transparent full-canvas layer that is
  then `rotate`d (drawtext has no rotation) and overlaid at `W*(x-.5), H*(y-.5)`; stickers are rotated around their
  own centre with `rotate=...:ow=rotw(iw):oh=roth(ih)`.
  Shared UI in `overlay_ui.py`: `OverlayHost` mixin (state, `text_layout`, `sticker_texture`, add/remove/select,
  hooks `ov_rect`/`ov_duration`/`ov_changed`/`ov_click`) + `OverlayGroup` panel rows (Text, Sticker, Selected, Size,
  Opacity, Rotation, Outline, Text, Font, Bold, Whole clip → Starts at / Ends at, Remove) + `OverlayLayer`
  (draws rotated overlays and the selection box, drag to move, scroll to resize, press on empty space calls
  `ov_click`). The layer hides while playing or during *Try it* so the baked preview is what you see.
  `Gtk.Snapshot.rotate()` takes **degrees**, and `Gtk.Snapshot.append_layout()` needs a scratch cairo context via
  `create_pango_layout()` — both were silent-wrong-render bugs for a while.
  Known gaps: no keyframes or per-overlay animations, one weight per text overlay, collages share one overlay list
  (not per cell).
- **Live preview**: while the editor or a collage is playing, edits re-render instead of stopping playback —
  `EditorPage._rebake()` (debounced 400 ms, restarts only the baked preview) and `CollagePage._swap(idx)`
  (re-renders the changed cell's proxy, swaps its `Gtk.MediaFile` and seeks it to the old position so the cells
  stay in step). Timeline drags follow live too. `CollagePage._on_tick` drives the timeline playhead from the
  selected cell's stream (`timestamp % window` through `effects.src_time`); without it the red line sat still.
- **Timeline**: drag either yellow end to stretch/shrink the selection (a collage cell window is exactly what that
  cell plays and loops, *Fit* resets it to the whole clip), drag inside to move it, and
  **double-click anywhere** to jump the window to that spot. `Timeline._hit()` must not downgrade start/end to
  "move" when `fixed=True` — that was why collage windows could only be moved.
- **Try it** (`HoldButton` in `widgets.py`) previews exactly what will be saved, in the editor and in collages
  (cached 540p collage render in `~/.cache/linuxlivepaper/collage`). Two real causes of it "only working sometimes":
  `thumbs.grab_frame` returned None for a seek at/after the last frame (now falls back `t-0.2`, `t-0.6`, 0), and
  `Gtk.GestureClick` emits `stopped` the instant the press is claimed, which ended every hold immediately — so the
  button only ends the hold on `released`.

- **No "LIVE" badge** anywhere (user asked for clean previews, 2026-11-03): tiles and the viewer show the still only;
  GIF and video-duration badges stay.
- **Fullscreen + chrome (2026-11-03)**: `win.fullscreen` action + `F11`, also `F` inside the viewer, double-click on the
  viewer picture, and the header / ☰ / `fs_btn` buttons; Escape leaves fullscreen first, then pops. Both the *gallery*
  and the *viewer* are fullscreen-capable. Chrome hides itself: the viewer shows **no "Press and hold" hint** (removed 2026-11-03: it broke immersion);
  the ←/→ arrows appear **only while the pointer is within `EDGE_PX` (140) of that side edge** (`Viewer._edge_moved`/
  `_set_edge`/`_arrows`; never while a live image/video plays), and the header + mouse cursor fade out after
  `CHROME_IDLE_MS` (5 s) of a still mouse and come back on motion (`Viewer.show_chrome`). The gallery header hides the same way (`Window._chrome`, `_moved`) but stays opaque on purpose:
  `set_extend_content_to_top_edge` made the tab switcher unreadable over the grid.
- **Collage framing (2026-11-03)**: drag a cell to move the video *inside* its frame (or scroll over it to zoom 1–4×,
  Framing row → *Reset*); pan is stored per cell as `pan [x, y]` 0..1 and is the same `collage.cell_fit` crop used by
  the render, so what you drag is what gets saved. Drags are absolute (`_pan0` + total offset, first motion report is
  the origin), they stop the preview first, and they no longer rebuild the 360 px panel mid-drag (`select(quiet=True)` +
  `_panel_stale`) — that rebuild-on-press was the "very rough, not smooth" feel.
- **File browser in the collage maker (2026-11-03)**: `widgets.FileBrowser` in a slide-out pane (handle button on the
  left edge). Folders first, then library clips with thumbnails; click a tile to fill the selected cell, or drag a tile
  onto any cell (the `Gtk.DropTarget` on the stage picks the cell under the pointer, following files fill the empty
  cells). Both panes scroll (`vscrollbar_policy=AUTOMATIC` + `vexpand` — the panel was clipping its last rows).
- **Collection icons**: the *Choose Icon…* picker must be parented to the app **window**, not `alert.get_root()`
  (an `Adw.AlertDialog` is already a toplevel, so the portal put the file chooser behind the modal dialog and the user
  saw "no menu"). `widgets.collection_dialog` now does `icon_dlg.open(parent if isinstance(parent, Gtk.Window) else
  parent.get_root(), …)`.
- **GPU acceleration (2026-11-03)**: playback was already hardware-decoded (GStreamer → `nvh264dec`), but every encode
  (collage play/rebake/save, MP4/GIF proxies) silently fell back to libx264 because the flatpak's ffmpeg was built
  against the SDK's ffnvcodec headers (NVENC API 13.1) while driver 580.173.02 only exposes 13.0 →
  "Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0". `flatpak/io.github.linuxlivepaper.yml`
  now installs `nv-codec-headers` 12.2.72.0 and builds ffmpeg with `--enable-ffnvcodec --enable-nvenc`;
  `thumbs.nvenc()` returns True and an encode uses ~16× less CPU (1.85 s vs 29.96 s CPU for 600 frames of 1080×1920)
  — that CPU burn was the loud fan. `LINUXLIVEPAPER_ENCODER=x264` still forces the CPU path.
- **Editor Repeat** (Motion group SpinRow, `EditorPage.repeat`, not part of `opts` so preview caching is unchanged):
  `livemake.make_live(..., repeat=n)` renders once, then `thumbs.repeat(once, mov, n, live_tags())`. Try it plays the
  repeated preview (`<preview>.xN.mp4`).
- **DVD / MPEG sources**: `library.VIDEO_EXT` includes `.vob .mpg .mpeg .ts .m2ts .mts`. `thumbs.SQUARE`
  (`scale=trunc(iw*sar/2)*2:ih,setsar=1`) is applied in frame grabs, MP4 proxies and collage cells, and
  `thumbs.probe` returns the *display* width, so anamorphic DVD (720×480, SAR 32:27) comes out 852×480. Verified with
  all files in `~/Documents/VOB file examples` (MPEG-2 + MP2, up to 3840×2160): probe, grabs, make_live, collage.
- **Flatpak**: `./build-flatpak.sh` → `flatpak/io.github.linuxlivepaper.yml` (GNOME 50 runtime; x264 + ffmpeg 6.1.2 (NOT 7.x: drawtext escaping differs)
  built **static** with gpl/libx264/freetype/harfbuzz/fontconfig so the runtime's GStreamer libav stays untouched;
  app copied to `/app/share/linuxlivepaper`, launcher `/app/bin/linuxlivepaper`; `--filesystem=home`, `/media`,
  `/run/media`, `/mnt`). Build state in `.flatpak/` (git-ignored), bundle in `dist/LinuxLivePaper.flatpak`.
  Inside the sandbox the config/cache live in `~/.var/app/io.github.linuxlivepaper/`.

### Hard-won facts (don't relearn these)
- `thumbs.grab_frame(..., chain)`: a chain ending in "," (cell_fit + empty look) used to make ffmpeg fail silently,
  which left collage cells as "…" forever. `_frame_at` now strips empty pieces.
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
- Ghidra: see "intoLive reverse-engineering files" above. Not needed for current work.

## Rules
- Don't copy intoLive assets (videos, fonts, LUTs) into the repo.
- Every non-trivial module gets one runnable self-check (`if __name__ == "__main__":` asserts).

## Next steps (suggested order)
1. Per-cell overlays in collages (one overlay list per `cell`, reuse `overlays.graph`); keyframes/sticker animations
   (intoLive has `stickerAnimationComeUp*`, `MarqueeLeftRight/UpDown`, `Rotate*`); more text styles.
2. Crop/rotate/flip and Ken Burns for clips; filter intensity slider + .cube LUT import (`lut3d`).
3. Gallery polish: date sections, scroll to newest on open, Gio.FileMonitor live reload, multi-select.
4. Collage: drag to swap cells, shared clock if cell drift ever shows. (Per-cell pan/zoom is done; the file browser
   pane relayouts a beat late when a drag starts — it only expands the stage on the next allocation.)
5. Flatpak: add AppStream metainfo if it ever goes to Flathub; consider narrowing `--filesystem=home` to portals.

## Log
- 2026-11-02: Phase 0. Static IPA analysis.
- 2026-11-02: Scope v2: iPhone Live Photo creator (superseded).
- 2026-11-02: Scope v3 (current): a Linux-exclusive gallery + creator; iOS compat deferred. Built the gallery MVP + quick creator; docs rewritten.
- 2026-11-02: Fixed GIF import/viewing (ffmpeg thumbs + MP4 proxies). Removed photos. Single library folder (default ~/Videos/LinuxLivePaper, settable).
  Replaced the seconds-typing CreateDialog with the timeline editor (`editor.py`). Audio checked: it works; the screencasts are silent.
- 2026-11-02: Fixed live audio being muted after hover previews (PipeWire mute restore). Added the immersive press transition (`playback.py`).
- 2026-11-03: Collage maker (per-cell volume/speed/direction/filter/effect), Collections (folder per collection, custom PNG icon), iOS-Photos grid, tabs All/Live/Collections, right-click Delete/Add to Collection, Save to collection in creators.
- 2026-11-02: Baked-in effects (`effects.py`: looks, effects, speed, reverse/bounce) in an editor side panel; press transition selectable per item (blur/zoom/fade/instant).
- 2026-11-03: Text overlays and image/GIF stickers (`overlays.py`), placed by dragging on the editor preview; GIF alpha preserved; baked into the saved MOV and cover still.
- 2026-11-03: Overlay editing round: shared `overlay_ui.py` (rotation, font/bold, outline width, per-overlay time window, sticker+text in collages), live preview while playing, collage windows stretchable + *Fit* + Try it, timeline double-click, `HoldButton` rework, near-end cover-frame fix, quit-while-playing crash fix.
- 2026-11-03: Collage length = longest cell, shorter cells loop (two-stage cached render); Repeat ×1–10 for collages and
  single live images; LIVE badges removed; VOB/MPEG + anamorphic support; fixed blank collage stills (trailing ","
  in grab chain); Swift reflection dump of intoLive (`docs/reference/intolive-re/`); flatpak build script.
- 2026-11-03: Viewer/gallery fullscreen + auto-hiding chrome (arrows, hint, header, cursor after 5 s idle), collage
  cell drag-pan/scroll-zoom with a smooth absolute mapping, slide-out file browser with drag-and-drop onto cells,
  Choose-Icon portal parenting fix, NVENC re-enabled in the flatpak (nv-codec-headers 12.2), collage/editor side
  panels scroll properly.
- 2026-11-03: Viewer immersion pass: removed the "Press and hold to play" hint (`LiveView.hint`/`set_hint`, `.hint` CSS) and made the
  ←/→ arrows edge-hover only (`EDGE_PX`). Logo (`flatpak/io.github.linuxlivepaper.svg`) centred at the top of README. Pushed to
  https://github.com/intosica/LinuxLivePaper.git (user's GitHub is authorised on this PC). Screenshots: user will add later.
- 2026-11-03: Collection icons accept PNG/JPG/WebP/GIF (animated or not; tested with every file in ~/Documents/GifDeck). Replacing an icon removes the old `.icon.*`.
- 2026-11-03: Added independent zoom −/+ for the media grid and the Collections view (smaller default collection icons: 72px), both persisted in config.json (`grid_zoom`, `collections_zoom`). Scrolling verified with mock data.
- 2026-11-03: Fixed grid zoom doing nothing in the flatpak: `Square.zoom` used a `WeakSet` of tiles, but PyGObject drops the wrapper of widgets only held by C (set_child), so the set was empty and GTK's cached tile size was never invalidated. Now walks all toplevel widget trees and `queue_resize()`s every `Square`. Lesson: never keep weakref registries of widgets; walk the tree. Verified inside the rebuilt flatpak with the real library (tiles 180→316 px after two +). Test via `flatpak run --filesystem=/tmp/x --command=python3 io.github.linuxlivepaper /tmp/x/test.py` (that run uses the REAL `~/.var/app/...` config, delete it after).
- 2026-11-03: Fixed collection zoom doing nothing for collections with a custom icon: a `Gtk.Picture`'s natural size is the image's pixel size (602×451 for the user's icon) and beat `width_request`. `widgets.icon_widget` now returns a `Gtk.Overlay` (empty fixed-size Box + picture as overlay). Verified 72→128 px on the real library; old code stayed 602×451. Fake-collection tests only used folder icons and missed this: test with a real image icon.
- 2026-11-03 (task 4): (a) Collection right-click → **Change Icon…** (`widgets.pick_icon`, `Window.change_icon`). (b) Collage: **Match clip lengths** Longest/Shortest buttons (`collage.match`, self-check in `collage.py` `__main__`); **pausing** returns every cell to the start of its window and the timeline marker to the selected cell's yellow start (`CollagePage.at_start`, reset by `_set_cover`/play). (c) Perf: for MPEG-1/2 & MJPEG cells the fast (preview) render passes `-lowres n` (`thumbs.lowres`, `_video_info` cached) — 4-VOB 540p preview 12.2 s → 4.2 s; reverse/bounce windows clamp to `MAX_WINDOW` (30 s; reverse buffers every frame in RAM); `thumbs.prune` caps `collage`/`previews` caches at 1.5 GB. GPU decode (`-hwaccel cuda`, `mpeg2_cuvid`) measured SLOWER (3.1→4.6 s on 4K, 0.7→1.4 s on 720p) so decode stays on CPU; encode already uses NVENC. Stress (synthetic ~1 GB VOBs made with ffmpeg concat `-c copy`): grab_frame 0.13–0.24 s at any offset, 9-cell preview 11 s, children peak RSS ~370 MB, re-render after editing one cell 6.5 s (cell cache hit; remaining cost = compose pass). (d) Two stray white pixels when zooming collections in the flatpak: **NOT reproduced** under Xvfb software rendering; hardened (icons pre-scaled with HYPER to ≤2× shown size in `widgets._texture`, icon holder `overflow=HIDDEN`). Unverified on real GPU — ask user where the pixels appear if it persists.
- 2026-11-03 (task 5): (a) **Preview-fails-then-works-on-second-Play bug**: preview/cell/proxy caches were built to a *shared* `<key>.tmp.*` file then renamed; Play + re-bake + Try it (or two cells) racing on the same key clobbered it / `rename` raised FileNotFoundError. Now everything goes through `thumbs.cached(out, build)`: per-file lock (one builder, others wait), unique temp file, atomic replace, ffprobe-validates the cached file (corrupt leftovers rebuilt), one retry. Used by `effects.preview`, `overlays.preview`, `collage._cell_file`, `CollagePage._try_path`, editor `.xN` repeat file, `thumbs.mp4_proxy`. Self-check in `thumbs.py __main__` (4 racing threads → 1 build). (b) **GPU/CPU**: Preferences → Performance → *GPU acceleration* switch (config key `gpu`, default true; greyed out when NVENC isn't detected; `thumbs.gpu_enabled()`/`nvenc_available()`; `LINUXLIVEPAPER_ENCODER=x264` still forces CPU). Affects encoding only (previews and saves); decode stays CPU (hw decode measured slower). (c) **Preview size**: `Preview: Full/720p/540p/360p/240p` dropdown in the editor and collage headers + a row in Preferences (config key `preview_height`, default 540, 0 = full; `thumbs.preview_height()`). Only affects preview renders (editor preview, collage Play/Try it; cache keys include the height); saving (`livemake.make_live`, `collage.render` without height) is untouched = full quality. Editor still-frame scrubbing stays 720p unless Full.
