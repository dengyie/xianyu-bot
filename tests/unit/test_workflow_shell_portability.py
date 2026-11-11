"""workflow 内联 shell 的静态守卫：语法能过、``$VAR`` 与中文之间必须有花括号。

部署 job 里的脚本只在真正部署时才被 bash 执行，而 ``.github/**`` 又在 workflow
的 paths-ignore 里——写坏一个 ``if`` 要等到"按了部署"才炸，炸的时候线上正在被改。
这里在 pytest 阶段先做两件事：

* 用 ``bash -n`` 过一遍每个 ``run:`` 块（GH 表达式先换成占位符，否则 ``${{ }}``
  本身就会被 bash 判成语法错误）；
* 禁止 ``$VAR`` 紧邻非 ASCII 字符。bash 3.2（macOS 自带）会把 UTF-8 首字节吞进
  变量名：``echo "属于 run=$OWNER，"`` 在 ``set -u`` 下直接报
  ``OWNER?: unbound variable`` 并退出。GitHub runner 与 Linux 宿主机是 bash 5
  不受影响，但这类脚本只能靠"真跑一遍"验证，而本地复现就得用 bash 3.2——
  写成 ``${VAR}`` 代价为零，却让同一段脚本在两种 bash 上都能测。
"""
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "docker-image.yml"


def _run_scripts():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")) or {}
    for job, spec in (data.get("jobs") or {}).items():
        for idx, step in enumerate(spec.get("steps") or []):
            script = step.get("run")
            if script:
                yield job, step.get("name") or "step%d" % idx, script


def test_every_run_block_parses():
    failures = []
    for job, name, script in _run_scripts():
        clean = re.sub(r"\$\{\{[^}]*\}\}", "GH_EXPR", script)
        handle = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
        try:
            handle.write(clean)
            handle.close()
            proc = subprocess.run(["bash", "-n", handle.name], capture_output=True, text=True)
        finally:
            Path(handle.name).unlink(missing_ok=True)
        if proc.returncode != 0:
            failures.append("%s / %s\n%s" % (job, name, proc.stderr.strip()))
    assert not failures, "workflow 的 run 块有语法错误：\n\n" + "\n\n".join(failures)


def test_no_unbraced_var_before_non_ascii():
    pattern = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])")
    offenders = []
    for job, name, script in _run_scripts():
        for match in pattern.finditer(script):
            line = script[: match.start()].count("\n") + 1
            offenders.append("%s / %s (script line %d): $%s" % (job, name, line, match.group(1)))
    assert not offenders, (
        "$VAR 后面紧跟非 ASCII 字符时请写成 ${VAR}"
        "（bash 3.2 会把 UTF-8 首字节吞进变量名，set -u 下直接退出）：\n  "
        + "\n  ".join(offenders)
    )
