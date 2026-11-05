"""Frame interpolation worker, run by a PyTorch-capable Python (the app itself needs no torch):
    python interp_worker.py '{"engine": "rife"|"flavr", "weights": ..., "code": ..., "factor": N, "w": W, "h": H,
                              "scale": 1.0}'
Reads raw yuv420p WxH frames on stdin, writes factor frames per input frame on stdout (the last input frame is held),
so N frames in -> N*factor frames out. YUV<->RGB (BT.601, limited range: what ffmpeg assumes for untagged video)
happens here on the GPU so ffmpeg's CPU colour conversion never touches the multiplied frames.
Stand-alone on purpose: no linuxlivepaper imports."""
import json
import sys

import torch
import torch.nn.functional as F


def frames(w, h):
    size, src = w * h * 3 // 2, sys.stdin.buffer
    while True:
        buf = bytearray(size)
        view, got = memoryview(buf), 0
        while got < size:
            n = src.readinto(view[got:])
            if not n:
                return
            got += n
        yield buf


class Runner:
    def __init__(self, a, dev):
        self.a, self.dev, self.out = a, dev, sys.stdout.buffer
        w, h = a["w"], a["h"]
        m = a["pad"]
        self.ph, self.pw = -h % m, -w % m

    def tensor(self, buf):
        w, h = self.a["w"], self.a["h"]
        b = torch.frombuffer(buf, dtype=torch.uint8).to(self.dev).float()
        y = (b[:w * h].view(1, 1, h, w) - 16) / 219
        uv = (b[w * h:].view(1, 2, h // 2, w // 2) - 128) / 224
        uv = F.interpolate(uv, (h, w), mode="nearest")
        u, v = uv[:, :1], uv[:, 1:]
        t = torch.cat((y + 1.402 * v, y - 0.344136 * u - 0.714136 * v, y + 1.772 * u), 1).clamp(0, 1)
        return F.pad(t, (0, self.pw, 0, self.ph), mode="replicate")

    def write(self, t):
        t = t[:, :, :self.a["h"], :self.a["w"]].clamp(0, 1)
        r, g, b = t[:, 0:1], t[:, 1:2], t[:, 2:3]
        y = 16 + 65.481 * r + 128.553 * g + 24.966 * b
        uv = F.avg_pool2d(torch.cat((128 - 37.797 * r - 74.203 * g + 112 * b,
                                     128 + 112 * r - 93.786 * g - 18.214 * b), 1), 2)
        out = torch.cat((y.flatten(), uv.flatten())).round().clamp(0, 255).byte()
        self.out.write(out.cpu().numpy().tobytes())


def rife(a, dev):
    sys.path[:0] = [a["code"], a["weights"]]  # model/warplayer.py, model/loss.py + train_log/*.py
    from train_log.RIFE_HDv3 import Model
    model = Model()
    model.load_model(a["weights"] + "/train_log", -1)
    model.eval()
    model.device()
    f, scale = a["factor"], a["scale"]
    a["pad"] = max(128, int(128 / scale))
    r = Runner(a, dev)

    prev = None
    for buf in frames(a["w"], a["h"]):
        cur = r.tensor(buf)
        if prev is not None:
            r.write(prev)
            for k in range(1, f):  # 4.x models take any timestep
                r.write(model.inference(prev, cur, k / f, scale))
        prev = cur
    for _ in range(f if prev is not None else 0):
        r.write(prev)


def flavr(a, dev):
    sys.path.insert(0, a["code"])
    from model.FLAVR_arch import UNet_3D_3D
    f = a["factor"]
    model = UNet_3D_3D("unet_18", n_inputs=4, n_outputs=f - 1, joinType="concat", upmode="transpose")
    sd = torch.load(a["weights"], map_location="cpu", weights_only=False)["state_dict"]
    model.load_state_dict({k.partition("module.")[-1]: v for k, v in sd.items()})
    model.to(dev).eval()
    scale = a["scale"]  # < 1: run the network on smaller frames (memory/speed), originals stay full size
    a["pad"] = 1
    r = Runner(a, dev)
    sw, sh = round(a["w"] * scale) // 32 * 32 or 32, round(a["h"] * scale) // 32 * 32 or 32
    small = lambda t: F.interpolate(t, (sh, sw), mode="bilinear", align_corners=False, antialias=True)
    full = lambda t: F.interpolate(t, (a["h"], a["w"]), mode="bilinear", align_corners=False)
    use_half = dev.type == "cuda"

    win = []  # sliding [i-1, i, i+1, i+2] window, edges padded by repeating the first/last frame

    def step(w4):
        r.write(w4[1][0])
        with torch.autocast("cuda", torch.float16, enabled=use_half):
            outs = model([x[1] for x in w4])
        for o in outs:
            r.write(full(o.float()))

    for buf in frames(a["w"], a["h"]):
        t = r.tensor(buf)
        item = (t, small(t))
        if not win:
            win = [item]
        win.append(item)
        if len(win) == 4:
            step(win)
            win.pop(0)
    if win:  # last pair (needs the last frame twice), then hold the last frame
        last = win[-1]
        if win[1] is not last:
            step(win + [last])
        for _ in range(f):
            r.write(last[0])


def main():
    a = json.loads(sys.argv[1])
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if dev.type == "cuda":
        torch.set_num_threads(2)  # the GPU does the work; don't let torch spin every CPU core
    torch.backends.cudnn.benchmark = True
    with torch.inference_mode():
        (rife if a["engine"] == "rife" else flavr)(a, dev)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
