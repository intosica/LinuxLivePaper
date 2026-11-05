#!/bin/bash
# Run LinuxLivePaper inside the flatpak runtime under gdb, with verbose logging + a resource monitor.
#   ./run-debug.sh              run THIS source tree (no rebuild needed)
#   ./run-debug.sh --installed  run the code baked into the installed flatpak
# Everything goes to ~/llp-logs/run-<timestamp>.log:
#   - versions, fd limit
#   - app stdout/stderr (G_MESSAGES_DEBUG, GST_DEBUG=2, Python faulthandler)
#   - "[mon]" lines every 10 s: open fds (by kind), threads, RSS, GPU memory  → shows leaks building up
#   - on a crash: native backtrace of every thread (gdb) + last fd/thread snapshot
APP=io.github.linuxlivepaper
REPO=$(cd "$(dirname "$0")" && pwd)
SRC=(--filesystem="$REPO":ro --env=PYTHONPATH="$REPO")
[ "$1" = --installed ] && { SRC=(--env=PYTHONPATH=/app/share/linuxlivepaper); shift; }
mkdir -p ~/llp-logs; LOG=~/llp-logs/run-$(date +%Y%m%d-%H%M%S).log
echo "logging to $LOG"

{
  echo "== $(date)  source: ${SRC[*]}"
  flatpak info "$APP" | grep -E "Ref|Commit|Runtime"
  nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null
  flatpak run --command=sh "$APP" -c 'echo "fd limit in sandbox: $(ulimit -Sn) (hard $(ulimit -Hn))"
    python3 -c "import gi; gi.require_version(\"Gtk\",\"4.0\"); gi.require_version(\"Gst\",\"1.0\")
from gi.repository import Gtk, Gst; Gst.init(None)
print(\"GTK\", Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version(), \"|\", Gst.version_string())"'
} 2>&1 | tee -a "$LOG"

# Resource monitor: waits for the app's python process, samples until it is gone.
(
  for _ in $(seq 60); do PID=$(pgrep -n -f -- "^(/usr/bin/)?python3 -m linuxlivepaper") && break; sleep 1; done
  [ -z "$PID" ] && exit
  while [ -d /proc/$PID ]; do
    fds=$(ls -l /proc/$PID/fd 2>/dev/null | awk 'NR>1{t=($NF=="(deleted)") ? $(NF-1) : $NF; sub(/:.*/,"",t); sub(/^\/dev\/dri\/.*/,"dri",t); if (t ~ /^\// && t !~ /^\/dev\//) t="file"; c[t]++; n++}
      END{printf "%d (", n; for(k in c) printf "%s:%d ", k, c[k]; printf ")"}')
    thr=$(ls /proc/$PID/task 2>/dev/null | wc -l)
    gl=$(cat /proc/$PID/task/*/comm 2>/dev/null | grep -c gstglcontext)
    rss=$(awk '/VmRSS/{print int($2/1024)"M"}' /proc/$PID/status 2>/dev/null)
    gpu=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader 2>/dev/null)
    echo "[mon] $(date +%T) fds=$fds threads=$thr gl_ctx_threads=$gl rss=$rss gpu=$gpu"
    sleep 10
  done
) >> "$LOG" 2>&1 &
MON=$!

# gdb: run; only if the app died from a signal, dump the backtraces.
GDBX=$(mktemp ~/llp-logs/.gdb-XXXX); trap 'rm -f "$GDBX"' EXIT
cat > "$GDBX" <<'GDB'
set debuginfod enabled off
set pagination off
handle SIGPIPE SIGUSR1 SIGUSR2 nostop noprint pass
run
if $_isvoid($_exitcode)
  echo \n===== CRASH: crashing thread =====\n
  bt full 30
  echo \n===== all threads =====\n
  thread apply all bt 25
  kill
end
GDB

flatpak run --devel "${SRC[@]}" \
  --env=PYTHONFAULTHANDLER=1 --env=PYTHONUNBUFFERED=1 \
  --env=G_MESSAGES_DEBUG=all --env=GST_DEBUG=2 \
  --filesystem="$GDBX":ro --command=gdb "$APP" -q -batch -return-child-result -x "$GDBX" \
  --args python3 -m linuxlivepaper "$@" 2>&1 | tee -a "$LOG"
echo "exit: ${PIPESTATUS[0]}" | tee -a "$LOG"
kill $MON 2>/dev/null
journalctl --user --since "-2min" 2>/dev/null | grep -i -E "segfault|coredump|linuxlivepaper|python" | tail -20 | tee -a "$LOG"
