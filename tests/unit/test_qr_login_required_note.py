"""2026-11-16 线上事故回归：扫码登录必须"有人看得见"，且外层时限不能掐死内层等待。

事故形态（单账号静默数小时）：
    平台把账密登录强制打回扫码页 → 每轮都在等人工扫码 → 但
    1. 外层 asyncio.wait_for 的 600s 比内层 XY_VERIFICATION_WAIT_TIMEOUT(450s)
       加登录步骤还短，于是总在平台等扫码的中途杀掉 Chromium；
    2. 超时收口把状态写成 failed，status_note 又是空的 → 面板告警消失，
       通知渠道 0 个时 send_token_refresh_notification 还是空操作；
    3. 结果：用户既看不到"要扫码"，也不知道二维码已失效。

修复契约：
- 整体时限由「内层验证等待 + 登录步骤预算」推导，默认严格大于内层等待；
- 平台要求扫码时把 `待扫码登录（需人工）` 落进 status_note（面板告警的开关）；
- 状态保持 verification_pending_manual，不被后续收口压成 failed；
- 恢复成功后按条件清除该文案，不误伤其他子系统的示警。
"""
from __future__ import annotations

import time

import pytest

import xianyu_auth_recovery as mod
from db_manager import db_manager


# ---------------------------------------------------------------- 时限推导

def test_deadline_default_exceeds_inner_verification_wait(monkeypatch):
    """默认配置下，外层时限必须严格大于内层扫码等待（否则必然中途杀浏览器）。"""
    monkeypatch.delenv('PASSWORD_LOGIN_DEADLINE_SECONDS', raising=False)
    monkeypatch.delenv('XY_VERIFICATION_WAIT_TIMEOUT', raising=False)

    wait = mod._resolve_verification_wait_seconds()
    deadline = mod._resolve_password_login_deadline_seconds()

    assert wait == float(mod._DEFAULT_VERIFICATION_WAIT_SECONDS)
    assert deadline > wait
    # 至少要留出一次完整的登录步骤预算，不能"刚好"等于等待时长
    assert deadline >= wait + 60


def test_deadline_tracks_custom_verification_wait(monkeypatch):
    """调大/调小 XY_VERIFICATION_WAIT_TIMEOUT 时，外层时限必须跟着走，不能漂移。"""
    monkeypatch.delenv('PASSWORD_LOGIN_DEADLINE_SECONDS', raising=False)

    for wait_value in ('120', '900'):
        monkeypatch.setenv('XY_VERIFICATION_WAIT_TIMEOUT', wait_value)
        wait = mod._resolve_verification_wait_seconds()
        deadline = mod._resolve_password_login_deadline_seconds()
        assert wait == float(wait_value)
        assert deadline == pytest.approx(wait + mod._LOGIN_STEP_BUDGET_SECONDS)


def test_explicit_deadline_env_wins_and_bad_values_fall_back(monkeypatch):
    """显式配置优先；非法值不得让模块导入期直接抛错。"""
    monkeypatch.setenv('XY_VERIFICATION_WAIT_TIMEOUT', '450')
    monkeypatch.setenv('PASSWORD_LOGIN_DEADLINE_SECONDS', '1234')
    assert mod._resolve_password_login_deadline_seconds() == pytest.approx(1234.0)

    monkeypatch.setenv('PASSWORD_LOGIN_DEADLINE_SECONDS', 'not-a-number')
    assert mod._resolve_password_login_deadline_seconds() == pytest.approx(
        mod._resolve_verification_wait_seconds() + mod._LOGIN_STEP_BUDGET_SECONDS
    )


def test_module_constant_matches_resolver(monkeypatch):
    """模块级常量必须与 resolver 同源，避免两套算法各自漂移。"""
    monkeypatch.delenv('PASSWORD_LOGIN_DEADLINE_SECONDS', raising=False)
    monkeypatch.delenv('XY_VERIFICATION_WAIT_TIMEOUT', raising=False)
    assert mod._PASSWORD_LOGIN_DEADLINE_SECONDS == mod._resolve_password_login_deadline_seconds()


# ------------------------------------------------------- 扫码失败信息识别

@pytest.mark.parametrize(
    'message',
    [
        '等待扫码登录超时（450秒）',
        '请使用闲鱼App扫码登录',
        'https://passport.goofish.com/mini_login.htm',
        '等待验证超时，未完成扫码',
        'verification_required',
        'verification_pending_manual',
    ],
)
def test_qr_markers_recognised(message):
    assert mod._is_qr_login_required_message(message) is True


