"""同账号已有滑块会话时，自动密码登录刷新必须让路。

2026-11-13 18:32:53 线上：18:23 那次账密登录正在等人工扫码并占着同账号的并发
槽位，18:31 的定时 Cookie 刷新又触发一次登录，排队 60 秒后抛「滑块验证等待槽位
超时」，被记成「密码登录刷新失败，Cookie可能仍然无效」——真正原因只是同账号
并发去重（设计如此），不该记成失败并列进退避/连续失败保护。
"""
import pytest


@pytest.mark.asyncio
async def test_password_login_refresh_defers_when_same_account_slider_active(monkeypatch):
    import xianyu_auth_recovery as mod

    class FakeDb:
        def mark_stale_risk_control_logs_failed(self, timeout_minutes=15, cookie_id=None):
            return 0

        def get_cookie_details(self, cookie_id):
            return {
                'username': 'someone',
                'password': 'secret',
                'show_browser': False,
                'cookie_value': 'unb=1',
            }

    risk_logs = []

    class FakeHost(mod.XianyuAuthRecoveryMixin):
        cookie_id = 'defer-1926782908'

        def __init__(self):
            self.cookies_str = 'unb=1'
            self.last_token_refresh_status = None
            self.last_token_refresh_error_message = ''
            self._last_password_login_time = {}
            self._password_login_cooldown = 60

        def is_manual_refresh_active(self, cookie_id, allow_handoff_recovery=False):
            return False

        def _should_defer_auth_recovery_for_qr_grace(self):
            return False

        def _get_active_password_login_failure_backoff(self, current_time=None):
            return None

        def acquire_auth_recovery_lock(self, cookie_id, owner):
            return True, None

        def release_auth_recovery_lock(self, cookie_id, owner):
            return True

        def _normalize_risk_trigger_scene(self, trigger_reason, default=None):
            return default or 'auto_cookie_refresh'

        @staticmethod
        def _new_risk_session_id(prefix):
            return f'{prefix}-session'

        def _create_risk_log(self, **kwargs):
            risk_logs.append(('create', kwargs))
            return 7

        def _update_risk_log(self, log_id, **kwargs):
            risk_logs.append(('update', kwargs))

        def _build_risk_event_meta(self, trigger_scene=None, extra=None):
            return {'scene': trigger_scene, **(extra or {})}

        def _has_active_same_account_slider_session(self):
            return True

    monkeypatch.setattr(mod, 'db_manager', FakeDb())
    monkeypatch.setattr(mod, 'log_captcha_event', lambda *a, **k: None)

    created_slider = []

    class ExplodingSlider:
        def __init__(self, *args, **kwargs):
            created_slider.append((args, kwargs))

    import slidex.stealth as stealth_mod

    monkeypatch.setattr(stealth_mod, 'XianyuSliderStealth', ExplodingSlider)

    host = FakeHost()
    result = await host._try_password_login_refresh('Cookie验证失败')

    assert result is False
    # 让路必须发生在创建浏览器之前：否则还是要去排队 60 秒
    assert created_slider == []

    updates = [kw for kind, kw in risk_logs if kind == 'update']
    assert updates, '让路必须落一条风控日志'
    assert updates[-1]['result_code'] == 'verification_in_progress'
    assert updates[-1]['processing_status'] == 'failed'
    assert updates[-1]['event_meta'].get('deferred') is True
