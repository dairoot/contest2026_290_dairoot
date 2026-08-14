#!/usr/bin/env python3
"""Train the DS-CNN wake-word model on the cached windows.

Split is by source clip (group), so no TTS clip leaks between train and
val.  Saves checkpoints/model_best.pt (weights + arch + norm stats) and
checkpoints/config.json with the operating threshold picked from the
val sweep.
"""

import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

import kws_common as K

ROOT = Path(__file__).parent
DATA = ROOT / "data"
CKPT = ROOT / "checkpoints"

# Frozen architecture — kws_nn.c implements exactly this
ARCH = dict(ch=48, blocks=3, conv1_k=(10, 4), conv1_s=(2, 2),
            conv1_p=(4, 1), t=K.T_FRAMES, m=K.NMEL)

EPOCHS = 40
BATCH = 128
LR = 1e-3
PATIENCE = 7
SEED = 20260814


class DSCNN(nn.Module):
    def __init__(self, a=ARCH):
        super().__init__()
        c = a["ch"]
        self.conv1 = nn.Conv2d(1, c, a["conv1_k"], a["conv1_s"], a["conv1_p"],
                               bias=False)
        self.bn1 = nn.BatchNorm2d(c)
        self.dw = nn.ModuleList()
        self.dwbn = nn.ModuleList()
        self.pw = nn.ModuleList()
        self.pwbn = nn.ModuleList()
        for _ in range(a["blocks"]):
            self.dw.append(nn.Conv2d(c, c, 3, 1, 1, groups=c, bias=False))
            self.dwbn.append(nn.BatchNorm2d(c))
            self.pw.append(nn.Conv2d(c, c, 1, bias=False))
            self.pwbn.append(nn.BatchNorm2d(c))
        self.fc = nn.Linear(c, 2)
        self.act = nn.ReLU(inplace=True)

    def forward(self, x):                      # x: (N,1,T,M)
        x = self.act(self.bn1(self.conv1(x)))
        for dw, dbn, pw, pbn in zip(self.dw, self.dwbn, self.pw, self.pwbn):
            x = self.act(dbn(dw(x)))
            x = self.act(pbn(pw(x)))
        x = x.mean(dim=(2, 3))                 # global average pool
        return self.fc(x)


def load_data():
    z = np.load(DATA / "cache.npz")
    n = np.load(DATA / "norm.npz")
    X = (z["X"].astype(np.float32) - n["mean"]) / n["std"]
    # per-window CMN over time, mirroring kws_nn.c (channel/level removal)
    X = X - X.mean(axis=1, keepdims=True)
    y = z["y"].astype(np.int64)
    g = z["group"]
    val = (g.astype(np.uint64) * 2654435761 % 2 ** 32) % 10 == 0  # ~10% of groups
    print(f"train {np.sum(~val)} / val {np.sum(val)} "
          f"(val pos {np.sum(y[val] == 1)}, neg {np.sum(y[val] == 0)})")
    mk = lambda idx, shuf: DataLoader(
        TensorDataset(torch.from_numpy(X[idx]).unsqueeze(1),
                      torch.from_numpy(y[idx])),
        batch_size=BATCH, shuffle=shuf, num_workers=2)
    return mk(~val, True), mk(val, False), n


@torch.no_grad()
def evaluate(model, loader):
    model.eval()
    probs, ys = [], []
    for xb, yb in loader:
        p = torch.softmax(model(xb), dim=1)[:, 1]
        probs.append(p)
        ys.append(yb)
    return torch.cat(probs).numpy(), torch.cat(ys).numpy()


def sweep(probs, ys):
    rows = []
    for th in np.arange(0.50, 0.99, 0.05):
        frr = np.mean(probs[ys == 1] < th)
        fpr = np.mean(probs[ys == 0] >= th)
        rows.append((float(th), float(frr), float(fpr)))
    return rows


def main():
    torch.manual_seed(SEED)
    CKPT.mkdir(exist_ok=True)
    train_loader, val_loader, norm = load_data()
    model = DSCNN()
    nparam = sum(p.numel() for p in model.parameters())
    print(f"params: {nparam}")
    opt = torch.optim.Adam(model.parameters(), lr=LR)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, EPOCHS)
    lossf = nn.CrossEntropyLoss()

    best_loss, best_state, bad = 1e9, None, 0
    for ep in range(EPOCHS):
        model.train()
        tot, cnt = 0.0, 0
        for xb, yb in train_loader:
            opt.zero_grad()
            loss = lossf(model(xb), yb)
            loss.backward()
            opt.step()
            tot += loss.item() * len(yb)
            cnt += len(yb)
        sched.step()
        probs, ys = evaluate(model, val_loader)
        vloss = nn.functional.cross_entropy(
            torch.log(torch.stack([1 - torch.tensor(probs),
                                   torch.tensor(probs)], 1).clamp_min(1e-7)),
            torch.tensor(ys)).item()
        acc = np.mean((probs >= 0.5) == ys)
        print(f"ep{ep:02d} train {tot / cnt:.4f}  val {vloss:.4f} "
              f"acc {acc:.4f}", flush=True)
        if vloss < best_loss - 1e-4:
            best_loss, bad = vloss, 0
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= PATIENCE:
                print("early stop")
                break

    model.load_state_dict(best_state)
    probs, ys = evaluate(model, val_loader)
    rows = sweep(probs, ys)
    print(" thr    FRR      FPR")
    for th, frr, fpr in rows:
        print(f" {th:.2f}  {frr:6.4f}  {fpr:6.4f}")
    # operating point: lowest θ≥0.7 with window-level FPR < 0.5%;
    # runtime smoothing suppresses most of what remains
    cand = [r for r in rows if r[0] >= 0.70 and r[2] < 0.005]
    th = cand[0][0] if cand else 0.90
    torch.save({"state_dict": model.state_dict(), "arch": ARCH,
                "mean": norm["mean"], "std": norm["std"]},
               CKPT / "model_best.pt")
    cfg = {"threshold": round(float(th), 2), "params": nparam,
           "val_sweep": rows,
           "val_acc": float(np.mean((probs >= 0.5) == ys))}
    (CKPT / "config.json").write_text(json.dumps(cfg, indent=1))
    print(f"saved. suggested threshold {th:.2f}")


if __name__ == "__main__":
    main()