@pytest.mark.parametrize(
    'message',
    [
        '',
        None,
        '滑块验证失败',
        '网络连接超时',
        '账号密码错误',
        'session expired',
    ],
)
def test_non_qr_messages_not_misclassified(message):
    """宁可少判，不可错判：滑块/网络/密码错误不能被当成扫码。"""
    assert mod._is_qr_login_required_message(message) is False


# ------------------------------------------------- status_note 条件清除语义

class TestClearStatusNoteIfEquals:
    def test_clears_when_value_matches(self):
        db_manager.save_cookie("9900000001", "unb=t", user_id=1)
        db_manager.update_cookie_status_note("9900000001", "待扫码登录（需人工）")

        assert db_manager.clear_cookie_status_note_if_equals("9900000001", "待扫码登录（需人工）") is True
        assert db_manager.get_cookie_details("9900000001")["status_note"] == ""

    def test_keeps_other_subsystem_note(self):
        """别的子系统写的示警不能被本链路顺手抹掉。"""
        db_manager.save_cookie("9900000002", "unb=t", user_id=1)
        db_manager.update_cookie_status_note("9900000002", "待风控处理")

        assert db_manager.clear_cookie_status_note_if_equals("9900000002", "待扫码登录（需人工）") is False
        assert db_manager.get_cookie_details("9900000002")["status_note"] == "待风控处理"

    def test_empty_expected_is_noop(self):
        """空期望值等价于"无条件清空"，必须拒绝，否则会变成危险的全清接口。"""
        db_manager.save_cookie("9900000003", "unb=t", user_id=1)
        db_manager.update_cookie_status_note("9900000003", "待风控处理")

        assert db_manager.clear_cookie_status_note_if_equals("9900000003", "") is False
        assert db_manager.get_cookie_details("9900000003")["status_note"] == "待风控处理"


# --------------------------------------------------- mixin 的标记/清除行为

class _NoteDB:
    """替身 db_manager：只记录状态文案写入/条件清除。"""

    def __init__(self):
        self.notes = {}
        self.clears = []

    def update_cookie_status_note(self, cookie_id, note):
        self.notes[cookie_id] = note
        return True

    def clear_cookie_status_note_if_equals(self, cookie_id, expected):
        self.clears.append((cookie_id, expected))
        if self.notes.get(cookie_id) == expected:
            self.notes[cookie_id] = ''
            return True
        return False


class _NoteHost(mod.XianyuAuthRecoveryMixin):
    cookie_id = "c-note"

    def _safe_str(self, e):
        return str(e)


@pytest.fixture
def note_db(monkeypatch):
    fake = _NoteDB()
    monkeypatch.setattr(mod, 'db_manager', fake)
    return fake


def test_mark_qr_login_required_writes_panel_note(note_db):
    host = _NoteHost()
    host._mark_qr_login_required('等待扫码登录超时（450秒）')

    assert host._qr_login_required is True
    assert '扫码' in host._qr_login_required_message
    assert note_db.notes['c-note'] == mod._QR_LOGIN_REQUIRED_NOTE


def test_clear_qr_login_required_only_touches_own_note(note_db):
    host = _NoteHost()
    host._mark_qr_login_required('扫码')
    host._clear_qr_login_required()

    assert host._qr_login_required is False
    assert host._qr_login_required_message == ''
    assert note_db.clears == [('c-note', mod._QR_LOGIN_REQUIRED_NOTE)]
    assert note_db.notes['c-note'] == ''


def test_mark_tolerates_db_failure(monkeypatch):
    """写面板失败不能影响登录收口（否则会掩盖真实失败原因）。"""
    class _Broken:
        def update_cookie_status_note(self, *a, **k):
            raise RuntimeError('db down')

    monkeypatch.setattr(mod, 'db_manager', _Broken())
    host = _NoteHost()
    host._mark_qr_login_required('扫码')

    assert host._qr_login_required is True


# ------------------------------------------------------ 超时收口的状态语义

class _TimeoutHost(mod.XianyuAuthRecoveryMixin):
    cookie_id = "c-timeout-qr"

    def __init__(self):
        self.last_token_refresh_status = 'started'
        self.last_token_refresh_error_message = ''
        self.notifications = []

    def _safe_str(self, e):
        return str(e)

    def _force_kill_timeout_browser(self, slider, pids=None):
        return 'killed'

    def classify_password_login_failure(self, message):
        return ('login_timeout', 300)

    def set_password_login_failure_backoff(self, *a, **k):
        return None

    def get_password_login_failure_backoff(self, cookie_id):
        return {}

    async def _protect_account_for_consecutive_failures(self, state):
        return False

    def _build_risk_event_meta(self, *a, **k):
        return {}

    def _update_risk_log(self, *a, **k):
        return True

    async def send_token_refresh_notification(self, message, notification_type, **kwargs):
        self.notifications.append((message, notification_type))
        return True


