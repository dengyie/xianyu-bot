"""线上轨迹池 / 轨迹 API 回归探针（容器内运行，无网络、无浏览器依赖）。

用法（生产容器）：
    docker cp scripts/dedupe_trajectory_pool.py xianyu-auto-bot:/tmp/dedupe_trajectory_pool.py
    docker exec -w /app -e PROBE_BASE=http://127.0.0.1:8090 xianyu-auto-bot python /tmp/dedupe_trajectory_pool.py

判定含义见文件内注释；任一 FAIL 都应在发版拦下（这些是 2026-11-11 事故后
建立的"新镜像是否真的在跑"的运行时事实校验）。
"""

"""One-off pool dedupe: exact-duplicate recordings occupy pool slots.

Duplicates are same-payload double writes (identical points+distance, same
recorded_at second) from 2026-10-12..14, before the recorder became
single-consumption (sessionStorage removeItem). Keep one file per payload
(lowest number = first write), delete the extras.

Usage: python dedupe_pool.py [--apply]
Default is a dry run.
"""
import json
import os
import sys

BASE = "/app/trajectory_history/.slidex/trajectories"
APPLY = "--apply" in sys.argv


def payload_key(rec):
    return json.dumps([rec.get("points"), rec.get("distance")], sort_keys=True)


total_deleted = 0
for cookie in sorted(os.listdir(BASE)):
    cdir = os.path.join(BASE, cookie)
    if not os.path.isdir(cdir):
        continue
    files = sorted(f for f in os.listdir(cdir) if f.startswith("trajectory_") and f.endswith(".json"))
    seen = {}
    doomed = []
    for name in files:
        path = os.path.join(cdir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError) as exc:
            print("  skip unreadable", name, exc)
            continue
        key = payload_key(rec)
        if key in seen:
            doomed.append((seen[key], name, rec.get("recorded_at")))
        else:
            seen[key] = name
    print("%s: files=%d unique=%d duplicates=%d" % (cookie, len(files), len(seen), len(doomed)))
    for keep, drop, when in doomed:
        print("   keep %s  drop %s  (%s)" % (keep, drop, when))
    if APPLY:
        for _, drop, _ in doomed:
            try:
                os.remove(os.path.join(cdir, drop))
                total_deleted += 1
            except OSError as exc:
                print("   delete failed", drop, exc)
print("mode:", "APPLY" if APPLY else "DRY-RUN", "deleted:", total_deleted)
