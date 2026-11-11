"""线上轨迹池 / 轨迹 API 回归探针（容器内运行，无网络、无浏览器依赖）。

用法（生产容器）：
    docker cp scripts/audit_trajectory_pool.py xianyu-auto-bot:/tmp/audit_trajectory_pool.py
    docker exec -w /app -e PROBE_BASE=http://127.0.0.1:8090 xianyu-auto-bot python /tmp/audit_trajectory_pool.py

判定含义见文件内注释；任一 FAIL 都应在发版拦下（这些是 2026-11-11 事故后
建立的"新镜像是否真的在跑"的运行时事实校验）。
"""

"""Production audit for slidex 0.11.2 trajectory pool (runs inside the container).

Sections
  1. _load_all x5 per cookie: record counts must be stable, and WARNING lines must
     equal the number of dirty files (warn-once), not 5x that (the 0.11.1 spams).
  2. per-file recorded_trace_verdict tally (the same classifier _load_all uses).
  3. duplicate geometry detection (same decoded polyline stored twice).
  4. offline replay smoothness: decode -> normalize -> scale -> build_drag_events,
     report event count / design duration / max inter-event gap / endpoint error.
     No network, no browser: this is the "trajectory must not stutter" check.
"""
import io
import json
import os
from collections import Counter

from loguru import logger

from slidex._drag import build_drag_events
from slidex._gestures import (  # noqa: E402
    GestureSession,
)
from slidex._slide_geometry import recorded_trace_verdict, upgrade_points_to_cumulative
from slidex._trajectory_pool import SliderTrajectoryPool

BASE = os.path.join(os.path.expanduser("~"), ".slidex", "trajectories")
print("pool base:", BASE, "exists:", os.path.isdir(BASE))
cookies = sorted(d for d in os.listdir(BASE) if os.path.isdir(os.path.join(BASE, d)))
print("cookies:", cookies)


def section(title):
    print("\n== " + title + " ==")


section("1. load_all x5 + warn-once")
captured = io.StringIO()
handler_id = logger.add(captured, level="WARNING", format="{message}")
try:
    for cid in cookies:
        pool = SliderTrajectoryPool(base_dir=BASE)
        counts = [len(pool._load_all(cid)) for _ in range(5)]
        print("  %s: load_all x5 counts=%s stable=%s" % (cid, counts, len(set(counts)) == 1))
finally:
    logger.remove(handler_id)
warn_lines = [line for line in captured.getvalue().splitlines() if line.strip()]
print("  WARNING lines over 5 loads:", len(warn_lines))
print("  by reason:", dict(Counter(line.rsplit(":", 1)[-1].strip() for line in warn_lines)))

section("2. per-file verdict")
per_cookie_files = {}
for cid in cookies:
    files = sorted(
        f for f in os.listdir(os.path.join(BASE, cid))
        if f.startswith("trajectory_") and f.endswith(".json")
    )
    per_cookie_files[cid] = files
    verdicts = Counter()
    for name in files:
        with open(os.path.join(BASE, cid, name), encoding="utf-8") as fh:
            rec = json.load(fh)
        verdicts[recorded_trace_verdict(rec.get("points") or [], rec.get("distance")) or "ok"] += 1
    print("  %s (%d files): %s" % (cid, len(files), dict(verdicts)))

section("3. duplicate geometry")
for cid in cookies:
    seen = {}
    dups = []
    for name in per_cookie_files[cid]:
        with open(os.path.join(BASE, cid, name), encoding="utf-8") as fh:
            rec = json.load(fh)
        pts = rec.get("points") or []
        dist = rec.get("distance")
        if recorded_trace_verdict(pts, dist):
            continue
        decoded = upgrade_points_to_cumulative(pts, dist)
        key = tuple((round(p[0], 0), round(p[2], 0)) for p in decoded)
        if key in seen:
            dups.append((seen[key], name))
        else:
            seen[key] = name
    print("  %s: kept=%d unique=%d duplicate_pairs=%d" % (cid, len(seen) + len(dups), len(seen), len(dups)))
    for a, b in dups[:5]:
        print("     dup:", a, "<->", b)

section("4. offline replay smoothness")
for cid in cookies:
    target = 400.0
    pool = SliderTrajectoryPool(base_dir=BASE)
    rec = pool.load_best_trajectory(cid, target, distance_tolerance=0.25)
    if not rec:
        print("  %s: no record <=25%% of %.0fpx" % (cid, target))
        continue
    plan = GestureSession._plan_from_human_rec(rec, target)
    if plan is None:
        print("  %s: plan=None (decode/scale rejected)" % cid)
        continue
    sx, sy = 100.0, 500.0
    pts_abs = [(sx + float(dx), sy + float(dy), float(d or 0.0)) for (dx, dy, d) in plan.points]
    events = build_drag_events(sx, sy, pts_abs, extra_overshoot=plan.extra_overshoot)
    gaps = [g for g, _ in events]
    kinds = [params.get("type") for _, params in events]
    # 首段两次 approach move 与末端 mouseReleased 前的握持都是设计内的停顿，
    # 平滑度只看 displacement 段的相邻间隙（那里出现大洞才是真的卡顿）。
    motion = [g for g, k in zip(gaps, kinds) if k == "mouseMoved"]
    # motion[0..1] = approach 靠近，motion[2] = 按下停顿，motion[-1] = 末端握持：
    # 这四段都是设计内的停顿，平滑度只看中间的位移段，那里出现大洞才是真的卡顿。
    motion_core = motion[3:-1] if len(motion) > 4 else []
    end_err = abs(pts_abs[-1][0] - (sx + target))
    print("  %s: source=%s events=%d design=%.0fms press_hold=%.0fms end_hold=%.0fms"
          % (cid, os.path.basename(str(rec.get("_file"))), len(events), sum(gaps),
             gaps[2] if len(gaps) > 2 else -1, gaps[-1]))
    if motion_core:
        med = sorted(motion_core)[len(motion_core) // 2]
        print("     motion gaps n=%d median=%.1fms max=%.1fms end_err=%.1fpx"
              % (len(motion_core), med, max(motion_core), end_err))
        if max(motion_core) > 120:
            print("     STUTTER: motion gap %.0fms exceeds 120ms" % max(motion_core))
