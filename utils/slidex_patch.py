"""slidex 运行时增强补丁：画像健康度策略（按指纹块历史成功率降温/升温）。

历史说明：本文件曾有 apply_slidex_patch——把 human_like_trajectory 猴补到
``slidex.solver/.provider_mixin.generate_trajectory`` 上以替换"机器人信号"
轨迹。自 slidex 0.6.28 起求解循环改用 GestureSession（5 族正交手势），
该补丁的两个挂载目标在 xianyu 现役各 pin 上均不存在，实际从未生效
（2026-11-10 review 定案后删除）。轨迹策略现由 slidex 原生承担：
solver 路径 = GestureSession；stealth 路径 = 物理轨迹 + 真人轨迹池回放。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

from loguru import logger


_PROFILE_POLICY_FLAG = "__xianyu_slidex_profile_policy_patched"
_PROFILE_POLICY_WRAPPED_FLAG = "__xianyu_profile_policy_wrapped__"


def _profile_policy_enabled() -> bool:
    return os.environ.get("XY_SLIDER_PROFILE_POLICY", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def _profile_paths(slider: Any) -> Tuple[str, str, str]:
    """返回 (健康度状态文件, success.json, failure.json) 三个路径。"""
    from utils import slider_profile_health as health

    user_id = str(getattr(slider, "pure_user_id", "") or "unknown")
    success_file = str(getattr(slider, "success_history_file", "") or "")
    failure_file = str(getattr(slider, "failure_history_file", "") or "")
    base_dir = os.path.dirname(success_file) or "trajectory_history"
    return health.state_path(base_dir, user_id), success_file, failure_file


def _persist_identity_decision(slider: Any, identity: Dict[str, Any]) -> None:
    """把换块结果写回 browser_profile.json（供 legacy 链路/重启后复用）。"""
    path = str(getattr(slider, "browser_profile_file", "") or "")
    if not path:
        return
    try:
        payload: Dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                payload = loaded
        payload.update(identity)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # 临时名带 pid + 线程号：web 进程与 bot 进程可能同时写同一个账号的画像文件
        tmp_path = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
    except Exception as exc:  # pragma: no cover - 落盘失败不影响求解
        logger.debug(f"profile policy persist skipped: {exc}")


def _record_profile_outcome(slider: Any, success: bool) -> None:
    """把一次求解结果记到本次实际下发的画像块上。

    只在"画像真被应用"的链路上记：``browser_features`` 为空说明这条路根本没走
    画像注入；Windows 无头下 slidex 会把画像强制成 1600x900，index 选择不生效，
    记到 index 标签上会污染统计。
    """
    if not _profile_policy_effective(slider):
        return
    features = getattr(slider, "browser_features", None)
    if not features:
        return
    try:
        from utils import slider_profile_health as health

        profile_id = str(
            getattr(slider, "profile_id", "") or (features or {}).get("profile_id") or ""
        )
        if not profile_id or profile_id in {"unknown", "unassigned"}:
            return
        state_file, success_file, failure_file = _profile_paths(slider)
        index = _profile_index(slider)
        if not os.path.exists(state_file):
            health.backfill_from_history(state_file, success_file, failure_file, index=index)
        health.record_outcome(state_file, profile_id, success, index=index)
    except Exception as exc:  # pragma: no cover - 统计失败不影响求解
        logger.debug(f"profile health record skipped: {exc}")


def _profile_index(slider: Any) -> Optional[int]:
    """本次实际使用的画像 index。

    优先用策略补丁记下的 ``_xy_profile_index``；补丁安装晚于实例初始化时该属性
    不存在，退回 ``browser_identity`` 里真正参与选画像的那个 index（两者语义一致）。
    """
    index = getattr(slider, "_xy_profile_index", None)
    if index is None:
        identity = getattr(slider, "browser_identity", None) or {}
        index = identity.get("profile_index") if isinstance(identity, dict) else None
    try:
        return int(index) if index is not None else None
    except (TypeError, ValueError):
        return None


def _profile_policy_effective(slider: Any) -> bool:
    """画像 index 是否真的会影响本次浏览器特征。

    slidex 在「Windows + 无头」时会把画像强制成 1600x900 的桌面块（见
    ``_get_random_browser_features`` 里的 ``preferred_profile``），选中的 index 被
    忽略——此时既不该据此换块，也不该把结果记到被跳过的 index 上。
    """
    return not (os.name == "nt" and bool(getattr(slider, "headless", False)))


def apply_slidex_profile_policy_patch() -> bool:
    """按画像块的历史成功率调画像：坏块降温、好块升温（粘性，不随机轮换）。

    覆盖 slidex ``XianyuSliderStealth`` 的两个入口：
    - ``_load_or_create_browser_identity``：选 index 前先过一遍健康度策略；
    - ``solve_slider``：每次求解结束记一次结果（成功 / 滑了但没过 / 没滑不算）。

    只在 ``solve_slider`` 这一层记结果，是为了让分子分母对称：slidex 内部
    ``_save_success_record`` 受 ``enable_learning`` 控制、一次求解只记一次，而
    ``_save_failure_record`` 每次失败尝试都记，直接挂在那两个方法上会把
    "多次失败 vs 一次成功" 算进成功率，也会在关闭学习时只剩失败没有成功。

    幂等；slidex 未安装时静默跳过；``XY_SLIDER_PROFILE_POLICY=0`` 可整体关闭。
    """
    if not _profile_policy_enabled():
        return False
    try:
        import slidex.stealth as _stealth
    except Exception:
        return False

    slider_cls = getattr(_stealth, "XianyuSliderStealth", None)
    if slider_cls is None:
        return False
    if getattr(slider_cls, _PROFILE_POLICY_FLAG, False):
        return True

    try:
        from utils import slider_profile_health as health
    except Exception:
        return False

    original_identity = getattr(slider_cls, "_load_or_create_browser_identity", None)
    original_solve = getattr(slider_cls, "solve_slider", None)
    if original_identity is None or original_solve is None:
        return False
    if getattr(original_solve, _PROFILE_POLICY_WRAPPED_FLAG, False):
        # 已被更早的一次安装包过（例如模块被重新导入后 class 引用不同），
        # 再包一层会重复计数。
        try:
            setattr(slider_cls, _PROFILE_POLICY_FLAG, True)
        except Exception:
            pass
        return True

    def patched_identity(self, profile_count, language_count, profile_version=2):
        identity = original_identity(self, profile_count, language_count, profile_version)
        try:
            try:
                current_index = int(identity.get("profile_index", 0))
            except (TypeError, ValueError):
                current_index = 0
            try:
                self._xy_profile_index = current_index
            except Exception:
                pass
            if not _profile_policy_effective(self):
                return identity

            candidate_count = int(profile_count or 0)
            state_file, success_file, failure_file = _profile_paths(self)
            if os.path.exists(state_file):
                state = health.load_state(state_file)
            else:
                state = health.backfill_from_history(
                    state_file, success_file, failure_file, index=current_index
                )
            chosen, reason, changed = health.choose_index(state, current_index, candidate_count)
            if not changed:
                logger.debug(f"【{getattr(self, 'pure_user_id', '?')}】画像策略: {reason}")
                return identity
            state = health.apply_switch(state, current_index, chosen, reason)
            health.save_state(state_file, state)
            new_identity = dict(identity)
            new_identity["profile_index"] = chosen
            new_identity["profile_policy"] = {
                "from": current_index,
                "to": chosen,
                "reason": reason,
                "at": time.time(),
            }
            try:
                self.browser_identity = new_identity
                self._xy_profile_index = chosen
            except Exception:
                pass
            _persist_identity_decision(self, new_identity)
            logger.warning(
                f"【{getattr(self, 'pure_user_id', '?')}】画像切换: {reason} "
                f"(index {current_index} -> {chosen}, {health.label_for_index(state, chosen) or '未知画像'})"
            )
            return new_identity
        except Exception as exc:  # pragma: no cover - 兜底，绝不影响求解
            logger.debug(f"profile policy choose skipped: {exc}")
            return identity

    def patched_solve_slider(self, *args, **kwargs):
        result = original_solve(self, *args, **kwargs)
        try:
            # 没滑动就返回失败（没出现滑块 / 本地异常）不能算到画像头上；
            # 滑动过但没拿到 token 才算这个画像的一次失败。
            if bool(result) or bool(getattr(self, "_slider_did_slide", False)):
                _record_profile_outcome(self, bool(result))
        except Exception as exc:  # pragma: no cover - 统计失败不影响求解
            logger.debug(f"profile policy outcome skipped: {exc}")
        return result

    try:
        setattr(patched_solve_slider, _PROFILE_POLICY_WRAPPED_FLAG, True)
        slider_cls._load_or_create_browser_identity = patched_identity
        slider_cls.solve_slider = patched_solve_slider
        setattr(slider_cls, _PROFILE_POLICY_FLAG, True)
    except Exception as exc:  # pragma: no cover
        logger.debug(f"profile policy patch failed: {exc}")
        return False
    return True


__all__ = [
    "apply_slidex_profile_policy_patch",
]
