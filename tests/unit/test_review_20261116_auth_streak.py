"""2026-11-16 线上事故回归：跨失败原因的连续失败链必须能触发自动暂停保护。

事故现场（5 小时、单账号）：
    平台把密码登录强制打回扫码页 → 每一轮恢复都是「等扫码 450 秒 → 页面空 →
    整体超时」，失败原因在 verification_required 与 login_timeout 之间交替。

两处叠加缺陷让刹车彻底失灵：
1. `_is_counted_password_login_failure_reason` 不含这两类 → 直接判定"可继续重试"；
2. 更隐蔽的一处：即便计入，旧逻辑 `consecutive_count = prev+1 if prev_reason==reason
   else 1` 也要求"同一理由连击"才累加。理由交替时每轮都归 1，
   5 次阈值保护永远走不到。

修复后语义（见 set_password_login_failure_backoff docstring）：
- consecutive_count：任意理由的连续失败总数 → 驱动自动暂停保护；
- same_reason_count：连续同因连击数 → 仅驱动退避升级曲线。
"""
from __future__ import annotations

import time

import pytest

import xianyu_auth_recovery as mod


class _Host(mod.XianyuAuthRecoveryMixin):
    _password_login_failure_backoff = {}
    cookie_id = "incident-20261116"

    def _has_recent_slider_success(self):
        return False

    def consume_manual_refresh_slider_failed_bypass(self, cookie_id):
        return False


class _ProtectHost(_Host):
    def __init__(self):
        self.calls = []

    async def _apply_account_pause_state(self, **kwargs):
        self.calls.append(('pause', kwargs))
        return True

    async def send_account_paused_notification(self, **kwargs):
        self.calls.append(('notify', kwargs))
        return True

    async def _request_stop_after_account_pause(self, reason):
        self.calls.append(('stop', reason))
        return True


@pytest.fixture(autouse=True)
def _clean_state():
    _Host._password_login_failure_backoff.clear()
    yield
    _Host._password_login_failure_backoff.clear()


def _expire(cid):
    """把当前退避推到已到期，复刻"下一次重试前"的真实时序。"""
    _Host._password_login_failure_backoff[cid]['until'] = time.time() - 5


@pytest.mark.asyncio
async def test_alternating_reasons_still_reach_protection_threshold():
    """理由在 verification_required / login_timeout 间交替，仍须在第 5 次触发暂停。"""
    host = _ProtectHost()
    cid = host.cookie_id
    for i in range(5):
        reason = 'verification_required' if i % 2 == 0 else 'login_timeout'
        host.set_password_login_failure_backoff(cid, reason, 600)
        protected = await host._protect_account_for_consecutive_failures(
            host.get_password_login_failure_backoff(cid)
        )
        if protected:
            break
        _expire(cid)

    assert protected is True, "跨原因交替的失败链必须能累计到保护阈值"
    assert [kind for kind, _ in host.calls] == ['pause', 'notify', 'stop']
    # 保护文案用的是总连续失败数（第 5 轮即 i=4，理由回到 verification_required）
    assert '连续5次verification_required' in host.calls[0][1]['error_message']


def test_consecutive_count_accumulates_across_reasons():
    host = _Host()
    cid = host.cookie_id
    host.set_password_login_failure_backoff(cid, 'verification_required', 450)
    host.set_password_login_failure_backoff(cid, 'login_timeout', 300)
    host.set_password_login_failure_backoff(cid, 'verification_required', 450)
    state = host.get_password_login_failure_backoff(cid)
    assert state['consecutive_count'] == 3
    # 换因后同因连击数归 1（升级曲线不跨因放大）
    assert state['same_reason_count'] == 1


def test_success_clears_the_whole_streak():
    """成功恢复后连续失败链必须清零，否则正常工作也会累积到误暂停。"""
    host = _Host()
    cid = host.cookie_id
    host.set_password_login_failure_backoff(cid, 'verification_required', 450)
    host.set_password_login_failure_backoff(cid, 'login_timeout', 300)
    host.clear_password_login_failure_backoff(cid)
    host.set_password_login_failure_backoff(cid, 'verification_required', 450)
    state = host.get_password_login_failure_backoff(cid)
    assert state['consecutive_count'] == 1
    assert state['same_reason_count'] == 1


@pytest.mark.asyncio
async def test_non_counted_reason_does_not_accrue_or_trigger_protection():
    """良性失败（网络/未确认等）既不计入保护计数，也不因此触发暂停。"""
    host = _Host()
    cid = host.cookie_id
    for _ in range(10):
        host.set_password_login_failure_backoff(cid, 'login_not_confirmed', 300)
    state = host.get_password_login_failure_backoff(cid)
    assert state['consecutive_count'] == 0
    assert await host._protect_account_for_consecutive_failures(state) is False


def test_benign_failure_does_not_reset_risk_chain():
    """一次瞬断不该让此前的真实风控连击清零。"""
    host = _Host()
    cid = host.cookie_id
    for _ in range(3):
        host.set_password_login_failure_backoff(cid, 'verification_required', 450)
    host.set_password_login_failure_backoff(cid, 'network', 180)
    state = host.get_password_login_failure_backoff(cid)
    assert state['consecutive_count'] == 3
    assert state['reason'] == 'network'


@pytest.mark.asyncio
async def test_timeout_settlement_also_runs_consecutive_failure_protection():
    """超时收口路径（事故主失败模式）也必须走连续失败保护，不能只记退避。"""
    calls = []

    class TimeoutHost(_ProtectHost):
        last_token_refresh_status = 'started'
        last_token_refresh_error_message = ''

        def _safe_str(self, e):
            return str(e)

        def _force_kill_timeout_browser(self, slider, pids=None):
            return 'killed'

        def _update_risk_log(self, *a, **k):
            return True

        def _build_risk_event_meta(self, *a, **k):
            return {}

        async def send_token_refresh_notification(self, *a, **k):
            calls.append(('notify_timeout', a))
            return True

    host = TimeoutHost()
    cid = host.cookie_id
    # 先铺 4 次同类风控失败，第 5 次交给超时收口
    for _ in range(4):
        host.set_password_login_failure_backoff(cid, 'verification_required', 450)
        _expire(cid)

    slider = type('S', (), {'last_login_error': '等待扫码登录超时（450秒）'})()
    await host._handle_password_login_timeout(
        slider,
        risk_session_id=None,
        refresh_risk_log_id=None,
        trigger_scene=None,
        base_event_meta=None,
        risk_log_started_at=time.time(),
        captured_browser_pids=[],
    )

    assert [k for k, _ in host.calls] == ['pause', 'notify', 'stop'], \
        '超时收口必须触发连续失败保护（暂停/通知/停止）'
