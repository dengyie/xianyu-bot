"""把池内加载侧判脏的录制移入 <cookie>/_quarantine/（保留证据、让出席位）。

脏录制（`recorded_trace_verdict` 非空：non_monotonic / jump / overshoot ...）
在 `_load_all` 里每次都被跳过，但仍占 `max_per_cookie` 名额并计入
`get_pool_stats`。删掉会丢排查材料，所以隔离而不删；`_load_all` 只扫
cookie 目录下的 `trajectory_*.json`，`_quarantine/` 子目录天然被忽略。

用法（容器内）：
    docker cp scripts/prune_dirty_recordings.py xianyu-auto-bot:/tmp/
    docker exec -w /app xianyu-auto-bot python /tmp/prune_dirty_recordings.py          # dry-run
    docker exec -w /app xianyu-auto-bot python /tmp/prune_dirty_recordings.py --apply  # 实际移动

2026-11-11 线上首发：30 个文件里 5 条脏（overshoot 2 / non_monotonic 2 / jump 1），
隔离后 25 条全部健康、WARNING 归零。其中 1000/1001 是 gate 上线前（11-10 22:27 /
23:29）的真人"拖过头再拉回"录制（retro 350px / 287px），回放必然过冲。
"""

"""把池内加载侧判脏的录制移入 <cookie>/_quarantine/，保留证据、让出席位。

脏录制（recorded_trace_verdict 非空：non_monotonic / jump / overshoot ...）
在 _load_all 里每次都被跳过，但仍占用 max_per_cookie 名额、并出现在
get_pool_stats 的计数里。删掉会丢排查材料，所以隔离而不是删除；
_load_all 只扫 cookie 目录下的 trajectory_*.json，子目录天然被忽略。

用法：python prune_dirty_recordings.py [--apply]   默认 dry-run
"""
import json
import os
import sys

from slidex._slide_geometry import recorded_trace_verdict

BASE = os.path.join(os.path.expanduser("~"), ".slidex", "trajectories")
APPLY = "--apply" in sys.argv

moved = 0
for cookie in sorted(os.listdir(BASE)):
    cdir = os.path.join(BASE, cookie)
    if not os.path.isdir(cdir) or cookie == "_quarantine":
        continue
    names = sorted(f for f in os.listdir(cdir)
                   if f.startswith("trajectory_") and f.endswith(".json"))
    dirty = []
    for name in names:
        path = os.path.join(cdir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError) as exc:
            print("  skip unreadable", name, exc)
            continue
        reason = recorded_trace_verdict(rec.get("points") or [], rec.get("distance"))
        if reason:
            dirty.append((name, reason))
    print("%s: files=%d dirty=%d %s" % (cookie, len(names), len(dirty),
                                        dict((r, sum(1 for _, x in dirty if x == r)) for _, r in dirty)))
    if not dirty:
        continue
    qdir = os.path.join(cdir, "_quarantine")
    for name, reason in dirty:
        src = os.path.join(cdir, name)
        dst = os.path.join(qdir, name)
        print("   %s -> _quarantine (%s)" % (name, reason))
        if APPLY:
            os.makedirs(qdir, exist_ok=True)
            os.replace(src, dst)
            moved += 1
print("mode:", "APPLY" if APPLY else "DRY-RUN", "moved:", moved)
