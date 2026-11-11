"""compose 传入的 SLIDEX_* 旋钮必须容忍「空字符串」（2026-11-11 潜在生产事故）。

compose 用 ``${SLIDEX_X:-}`` 传旋钮：宿主 .env 未设置时进容器的是**空字符串**，
不是「变量不存在」。slidex 0.11.3 及更早对布尔开关写成
``os.environ.get("SLIDEX_TRAJ_POOL_ENABLED", "1") == "1"``——第二个参数是默认值
而非兜底，空串直接判 False，等于**静默关闭整个轨迹池**（回放退回合成轨迹）。

这里用 compose 里的真实旋钮清单做回归：把每个 SLIDEX_* 设成空串后，
``SlidexConfig.from_env()`` 的池开关必须仍是默认的「开」。它同时锁住两件事：

* slidex 保持「空值视同未设置」契约（0.11.4 起布尔也覆盖）；
* compose 新增旋钮时不会引入一个空串即翻车的键。
"""
from pathlib import Path

import pytest
import yaml

pytest.importorskip("slidex", reason="slidex 由 Dockerfile 单独安装")

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"


def _slidex_env_keys():
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8")) or {}
    keys = set()
    for item in (doc["services"]["xianyu-app"].get("environment") or []):
        text = str(item)
        if text.startswith("SLIDEX_"):
            keys.add(text.split("=", 1)[0])
    return keys


def test_compose_declares_slidex_knobs():
    keys = _slidex_env_keys()
    # 至少要有池开关和录制时序闸门，否则本测试会因集合为空而空转通过
    assert "SLIDEX_TRAJ_POOL_ENABLED" in keys
    assert "SLIDEX_RECORDED_GAP_MAX_MS" in keys


def test_empty_string_knobs_keep_pool_enabled(monkeypatch):
    from slidex.config import SlidexConfig

    for key in _slidex_env_keys():
        monkeypatch.setenv(key, "")
    cfg = SlidexConfig.from_env()
    assert cfg.trajectory_pool_enabled is True, (
        "compose 的 ${VAR:-} 注入空串后轨迹池被关掉了；需要 slidex>=0.11.4 的 parse_bool 契约"
    )
    assert cfg.trajectory_pool_rotation_strategy == "lru"
    assert cfg.trajectory_pool_recorded_only_mode is False


def test_empty_string_knobs_keep_numeric_defaults(monkeypatch):
    from slidex.config import SlidexConfig

    for key in _slidex_env_keys():
        monkeypatch.setenv(key, "")
    cfg = SlidexConfig.from_env()
    assert cfg.trajectory_pool_max_per_cookie == 50
    assert cfg.trajectory_pool_min_pool_size == 5