def _slider(reason):
    return type('S', (), {'last_login_error': reason})()


@pytest.mark.asyncio
async def test_timeout_with_qr_reason_keeps_manual_pending_status(note_db):
    """超时原因是"等扫码"时，状态必须留在待人工处理，面板告警才不消失。"""
    host = _TimeoutHost()
    await host._handle_password_login_timeout(
        _slider('等待扫码登录超时（450秒）'),
        risk_session_id=None,
        refresh_risk_log_id=None,
        trigger_scene=None,
        base_event_meta=None,
        risk_log_started_at=time.time(),
        captured_browser_pids=[],
    )

    assert host.last_token_refresh_status == 'verification_pending_manual'
    assert note_db.notes['c-timeout-qr'] == mod._QR_LOGIN_REQUIRED_NOTE


@pytest.mark.asyncio
async def test_timeout_without_qr_reason_stays_failed(note_db):
    """普通假死/OOM 超时仍按失败处理，不得凭空写"待扫码"污染面板。"""
    host = _TimeoutHost()
    await host._handle_password_login_timeout(
        _slider('页面加载超时'),
        risk_session_id=None,
        refresh_risk_log_id=None,
        trigger_scene=None,
        base_event_meta=None,
        risk_log_started_at=time.time(),
        captured_browser_pids=[],
    )

    assert host.last_token_refresh_status == 'failed'
    assert 'c-timeout-qr' not in note_db.notes


@pytest.mark.asyncio
async def test_timeout_uses_round_flag_even_without_marker(note_db):
    """回调已标记本轮要扫码时，即使 last_login_error 措辞变了也不能退化成 failed。"""
    host = _TimeoutHost()
    host._qr_login_required = True
    host._qr_login_required_message = '请扫码登录'

    await host._handle_password_login_timeout(
        _slider('Target page, context or browser has been closed'),
        risk_session_id=None,
        refresh_risk_log_id=None,
        trigger_scene=None,
        base_event_meta=None,
        risk_log_started_at=time.time(),
        captured_browser_pids=[],
    )

    assert host.last_token_refresh_status == 'verification_pending_manual'
    assert note_db.notes['c-timeout-qr'] == mod._QR_LOGIN_REQUIRED_NOTE


# ------------------------------------------------------------- 源契约

def test_inner_failure_branch_also_keeps_qr_status():
    """内层失败（不走外层 TimeoutError）同样要保留待人工状态。"""
    src = open('xianyu_auth_recovery.py', encoding='utf-8').read()

    marker = "if getattr(self, '_qr_login_required', False) or _is_qr_login_required_message(login_error):"
    assert marker in src
    # 必须在风控重试/退避分类之前判定，否则状态会被后续赋值覆盖
    assert src.index(marker) < src.index("await self._protect_account_from_risk_login_retry(login_error)")


def test_round_start_resets_qr_flag():
    """每轮开始先清标记，上一轮残留不得污染本轮判定。"""
    src = open('xianyu_auth_recovery.py', encoding='utf-8').read()

    # 定位本轮开始到回调定义之间的窗口，断言清零发生在窗口内、回调之前
    window_start = src.index("开始使用{browser_mode}浏览器进行密码登录刷新Cookie")
    window_end = src.index("async def notification_callback_wrapper(")
    assert window_start < window_end
    window = src[window_start:window_end]
    assert "self._qr_login_required = False" in window
    assert "self._qr_login_required_message = ''" in window


def test_exception_path_keeps_qr_note():
    """异常收口（多半是 Chromium 被杀后的 Target closed 连锁报错）也要留住面板文案。"""
    src = open('xianyu_auth_recovery.py', encoding='utf-8').read()

    exc_start = src.index("except Exception as refresh_e:")
    exc_end = src.index("finally:", exc_start)
    block = src[exc_start:exc_end]
    assert "'_qr_login_required'" in block
    assert "self._mark_qr_login_required(" in block
    # 风控暂停优先级更高，扫码文案只能写在它之后
    assert block.index("_protect_account_from_risk_login_retry") < block.index(
        "self._mark_qr_login_required("
    )


def test_success_path_clears_qr_note():
    """扫码登录成功后必须清掉面板告警，否则陈旧提示会长期挂着。"""
    src = open('xianyu_token_mixins.py', encoding='utf-8').read()

    assert '_clear_qr_login_required()' in src
    assert src.index("self.last_token_refresh_error_message = None") < src.index('_clear_qr_login_required()')
