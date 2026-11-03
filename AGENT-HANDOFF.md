# AGENT HANDOFF — LinuxLivePaper

Read this first. Update it before you stop (state, files touched, next steps, log line).

## Goal
**LinuxLivePaper** (`/home/montel/Documents/LinuxLivePaper`) is a **Linux-exclusive entertainment app** for
*live images* (stills that play when pressed, iPhone-Live-Photo style), inspired by the iOS app *intoLive*.
1. **Gallery** of the user's own collection (photos, videos, GIFs, live images) in an attractive grid. This is the core experience.
2. **Creator** with intoLive-style video effects/editing that outputs live images.

The user's decisions:
- iOS/iPhone compatibility doesn't matter for now ("we can worry about the format working outside of the Linux version later").
- Windows comes later. It's not a desktop-wallpaper engine (that would be a separate Rust project).

## Read in this order
1. `README.md`: how to run, what works
2. `docs/ARCHITECTURE.md`: stack, live-image format (still+video pair), effect→ffmpeg table, data model
3. `docs/ROADMAP.md`: phased checklist (tick boxes as you go)
4. `docs/INTOLIVE-ANALYSIS.md`: intoLive feature map, to use as the parity checklist for the creator

## Current state
- ✅ Gallery MVP (`linuxlivepaper/app.py`, `library.py`, `thumbs.py`): grid, filters, LIVE/GIF/duration badges,
  hover-to-play, viewer (press-and-hold live with sound, video, GIF, ←/→/Esc/Space).
- ✅ Quick creator (`livemake.py` + `CreateDialog`, the **+** button): video → trim + cover frame → `~/Pictures/LinuxLivePaper/LLP_*.jpg/.mov`.
- A demo item `~/Pictures/LinuxLivePaper/LLP_test.{jpg,mov}` was made from a screencast. It's safe to delete.
- Self-checks: `/usr/bin/python3 -m linuxlivepaper.library`, `/usr/bin/python3 -m linuxlivepaper.livemake`.

## How to test the UI headlessly (without capturing the user's real screen!)
```bash
cd ~/Documents/LinuxLivePaper
cat > /tmp/shot.py <<'EOF'
import gi, sys; gi.require_version('Gdk','3.0')
from gi.repository import Gdk
w=Gdk.get_default_root_window(); Gdk.pixbuf_get_from_window(w,0,0,w.get_width(),w.get_height()).savev(sys.argv[1],'png',[],[])
EOF
xvfb-run -a -s "-screen 0 1400x900x24" bash -c '/usr/bin/python3 -m linuxlivepaper & P=$!; sleep 10;
  xdotool mousemove 130 200; sleep 2; /usr/bin/python3 /tmp/shot.py /tmp/shot.png; kill $P'
```
Ignore the DRI/EGL warnings under Xvfb. Never screenshot the user's real `:0` display; it shows their private browsing.

## Environment gotchas
- Use **`/usr/bin/python3`** (3.12, GTK 4.14, libadwaita 1.5). The miniconda `python3` on PATH has no `gi`.
- GNOME 46 on X11. Installed: ffmpeg 6.1.1, GStreamer 1.24 (libav, avdec_gif, h265 decode), exiftool, Xvfb, xdotool.
  NVENC is available (`h264_nvenc`, `hevc_nvenc`).
- No new dependencies unless unavoidable (stdlib + PyGObject + ffmpeg CLI).
- The user's collection is ~350 files, mostly PNG screenshots plus 23 MP4 screencasts.
- Ghidra MCP (pyghidra-mcp) is its own empty project, not the user's CodeBrowser. Not needed now. Only revisit it for
  iPhone-compat details (`LiveVideoWriter`, `LiveKeyPhotoWriter`).

## Rules
- Don't copy intoLive assets (videos, fonts, LUTs) into the repo.
- Every non-trivial module gets one runnable self-check (`if __name__ == "__main__":` asserts).

## Next steps (suggested order)
1. Gallery polish: folder management + config, date sections, fullscreen/zoom, context menu (trash, open folder), favorites.
2. Creator Phase 2: `project.py` + `render.py` (ffmpeg filter graph), then the editor page and effects (ROADMAP Phases 3–4).
3. Package it: a `.desktop` file + icon.

## Log
- 2026-11-02: Phase 0. Static IPA analysis.
- 2026-11-02: Scope v2: iPhone Live Photo creator (superseded).
- 2026-11-02: Scope v3 (current): a Linux-exclusive gallery + creator; iOS compat deferred. Built the gallery MVP + quick creator; docs rewritten.
