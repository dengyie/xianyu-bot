"""compose 只挂数据与配置，绝不挂应用代码（2026-11-11 线上事故回归）。

事故经过：宿主机 `/opt/xianyu-auto-bot/docker-compose.yml` 漂移出一条
`./api_captcha_remote.py:/app/api_captcha_remote.py:ro`（以及同名 html）。宿主机
那份是 2026-09-29 的旧副本，作为只读层盖住镜像里的新版文件，于是出现
「新镜像 + 六周前代码」：镜像 revision 校验通过、健康检查绿，但 `/api/captcha/
trajectory` 的 400 几何闸门根本没上线。旧副本里还留着 `redeem_control_ticket`
这个仓库里不存在、宿主机也没有定义的调用——带 `?ticket=` 直接 AttributeError 500。

规则：容器 `/app` 下的 .py / .html 只能来自镜像。数据目录、配置文件可以挂。
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"

CODE_SUFFIXES = (".py", ".html", ".js", ".sh")


def _volumes():
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8")) or {}
    out = []
    for service, spec in (data.get("services") or {}).items():
        for vol in spec.get("volumes") or []:
            if isinstance(vol, str):
                out.append((service, vol))
    return out


def test_compose_mounts_only_data_and_config():
    offenders = []
    for service, vol in _volumes():
        host_side = vol.split(":")[0]
        # 匿名卷（只写容器内路径）不可能是宿主机源码覆盖
        if not host_side.startswith((".", "/")):
            continue
        if host_side.lower().endswith(CODE_SUFFIXES):
            offenders.append("%s: %s" % (service, vol))
    assert not offenders, (
        "compose 挂载了应用代码/前端文件，会以只读层覆盖镜像内新版文件"
        "（2026-11-11：/trajectory 的 400 闸门因此没生效）。改为随镜像发布：\n  "
        + "\n  ".join(offenders)
    )


def test_compose_keeps_required_data_mounts():
    """护栏不能靠"把挂载全删了"来通过：数据/配置必须还在。"""
    mounts = {vol.split(":")[0] for _, vol in _volumes()}
    for required in ("./data", "./logs", "./trajectory_history", "./global_config.yml"):
        assert required in mounts, "缺少必需的数据/配置挂载：%s" % required


def test_api_file_is_not_shadowed_by_a_mount():
    for service, vol in _volumes():
        parts = vol.split(":")
        if len(parts) >= 2:
            dest = parts[1]
            assert dest not in ("/app/api_captcha_remote.py", "/app/captcha_control.html"), (
                "%s 的 %s 会被宿主机文件覆盖，线上跑的不是镜像里的代码" % (service, dest)
            )
