"""Optional AI frame interpolation for saved live images/collages: Practical-RIFE (any model from its list) or FLAVR
(2x/4x/8x). Off by default. The app itself never imports torch: interp_worker.py runs under whichever Python has
PyTorch (auto-detected, or set in Preferences), fed raw frames by one ffmpeg and feeding another.
Models + upstream network code download on demand to ~/.local/share/linuxlivepaper/interp/."""
import functools
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import library, thumbs

DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "linuxlivepaper" / "interp"
WORKER = Path(__file__).with_name("interp_worker.py")
DRIVE = "https://drive.usercontent.google.com/download?id={}&export=download&confirm=t"
CODE = {"rife": "https://codeload.github.com/hzwer/Practical-RIFE/tar.gz/refs/heads/main",
        "flavr": "https://codeload.github.com/tarun005/FLAVR/tar.gz/refs/heads/main"}

# latest Practical-RIFE models (README "Trained Model"; 4.25 is upstream's recommended default): version → Drive id
RIFE = {"4.26": "1gViYvvQrtETBgU1w8axZSsr7YUuw31uy", "4.25": "1ZKjcbmt1hypiFprJPIKW0Tt0lr_2i7bg",
        "4.25.lite": "1zlKblGuKNatulJNFf5jdB-emp9AqGK05"}
# FLAVR pretrained checkpoints (Vimeo-90K): factor → Google Drive file id. Each model does exactly its factor.
FLAVR = {2: "1IZe-39ZuXy3OheGJC-fT3shZocGYuNdH", 4: "1GARJK0Ti1gLH_O0spxAEqzbMwUKqE37S",
         8: "1xoZqWJdIOjSaE2DtH4ifXKlRwFySm5Gq"}
ENGINES = {"off": "Off", "rife": "RIFE (Practical-RIFE)", "flavr": "FLAVR"}
FACTORS = {"rife": [2, 3, 4, 8], "flavr": [2, 4, 8]}
MODES = {"smooth": "Smoother (more frames, same length)", "slow": "Slow motion (same frame rate, longer)"}
SCALES = {1.0: "Full", 0.5: "Half (faster, for 4K)"}
DEFAULTS = {"engine": "off", "rife": "4.25", "factor": 2, "mode": "smooth", "scale": 1.0, "python": ""}


def settings():
    s = {**DEFAULTS, **library.load_config().get("interp", {})}
    if s["factor"] not in FACTORS.get(s["engine"], [s["factor"]]):
        s["factor"] = 2
    return s


def save(**kw):
    library.set_pref("interp", {**library.load_config().get("interp", {}), **kw})


def enabled(s):
    return bool(s) and s.get("engine", "off") != "off" and int(s.get("factor", 1)) > 1


def stretch(s):
    """How much longer interpolation makes the clip (slow motion: factor, smoother: 1)."""
    return int(s["factor"]) if enabled(s) and s.get("mode") == "slow" else 1


def weights(s):
    """Path of the selected model's weights (exists once downloaded)."""
    if s["engine"] == "rife":
        return DATA / "rife" / s["rife"]
    return DATA / "flavr" / f"FLAVR_{int(s['factor'])}x.pth"


def code(engine):
    return DATA / "code" / engine


def installed(s):
    return (weights(s) / "train_log" / "flownet.pkl" if s["engine"] == "rife" else weights(s)).exists() and \
        (code(s["engine"]) / "model").is_dir()


@functools.lru_cache(maxsize=8)
def _has_torch(py):
    try:
        return subprocess.run([py, "-c", "import torch, torchvision, numpy"], capture_output=True, timeout=60
                              ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def python(s=None):
    """A Python that can import torch: the one set in Preferences, else the first that works among the usual places."""
    s = s or settings()
    if s.get("python"):
        return s["python"] if _has_torch(s["python"]) else None
    home = Path.home()
    seen = []
    for c in [sys.executable, shutil.which("python3"), DATA / "venv" / "bin" / "python",
              *(home / d / "bin" / "python3" for d in ("miniconda3", "anaconda3", "miniforge3", "mambaforge"))]:
        if c and Path(c).exists() and str(c) not in seen:
            seen.append(str(c))
            if _has_torch(str(c)):
                return str(c)
    return None


def _fetch(url, progress=None, share=(0, 1)):
    """Download url into memory, reporting progress(fraction) across the `share` slice of the whole job."""
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "LinuxLivePaper"}),
                                timeout=60) as r:
        total, buf = int(r.headers.get("Content-Length") or 0), io.BytesIO()
        while chunk := r.read(1 << 20):
            buf.write(chunk)
            if progress and total:
                progress(share[0] + (share[1] - share[0]) * buf.tell() / total)
    data = buf.getvalue()
    if data[:15].lower().startswith((b"<!doctype", b"<html")):
        raise RuntimeError("download blocked (Google Drive quota?) — try again later")
    return data


