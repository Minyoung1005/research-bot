# Plotting & visualization
Conventions and code templates for figures that get auto-uploaded to Slack: training curves, run comparisons, image grids, and videos.

## Rules

1. **Always save to a file and state its absolute path in your reply** — the bot uploads any produced `.png`/`.mp4`/`.gif` it sees mentioned. Never rely on `plt.show()`.
2. `dpi=150`, `bbox_inches="tight"`, labeled axes **with units**, legend when >1 series, `alpha=0.25` raw + smoothed overlay for noisy curves.
3. Log-scale the y-axis when a loss spans decades. Steps or wall-clock on x — say which.
4. One message = one figure if possible; Slack previews the first image best.

## Training-curve template

```python
import re, numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def parse_metric(log_path, pattern=r"step[= ](\d+).*?loss[= ]([0-9.eE+-]+)"):
    steps, vals = [], []
    for line in open(log_path):
        m = re.search(pattern, line)
        if m:
            steps.append(int(m.group(1))); vals.append(float(m.group(2)))
    return np.array(steps), np.array(vals)

def ema(x, alpha=0.98):
    out, m = [], x[0]
    for v in x:
        m = alpha * m + (1 - alpha) * v
        out.append(m)
    return np.array(out)

fig, ax = plt.subplots(figsize=(7, 4))
for label, log in {"baseline": "runs/base/train.log", "ours": "runs/ours/train.log"}.items():
    s, v = parse_metric(log)
    (line,) = ax.plot(s, v, alpha=0.25)
    ax.plot(s, ema(v), color=line.get_color(), label=label)
ax.set_xlabel("training step"); ax.set_ylabel("loss"); ax.set_yscale("log")
ax.legend(); ax.grid(alpha=0.3)
out = "/tmp/loss_curves.png"
fig.savefig(out, dpi=150, bbox_inches="tight")
print(f"saved: {out}")
```

Works the same for TensorBoard event files (`tbparse`'s `SummaryReader`) or W&B (`wandb.Api().run(...).history()`); parse, then reuse the plotting block.

## Image grids

```python
import matplotlib.pyplot as plt, matplotlib.image as mpimg
paths = sorted(__import__("glob").glob("rollouts/ep*/frame_000.png"))[:16]
fig, axes = plt.subplots(4, 4, figsize=(12, 12))
for ax, p in zip(axes.flat, paths):
    ax.imshow(mpimg.imread(p)); ax.set_title(p.split("/")[-2], fontsize=8); ax.axis("off")
fig.savefig("/tmp/grid.png", dpi=150, bbox_inches="tight")
```

## Video / GIF from frames

```bash
ffmpeg -y -framerate 20 -pattern_type glob -i 'frames/*.png' \
       -c:v libx264 -pix_fmt yuv420p -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" /tmp/rollout.mp4
ffmpeg -y -i /tmp/rollout.mp4 -vf "fps=10,scale=480:-1" /tmp/rollout.gif   # Slack-friendly preview
```

Prefer `.mp4` for length, `.gif` for instant inline preview in Slack.
