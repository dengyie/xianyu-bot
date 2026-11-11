"""线上轨迹池 / 轨迹 API 回归探针（容器内运行，无网络、无浏览器依赖）。

用法（生产容器）：
    docker cp scripts/probe_trajectory_api.py xianyu-auto-bot:/tmp/probe_trajectory_api.py
    docker exec -w /app -e PROBE_BASE=http://127.0.0.1:8090 xianyu-auto-bot python /tmp/probe_trajectory_api.py

判定含义见文件内注释；任一 FAIL 都应在发版拦下（这些是 2026-11-11 事故后
建立的"新镜像是否真的在跑"的运行时事实校验）。
"""

"""Production API probe for the 0.11.2 trajectory geometry gate.

Runs INSIDE the xianyu-auto-bot container. Reads CAPTCHA_CONTROL_API_KEY from the
environment (never prints its value) and drives the real /api/captcha/trajectory
endpoint, including raw NaN / Infinity tokens and the null that JSON.stringify(NaN)
produces.

Expected on 0.11.2:
  healthy                       -> 200, one file created
  null / NaN / string coords    -> 400 invalid_points, nothing written
  overshoot / non_monotonic / too-few / nan-distance -> 400 with that reason
"""
import glob
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/app")

BASE = os.environ.get("PROBE_BASE", "http://127.0.0.1:8000")
KEY = (os.environ.get("CAPTCHA_CONTROL_API_KEY") or "").strip()
PATH = "/api/captcha/trajectory"
COOKIE = "probe-gate-ee5f1a"
POOL_ROOT = os.path.join(os.path.expanduser("~"), ".slidex", "trajectories")


def _files(cookie):
    return sorted(glob.glob(os.path.join(POOL_ROOT, cookie, "*.json")))


def _raw_post(body):
    req = urllib.request.Request(BASE + PATH, data=body.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Captcha-Control-Key", KEY)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def probe(tag, points, distance):
    cookie = COOKIE + "-" + tag
    body = json.dumps({
        "session_id": "probe",
        "cookie_id": cookie,
        "points": points,
        "distance": distance,
        "verify_url": "",
    })
    before = len(_files(cookie))
    code, text = _raw_post(body)
    return code, text, len(_files(cookie)) - before


def probe_raw(tag, raw_body):
    cookie = COOKIE + "-" + tag
    before = len(_files(cookie))
    code, text = _raw_post(raw_body.replace("@@COOKIE@@", cookie))
    return code, text, len(_files(cookie)) - before


HEALTHY = [[0.0, 700.0, 0.0], [80.0, 703.0, 310.0], [160.0, 709.0, 620.0],
           [245.0, 718.0, 940.0], [330.0, 729.0, 1290.0], [400.0, 740.0, 1600.0]]

CASES = [
    ("healthy", HEALTHY, 400.0, 200),
    ("nan-x", [[0.0, 700.0, 0.0], [float("nan"), 703.0, 310.0], [160.0, 709.0, 620.0]], 400.0, 400),
    ("inf-y", [[0.0, 700.0, 0.0], [80.0, float("inf"), 310.0], [160.0, 709.0, 620.0]], 400.0, 400),
    ("nan-dist", HEALTHY, float("nan"), 400),
    ("overshoot", [[0.0, 700.0, 0.0], [300.0, 700.0, 300.0], [620.0, 700.0, 600.0],
                   [300.0, 700.0, 900.0]], 400.0, 400),
    ("non-monotonic", [[0.0, 700.0, 0.0], [300.0, 700.0, 300.0], [620.0, 700.0, 600.0],
                       [900.0, 700.0, 900.0]], 400.0, 400),
    ("too-few", [[0.0, 700.0, 0.0]], 400.0, 400),
]

RAW_CASES = [
    # null / "abc" 在 Pydantic 请求模型层就被拒（422 float_type/float_parsing），
    # 比进业务层再判 invalid_points 更早，且同样零落盘——两者都是合格拒绝。
    ("null-x",
     '{"session_id":"probe","cookie_id":"@@COOKIE@@",'
     '"points":[[0.0,700.0,0.0],[null,703.0,310.0]],"distance":400.0,"verify_url":""}',
     (400, 422)),
    ("string-x",
     '{"session_id":"probe","cookie_id":"@@COOKIE@@",'
     '"points":[[0.0,700.0,0.0],["abc",703.0,310.0]],"distance":400.0,"verify_url":""}',
     (400, 422)),
    ("literal-nan",
     '{"session_id":"probe","cookie_id":"@@COOKIE@@",'
     '"points":[[0.0,700.0,0.0],[NaN,703.0,310.0]],"distance":400.0,"verify_url":""}',
     400),
]


def main():
    import shutil

    print("key_configured:", bool(KEY), "len:", len(KEY))
    print("pool_root:", POOL_ROOT, "exists:", os.path.isdir(POOL_ROOT))
    for stale in glob.glob(os.path.join(POOL_ROOT, COOKIE + "*")):
        shutil.rmtree(stale, ignore_errors=True)
    failures = []
    for tag, points, distance, want in CASES:
        code, text, delta = probe(tag, points, distance)
        ok = code == want and delta == (1 if want == 200 else 0)
        print("  %-16s -> http=%s want=%s files+=%s %s | %s"
              % (tag, code, want, delta, "OK" if ok else "MISMATCH", text[:90]))
        if not ok:
            failures.append(tag)
    for tag, raw, want in RAW_CASES:
        code, text, delta = probe_raw(tag, raw)
        want_ok = code in want if isinstance(want, tuple) else code == want
        ok = want_ok and delta == 0
        print("  %-16s -> http=%s want=%s files+=%s %s | %s"
              % (tag, code, want, delta, "OK" if ok else "MISMATCH", text[:90]))
        if not ok:
            failures.append(tag)

    from slidex._slide_geometry import recorded_trace_verdict

    bad_values = []
    persisted = 0
    for fp in glob.glob(os.path.join(POOL_ROOT, COOKIE + "*", "*.json")):
        with open(fp, encoding="utf-8") as fh:
            rec = json.load(fh)
        persisted += 1
        for point in rec.get("points") or []:
            for value in point:
                if not isinstance(value, (int, float)):
                    bad_values.append((fp, point))
                    continue
                if value != value or value in (float("inf"), float("-inf")):
                    bad_values.append((fp, point))
        verdict = recorded_trace_verdict(rec.get("points") or [], rec.get("distance"))
        print("  persisted %s verdict=%s" % (os.path.basename(fp), verdict or "ok"))
    print("persisted_files:", persisted, "non_finite_values:", bad_values)
    ok = not failures and not bad_values and persisted == 1
    print("RESULT:", "PASS" if ok else "FAIL failures=%s" % failures)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
