"""compose 在 `/app` 下只允许挂 6 个数据/配置目标——代码只能随镜像走。

事故经过（2026-11-11）：宿主机 `/opt/xianyu-auto-bot/docker-compose.yml` 漂移出
`./api_captcha_remote.py:/app/api_captcha_remote.py:ro`（以及同名 html）。宿主机那份
是 2026-09-29 的旧副本，作为只读层盖住镜像里的新版文件，于是出现「新镜像 + 六周前
代码」：镜像 revision 校验通过、健康检查绿，但 `/api/captcha/trajectory` 的 400 几何
闸门根本没上线。

**为什么判据是白名单而不是后缀黑名单**：旧判据 `^/app/.*\\.(py|html)$` 只拦得住
"单文件、后缀像代码"这一种形态。下列三种全部直通，且都不是假想：

* `- .:/app:rw` —— 整树挂载。这行就注释在本仓库 compose 第 36 行，是"开发时打开"
  的现成入口，是**最容易**被误开的一条；
* `- ./captcha:/app/captcha:ro` —— 目录挂载，target 里没有后缀；
* `- ./config.json:/app/config.json:ro` —— 数据/配置后缀，但落在 `/app` 顶层。

白名单把不变量写成"`/app` 下只准这 6 个目标"，形态怎么变都拦得住；新增合法数据
目录时改一处（本清单 + compose + workflow 的运行时闸门，后者由下面的测试钉住）。
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "docker-image.yml"

# 唯一事实源：/app 下允许出现的挂载目标。
ALLOWED_APP_TARGETS = frozenset(
    {
        "/app/data",
        "/app/logs",
        "/app/global_config.yml",
        "/app/backups",
        "/app/static/uploads",
        "/app/trajectory_history",
    }
)


def offending_targets(targets):
    """返回越界的 /app 挂载目标。

    `/app` 本身（整树挂载）以及 `/app` 下任何不在白名单里的路径都算越界
    ——不只是 .py/.html 文件。
    """
    bad = []
    for target in targets:
        if target == "/app" or target.startswith("/app/"):
            if target not in ALLOWED_APP_TARGETS:
                bad.append(target)
    return bad


def _mounts(service_filter=None):
    """解析 compose 的 (service, host_side, target)；匿名卷 host_side 为空串。"""
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8")) or {}
    out = []
    for service, spec in (data.get("services") or {}).items():
        if service_filter and service != service_filter:
            continue
        for vol in spec.get("volumes") or []:
            if isinstance(vol, dict):
                out.append((service, str(vol.get("source") or ""), str(vol.get("target") or "")))
                continue
            parts = str(vol).split(":")
            if len(parts) == 1:  # 匿名卷：只有容器内路径，不可能是宿主机覆盖
                out.append((service, "", parts[0]))
            else:
                out.append((service, parts[0], parts[1]))
    return out


def test_app_mount_targets_are_whitelisted():
    bad = offending_targets([target for _, _, target in _mounts("xianyu-app")])
    assert not bad, (
        "compose 在 /app 下挂了白名单之外的目标，会以只读层覆盖镜像内容"
        "（2026-11-11：/trajectory 的 400 闸门因此没生效）。代码请随镜像发布，"
        "新增数据目录请同步 ALLOWED_APP_TARGETS 与 workflow 的 ALLOWED=：\n  "
        + "\n  ".join(bad)
    )


def test_keeps_all_six_data_mounts():
    """护栏不能靠"把挂载全删了"通过：6 个数据/配置必须都在。"""
    targets = {target for _, _, target in _mounts("xianyu-app")}
    missing = ALLOWED_APP_TARGETS - targets
    assert not missing, "缺少数据/配置挂载：%s" % "、".join(sorted(missing))


@pytest.mark.parametrize(
    "target",
    [
        "/app",                          # 整树挂载（compose 里被注释的 .:/app:rw）
        "/app/captcha",                  # 目录挂载，target 无后缀
        "/app/api_captcha_remote.py",    # 事故原型：单文件覆盖
        "/app/captcha_control.html",
        "/app/config.json",              # 数据后缀但落在 /app 顶层
        "/app/static/uploads2",          # 与白名单前缀相同但不相等
    ],
)
def test_whitelist_rejects_non_data_targets(target):
    assert offending_targets([target]) == [target]


def test_whitelist_allows_data_targets():
    for target in ALLOWED_APP_TARGETS:
        assert offending_targets([target]) == []


def test_runtime_gate_uses_the_same_whitelist():
    """workflow 里两处内联白名单都必须与本清单一致，否则会漂移。

    宿主机上没有仓库代码，两处闸门只能各内联一份：安装 compose 那步（落盘前拦）
    与 post-deploy 那步（容器起来后拦）。这里把所有 ``ALLOWED="`` 都钉住——只查第
    一处的话，改了一处忘了另一处，联测阶段仍然是绿的。
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = 'ALLOWED="'
    assert marker in text, "workflow 里找不到 ALLOWED= 清单（运行时闸门被改写了？）"
    found = []
    for chunk in text.split(marker)[1:]:
        found.append(chunk.split('"', 1)[0])
    assert len(found) >= 2, "workflow 应有两处白名单（安装 compose + post-deploy 闸门），实际 %d 处" % len(found)
    for raw in found:
        assert set(raw.split()) == set(ALLOWED_APP_TARGETS), (
            "workflow 的 ALLOWED 与本测试的 ALLOWED_APP_TARGETS 不一致：\n"
            "  workflow: %s\n  test    : %s" % (sorted(raw.split()), sorted(ALLOWED_APP_TARGETS))
        )