def install(s, progress=None):
    """Download the upstream network code (once per engine) and the selected model. Blocks."""
    engine = s["engine"]
    DATA.mkdir(parents=True, exist_ok=True)
    if not (code(engine) / "model").is_dir():
        with tempfile.TemporaryDirectory(dir=DATA) as d, \
                tarfile.open(fileobj=io.BytesIO(_fetch(CODE[engine], progress, (0, .1)))) as t:
            members = [m for m in t.getmembers() if m.name.split("/")[1:2] == ["model"]]
            t.extractall(d, members, filter="data")
            shutil.rmtree(code(engine), ignore_errors=True)
            code(engine).parent.mkdir(parents=True, exist_ok=True)
            os.replace(Path(d, members[0].name.split("/")[0]), code(engine))
    dest = weights(s)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if installed(s):
        pass
    elif engine == "flavr":
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(_fetch(DRIVE.format(FLAVR[int(s["factor"])]), progress, (.1, 1)))
        os.replace(tmp, dest)
    else:
        with tempfile.TemporaryDirectory(dir=DATA) as d:
            with zipfile.ZipFile(io.BytesIO(_fetch(DRIVE.format(RIFE[s["rife"]]), progress, (.1, 1)))) as z:
                z.extractall(d)
            # zips differ: train_log/ at the root or under RIFEv4.xx/, plus __MACOSX/ junk
            pkl = next(p for p in Path(d).rglob("flownet.pkl") if "__MACOSX" not in p.parts)
            out = Path(d, "out")
            out.mkdir()
            os.replace(pkl.parent, out / "train_log")
            shutil.rmtree(dest, ignore_errors=True)
            os.replace(out, dest)
    if progress:
        progress(1.0)


def _rate(src):
    """(num, den) frame rate of src's video stream."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate",
                        "-of", "csv=p=0", str(src)], capture_output=True, text=True).stdout.strip()
    n, _, d = r.partition("/")
    try:
        n, d = int(n), int(d or 1)
        return (n, d) if n and d else (30, 1)
    except ValueError:
        return 30, 1


def atempo(f):
    """atempo chain slowing audio down by f (each atempo stage only goes down to 0.5)."""
    out = []
    while f > 2:
        out.append("atempo=0.5")
        f /= 2
    return ",".join(out + [f"atempo={1 / f:.6f}"])


def apply(src, out, s, extra=()):
    """Write `out` = src with factor-1 AI frames between each pair. smooth: frame rate x factor, same length;
    slow: same frame rate, x factor longer (audio slowed to match, pitch kept)."""
    py = python(s)
    if not py:
        raise RuntimeError("Frame interpolation needs a Python with PyTorch — set it in Preferences")
    if not installed(s):
        raise RuntimeError("Download the interpolation model first (Interpolation → Download)")
    f = int(s["factor"])
    _, w, h = thumbs.probe(str(src))
    w, h = w // 2 * 2, h // 2 * 2
    n, d = _rate(src)
    rate = f"{n * f}/{d}" if s.get("mode") != "slow" else f"{n}/{d}"
    audio = ["-c:a", "copy"] if s.get("mode") != "slow" else ["-af", atempo(f), "-c:a", "aac"]
    args = json.dumps({"engine": s["engine"], "weights": str(weights(s)), "code": str(code(s["engine"])),
                       "factor": f, "w": w, "h": h, "scale": float(s.get("scale", 1.0))})
    # NVDEC decode when GPU acceleration is on; frames cross the pipes as yuv420p (half the bytes of RGB) and the
    # worker converts them on the GPU, so the CPU only shuffles bytes; NVENC encodes (thumbs.h264)
    dec = ["ffmpeg", "-v", "error", *thumbs.hwdec(src), "-i", str(src), "-map", "0:v:0",
           "-vf", f"scale={w}:{h},setsar=1,format=yuv420p", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]
    enc = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}", "-r", rate,
           "-i", "-", "-i", str(src), "-map", "0:v", "-map", "1:a:0?", *audio, *thumbs.h264(False, (w, h)),
           *extra, str(out)]
    with tempfile.TemporaryFile() as err:
        p1 = subprocess.Popen([*thumbs.NICE, *dec], stdout=subprocess.PIPE, stderr=err)
        p2 = subprocess.Popen([*thumbs.NICE, py, str(WORKER), args], stdin=p1.stdout, stdout=subprocess.PIPE, stderr=err)
        p1.stdout.close()
        p3 = subprocess.Popen([*thumbs.NICE, *enc], stdin=p2.stdout, stderr=err)
        p2.stdout.close()
        codes = [p3.wait(), p2.wait(), p1.wait()]
        if any(codes):
            err.seek(0)
            lines = err.read().decode(errors="replace").strip().splitlines()
            thumbs.log("interpolation failed:", codes, *lines[-20:])
            raise RuntimeError(f"Frame interpolation failed: {lines[-1] if lines else codes}")
    return out


if __name__ == "__main__":
    assert atempo(2) == "atempo=0.500000" and atempo(8) == "atempo=0.5,atempo=0.5,atempo=0.500000"
    assert atempo(3) == "atempo=0.5,atempo=0.666667"
    s = {**DEFAULTS, "engine": "rife", "factor": 3, "mode": "slow"}
    assert enabled(s) and stretch(s) == 3 and stretch({**s, "mode": "smooth"}) == 1 and not enabled(DEFAULTS)
    py = python(s)
    print("torch python:", py)
    tests = [({**s, "rife": v}, 3) for v in ("4.25",) if installed({**s, "rife": v})] + \
        [({**s, "engine": "flavr", "factor": 2, "mode": "smooth"}, 2)] * installed({**s, "engine": "flavr", "factor": 2})
    with tempfile.TemporaryDirectory() as d:
        src = Path(d, "a.mov")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1", "-f", "lavfi",
                        "-i", "sine=d=1", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)], check=True)
        for t, f in tests if py else []:
            out = apply(src, Path(d, f"{t['engine']}.mov"), t)
            dur, w, h = thumbs.probe(str(out))
            frames = int(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                                         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(out)],
                                        capture_output=True, text=True).stdout)
            assert (w, h) == (320, 180) and frames == 30 * f, (t, frames, w, h)
            assert abs(dur - stretch(t)) < 0.15, (t, dur)
            print(t["engine"], "ok", frames, "frames", f"{dur:.2f}s")
    print("interp ok")
