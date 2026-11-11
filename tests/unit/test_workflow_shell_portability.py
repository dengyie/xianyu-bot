"""workflow 内联 shell 的静态守卫：语法能过、``$VAR`` 与中文之间必须有花括号。

部署 job 里的脚本只在真正部署时才被 bash 执行，而 ``.github/**`` 又在 workflow
的 paths-ignore 里——写坏一个 ``if`` 要等到"按了部署"才炸，炸的时候线上正在被改。
这里在 pytest 阶段先做三件事：

* 用 ``bash -n`` 过一遍每个 ``run:`` 块（GH 表达式先换成占位符，否则 ``${{ }}``
  本身就会被 bash 判成语法错误）；
* 禁止 ``$VAR`` 紧邻非 ASCII 字符。bash 3.2（macOS 自带）会把 UTF-8 首字节吞进
  变量名：``echo "属于 run=$OWNER，"`` 在 ``set -u`` 下直接报
  ``OWNER?: unbound variable`` 并退出。GitHub runner 与 Linux 宿主机是 bash 5
  不受影响，但这类脚本只能靠"真跑一遍"验证，而本地复现就得用 bash 3.2——
  写成 ``${VAR}`` 代价为零，却让同一段脚本在两种 bash 上都能测。
* 把两处 ``/app`` 白名单闸门**原样抽出来执行**：只断言 workflow 里存在某段字符串，
  判据漂了也照样绿；这里用真实壳跑真实循环，钉住"越界目标必须被抓到"。
"""
from pathlib import Path
import os
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


# ── 白名单闸门：抽出真实 shell 循环执行，而不是只查字符串 ──

_WHITELIST_BLOCK = re.compile(
    r'ALLOWED="[^"]*"\n\s*BAD=""\n\s*while read -r target.*?done < (?P<src>\S+)',
    re.S,
)


def _whitelist_blocks():
    """返回 (块文本, 该块内联的允许目标列表)。

    两处闸门各内联一份 ALLOWED（宿主机没有仓库代码，只能复制），所以这里
    按块取各自的清单，不假设两份相同——同源由
    test_compose_no_source_mounts.test_runtime_gate_uses_the_same_whitelist 负责。
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    blocks = []
    for match in _WHITELIST_BLOCK.finditer(text):
        allowed = match.group(0).split('ALLOWED="', 1)[1].split('"', 1)[0]
        blocks.append((match.group(0), allowed.split()))
    return blocks


def _run_whitelist(block, mounts_text):
    """把块里的 `done < /tmp/xxx` 换成临时文件后真跑一遍，返回判出的越界目标。"""
    script = block.rsplit("done <", 1)[0] + 'done < "$MOUNTS"\nprintf "%s" "$BAD"'
    with tempfile.TemporaryDirectory() as tmp:
        mounts = Path(tmp) / "mounts.txt"
        mounts.write_text(mounts_text, encoding="utf-8")
        proc = subprocess.run(
            ["bash", "-c", script],
            env={**os.environ, "MOUNTS": str(mounts)},
            capture_output=True,
            text=True,
        )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split()


def test_whitelist_blocks_are_present():
    blocks = _whitelist_blocks()
    assert len(blocks) == 2, "应有两处白名单闸门（安装 compose + post-deploy），实际 %d" % len(blocks)


def test_whitelist_accepts_data_targets():
    for block, allowed in _whitelist_blocks():
        assert _run_whitelist(block, "\n".join(allowed)) == []


def test_whitelist_rejects_evasion_shapes():
    # 整树挂载、目录挂载、/app 顶层配置文件——旧的后缀黑名单三种全放过。
    for block, _allowed in _whitelist_blocks():
        assert _run_whitelist(block, "/app") == ["/app"]
        assert _run_whitelist(block, "/app/captcha") == ["/app/captcha"]
        assert _run_whitelist(block, "/app/config.json") == ["/app/config.json"]
        assert _run_whitelist(block, "/app/static/uploads2") == ["/app/static/uploads2"]


def test_whitelist_judges_unterminated_last_line():
    """末行不带换行符时也必须判到。

    裸 ``while read -r target`` 在 EOF 且末行无换行符时返回非零、循环体不执行，
    越界目标就这样被静默放过（已实证：旧写法 BAD=[]，新写法 BAD=[/app/captcha]）。
    两处产出者目前都写换行符，但闸门不能依赖上游的体贴。
    """
    for block, _allowed in _whitelist_blocks():
        assert _run_whitelist(block, "/app/data\n/app/captcha") == ["/app/captcha"]
        assert _run_whitelist(block, "/app/captcha") == ["/app/captcha"]
