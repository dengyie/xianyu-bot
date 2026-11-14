"""2026-11-15 深度 review 回归：退避连续链、失败分类、手动刷新 TTL、稳定期让路。

四个缺陷都指向同一类后果——"账号在被打，但系统不知道/不刹车"：

1. `_cleanup_password_login_failure_backoff` 在每次重试前把到期记录 pop 掉，
   `consecutive_count` 恒为 1：升级曲线（slider_punish 2x→4h）与
   "连续 5 次风控失败→自动暂停"保护全部失效（线上从未出现过
   consecutive_failure_protected）。
2. 登录链路真实产出的话术不在分类表里，落成 unknown：退避只有 300 秒，且
   unknown 被排除在"退避期跳过 token 预检"之外，配合 10 秒级重试变成持续打平台。
3. `manual_refresh` 状态的 `expires_at` 是 None，唯一解除路径是 UI 线程 finally；
   线程挂死就永久压制该账号的自动恢复。
4. 扫码稳定期让路被当成初始化鉴权失败记录（ERROR + 计数 + 可能误开熔断）。
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

import xianyu_auth_recovery as mod


class _Host(mod.XianyuAuthRecoveryMixin):
    _password_login_failure_backoff = {}
    _manual_refresh_state = {}
    _manual_refresh_lock = threading.Lock()
    cookie_id = "review-1926782908"

    def _has_recent_slider_success(self):
        return False

    def consume_manual_refresh_slider_failed_bypass(self, cookie_id):
        return False

    @classmethod
    def get_instance(cls, cookie_id):
        # 真实定义在宿主 XianyuLive 上；这里只关心状态机，没有运行实例
        return None


@pytest.fixture(autouse=True)
def _clean_state():
    _Host._password_login_failure_backoff.clear()
    _Host._manual_refresh_state.clear()
    yield
    _Host._password_login_failure_backoff.clear()
    _Host._manual_refresh_state.clear()


def _expire(host, cookie_id):
    """把当前退避推到"已到期",复刻下一次重试前的真实时序。"""
    host._password_login_failure_backoff[cookie_id]['until'] = time.time() - 5


# --------------------------------------------------------------------------
# 1. 连续失败链必须跨退避到期存活
# --------------------------------------------------------------------------

def test_expired_backoff_still_keeps_streak():
    host = _Host()
    cid = host.cookie_id
    host.set_password_login_failure_backoff(cid, "slider_punish", 600,
                                           escalation_factor=2.0, max_cap_seconds=14400)
    _expire(host, cid)

    # 到期后"可以重试了"，但失败链没断：记录还在，等待态不算 active
    assert host.get_password_login_failure_backoff(cid) is not None
    assert host._get_active_password_login_failure_backoff() is None


def test_escalation_continues_across_expiry():
    """每次重试前都会读取退避（触发 cleanup），升级曲线不能因此重来。"""
    host = _Host()
    cid = host.cookie_id
    seen = []
    for _ in range(4):
        host.set_password_login_failure_backoff(cid, "slider_punish", 600,
                                               escalation_factor=2.0, max_cap_seconds=14400)
        seen.append(host.get_password_login_failure_backoff(cid)["seconds"])
        _expire(host, cid)

    assert seen == [600, 1200, 2400, 4800]


def test_cleanup_drops_streak_beyond_retention_window():
    host = _Host()
    cid = host.cookie_id
    host.set_password_login_failure_backoff(cid, "slider_punish", 600)
    host._password_login_failure_backoff[cid]['until'] = (
        time.time() - host._password_login_backoff_streak_retention_seconds - 1
    )
    assert host.get_password_login_failure_backoff(cid) is None


@pytest.mark.asyncio
async def test_protection_triggers_once_streak_reaches_threshold():
    """连续 5 次风控失败 → 暂停账号。计数修好之前这条路径永远走不到。"""
    calls = []

    class ProtectHost(_Host):
        async def _apply_account_pause_state(self, **kwargs):
            calls.append(('pause', kwargs))
            return True

        async def send_account_paused_notification(self, **kwargs):
            calls.append(('notify', kwargs))
            return True

        async def _request_stop_after_account_pause(self, reason):
            calls.append(('stop', reason))
            return True

    host = ProtectHost()
    cid = host.cookie_id
    for _ in range(5):
        host.set_password_login_failure_backoff(cid, "slider_punish", 600,
                                               escalation_factor=2.0, max_cap_seconds=14400)
        protected = await host._protect_account_for_consecutive_failures(
            host.get_password_login_failure_backoff(cid)
        )
        if protected:
            break
        _expire(host, cid)

    assert protected is True
    assert [kind for kind, _ in calls] == ['pause', 'notify', 'stop']
    assert '连续5次slider_punish' in calls[0][1]['error_message']


# --------------------------------------------------------------------------
# 2. 分类：登录链路真实话术不能再落成 unknown
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    'message,expected_reason',
    [
        ("等待扫码登录超时（450秒）", "verification_required"),
        ("等待验证超时（450秒）", "verification_required"),
        ("当前身份验证页面已超时/失效，请重新发起验证", "verification_required"),
        ("登录状态未确认，无法获取Cookie", "login_not_confirmed"),
        ("登录未成功，无法获取Cookie", "login_not_confirmed"),
        ("获取Cookie失败", "login_not_confirmed"),
        ("滑块验证后登录状态未确认，请稍后重试", "login_not_confirmed"),
        ("refresh后关键Cookie仍未齐全，服务端Session仍未就绪: t, cookie2", "login_not_confirmed"),
        ("密码登录超过 600 秒未返回，已中止本次尝试并清理浏览器（已杀 PID 123）", "login_timeout"),
        ("密码登录失败，未获取到Cookie", "login_not_confirmed"),
    ],
)
def test_real_login_messages_are_classified(message, expected_reason):
    reason, seconds = _Host.classify_password_login_failure(message)
    assert reason == expected_reason
    assert seconds > 0


def test_unknown_backoff_also_skips_platform_token_request():
    """任何仍在生效的退避都不该再真打一次平台。"""
    host = _Host()
    host.last_token_refresh_status = "started"
    host.set_password_login_failure_backoff(host.cookie_id, "unknown", 300)
    assert host._should_skip_token_refresh_for_login_backoff() is True


# --------------------------------------------------------------------------
# 3. 手动刷新保护期必须有硬 TTL
# --------------------------------------------------------------------------

def test_manual_refresh_state_carries_hard_expiry():
    state = _Host.begin_manual_refresh(_Host.cookie_id)
    assert state['started'] is True

    stored = _Host._manual_refresh_state[_Host.cookie_id]
    assert stored['expires_at'] is not None
    assert stored['expires_at'] > time.time() + mod._PASSWORD_LOGIN_DEADLINE_SECONDS


def test_leaked_manual_refresh_state_self_heals():
    """UI 登录线程挂死时 finally 不会执行,状态必须自己过期。"""
    _Host.begin_manual_refresh(_Host.cookie_id)
    _Host._manual_refresh_state[_Host.cookie_id]['expires_at'] = time.time() - 1

    assert _Host.get_manual_refresh_state(_Host.cookie_id) is None


# --------------------------------------------------------------------------
# 4. 扫码稳定期让路 ≠ 初始化鉴权失败
# --------------------------------------------------------------------------

def test_qr_grace_deferral_raises_dedicated_exception():
    from XianyuAutoAsync import InitAuthDeferred, InitAuthError, XianyuLive

    assert not issubclass(InitAuthDeferred, InitAuthError), "让路不能被 InitAuthError 分支接走"

    host = SimpleNamespace(
        cookie_id="review-1926782908",
        current_token=None,
        last_token_refresh_time=0,
        token_refresh_interval=1,
        last_token_refresh_error_message="扫码登录稳定期中，剩余899秒",
        _should_defer_auth_recovery_for_qr_grace=lambda: True,
    )

    with pytest.raises(InitAuthDeferred):
        asyncio.run(XianyuLive.init(host, None))
