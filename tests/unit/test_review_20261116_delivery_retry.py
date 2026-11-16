"""2026-11-16 review：部分发货自动补发扫描 + 相关回归。

覆盖：
- DB `get_stuck_delivery_orders` 的状态/时效/单元记录/账号过滤；
- `_sweep_stuck_delivery_orders` 的准入、幂等复用与重试节流；
- `reset_password_login_backoff_timer` 只清计时、保留连续风控计数；
- `_complete_delivery_unit_after_terminal_confirm` 的 finalized 落盘与成功日志。
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections import defaultdict
from unittest import mock

import pytest

import xianyu_auth_recovery
import xianyu_delivery_mixin
from db_manager.orders import DBOrdersMixin
from xianyu_delivery_mixin import DeliveryMixin
from xianyu_messaging_mixins import SendMixin


# --------------------------------------------------------------------------
# 轻量 DB host：只装配 get_stuck_delivery_orders 需要的成员
# --------------------------------------------------------------------------
class _DbHost(DBOrdersMixin):
    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.conn.executescript('''
            CREATE TABLE orders (
                order_id TEXT PRIMARY KEY,
                cookie_id TEXT,
                item_id TEXT,
                buyer_id TEXT,
                buyer_nick TEXT,
                sid TEXT,
                quantity INTEGER,
                order_status TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE delivery_finalization_states (
                order_id TEXT NOT NULL,
                unit_index INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                updated_at TEXT
            );
        ''')

    @staticmethod
    def _normalize_order_status(status):
        return (status or '').strip()

    @staticmethod
    def _sanitize_order_buyer_nick(nick):
        return nick

    def add_order(self, order_id, *, cookie_id='ck-1', status='partial_success',
                  age_minutes=30, with_state=True, item_id='item-1',
                  buyer_id='buyer-1', sid='sid-1@goofish', quantity=2):
        # orders.updated_at 由 CURRENT_TIMESTAMP 写入，是 UTC；测试必须同样用 UTC
        updated_at = time.strftime(
            '%Y-%m-%d %H:%M:%S', time.gmtime(time.time() - age_minutes * 60)
        )
        self.conn.execute(
            'INSERT INTO orders (order_id, cookie_id, item_id, buyer_id, buyer_nick, sid,'
            ' quantity, order_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (order_id, cookie_id, item_id, buyer_id, 'nick', sid, quantity,
             status, updated_at, updated_at),
        )
        if with_state:
            self.conn.execute(
                "INSERT INTO delivery_finalization_states (order_id, unit_index, status, updated_at)"
                " VALUES (?, 1, 'sent', ?)",
                (order_id, updated_at),
            )
        self.conn.commit()


def test_stuck_order_query_filters_status_age_state_and_account():
    host = _DbHost()
    host.add_order('hit', status='partial_success', age_minutes=30)
    host.add_order('hit-pending-finalize', status='partial_pending_finalize', age_minutes=30)
    host.add_order('too-new', status='partial_success', age_minutes=0)
    host.add_order('wrong-status', status='pending_ship', age_minutes=30)
    host.add_order('no-unit-state', status='partial_success', age_minutes=30, with_state=False)
    host.add_order('other-cookie', status='partial_success', age_minutes=30, cookie_id='ck-2')

    rows = host.get_stuck_delivery_orders('ck-1', older_than_minutes=10, limit=10)
    assert {r['order_id'] for r in rows} == {'hit', 'hit-pending-finalize'}
    assert rows[0]['sid'] == 'sid-1@goofish'


def test_stuck_order_query_honors_limit_and_sorts_oldest_first():
    host = _DbHost()
    host.add_order('newer', age_minutes=30)
    host.add_order('oldest', age_minutes=120)

    rows = host.get_stuck_delivery_orders('ck-1', older_than_minutes=10, limit=1)
    assert [r['order_id'] for r in rows] == ['oldest']


# --------------------------------------------------------------------------
# 扫描器
# --------------------------------------------------------------------------
class _SweepHost(DeliveryMixin):
    cookie_id = 'sweep-ck'
    _safe_str = staticmethod(lambda e: str(e))
    delivery_cooldown = 600
    last_delivery_time = {}

    def __init__(self, orders, *, ws=True, auto_confirm=True,
                 expected_quantity=2, finalized_units=None,
                 locked_orders=(), cooling_orders=()):
        self._orders = orders
        self.ws = object() if ws is True else ws
        self._auto_confirm = auto_confirm
        self._expected_quantity = expected_quantity
        self._finalized_units = set(finalized_units or [])
        self._handle_auto_delivery = mock.AsyncMock()
        self._delivery_retry_attempts = {}
        self._locked_orders = set(locked_orders)
        self.last_delivery_time = {
            oid: time.time() for oid in cooling_orders
        }

    def is_lock_held(self, order_id):
        return order_id in self._locked_orders

    def is_auto_confirm_enabled(self):
        return self._auto_confirm

    def _get_order_expected_delivery_quantity(self, order_id):
        return self._expected_quantity

    def _get_finalized_delivery_unit_indexes(self, order_id):
        return set(self._finalized_units)


def _patch_db(monkeypatch, rows):
    fake = mock.Mock()
    fake.get_stuck_delivery_orders.return_value = list(rows)
    monkeypatch.setattr(xianyu_delivery_mixin, '_db_package', lambda: fake)
    return fake


def _row(order_id='order-1', item_id='item-1', buyer_id='buyer-1', sid='sid-1@goofish'):
    return {
        'order_id': order_id,
        'item_id': item_id,
        'buyer_id': buyer_id,
        'buyer_nick': '买家昵称',
        'sid': sid,
        'quantity': 2,
        'order_status': 'partial_success',
    }


@pytest.mark.asyncio
async def test_sweep_requires_websocket_and_auto_confirm(monkeypatch):
    fake = _patch_db(monkeypatch, [_row()])

    no_ws = _SweepHost([], ws=None)
    assert await no_ws._sweep_stuck_delivery_orders() == 0

    disabled = _SweepHost([], auto_confirm=False)
    assert await disabled._sweep_stuck_delivery_orders() == 0
    fake.get_stuck_delivery_orders.assert_not_called()


class _ClosedWs:
    closed = True


@pytest.mark.asyncio
async def test_sweep_skips_closed_websocket_without_burning_attempts(monkeypatch):
    fake = _patch_db(monkeypatch, [_row()])
    host = _SweepHost([], ws=_ClosedWs(), expected_quantity=2, finalized_units={1})

    assert await host._sweep_stuck_delivery_orders() == 0
    # 已关闭的连接不能再被当作可发送通道：既不查库也不消耗重试额度
    fake.get_stuck_delivery_orders.assert_not_called()
    assert host._delivery_retry_attempts == {}
    host._handle_auto_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_reuses_idempotent_handler_with_order_id(monkeypatch):
    _patch_db(monkeypatch, [_row()])
    host = _SweepHost([], expected_quantity=2, finalized_units={1})

    resumed = await host._sweep_stuck_delivery_orders()

    assert resumed == 1
    host._handle_auto_delivery.assert_awaited_once()
    args, kwargs = host._handle_auto_delivery.await_args
    assert kwargs['order_id'] == 'order-1'
    # chat_id 用去掉 @goofish 后缀的 sid，与实时消息路径一致
    assert args[5] == 'sid-1'
    assert host._delivery_retry_attempts['order-1']['count'] == 1


@pytest.mark.asyncio
async def test_sweep_skips_missing_sid_and_all_finalized(monkeypatch):
    _patch_db(monkeypatch, [
        _row(order_id='no-sid', sid=''),
        _row(order_id='done'),
    ])
    host = _SweepHost([], expected_quantity=2, finalized_units={1, 2})
    host._delivery_retry_attempts['done'] = {'count': 2, 'last_ts': 0.0}

    assert await host._sweep_stuck_delivery_orders() == 0
    host._handle_auto_delivery.assert_not_awaited()
    # 已完成的订单要释放内存中的重试计数
    assert 'done' not in host._delivery_retry_attempts


@pytest.mark.asyncio
async def test_sweep_caps_attempts_and_spaces_retries(monkeypatch):
    _patch_db(monkeypatch, [_row(order_id='capped'), _row(order_id='cooling')])
    host = _SweepHost([], expected_quantity=2, finalized_units={1})
    now = time.time()
    host._delivery_retry_attempts = {
        'capped': {'count': host._delivery_retry_max_attempts, 'last_ts': 0.0},
        'cooling': {'count': 1, 'last_ts': now - 60},
    }

    assert await host._sweep_stuck_delivery_orders() == 0
    host._handle_auto_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_does_not_burn_attempts_on_lock_or_cooldown(monkeypatch):
    """冷却/延迟锁是正常节奏，不该消耗补发额度（否则真实补发会少一次机会）。"""
    _patch_db(monkeypatch, [
        _row(order_id='locked'),
        _row(order_id='cooling'),
        _row(order_id='ready'),
    ])
    host = _SweepHost(
        [], expected_quantity=2, finalized_units={1},
        locked_orders={'locked'}, cooling_orders={'cooling'},
    )

    assert await host._sweep_stuck_delivery_orders() == 1
    assert 'locked' not in host._delivery_retry_attempts
    assert 'cooling' not in host._delivery_retry_attempts
    assert host._delivery_retry_attempts['ready']['count'] == 1
    args, kwargs = host._handle_auto_delivery.await_args
    assert kwargs['order_id'] == 'ready'


@pytest.mark.asyncio
async def test_sweep_prunes_orders_no_longer_candidates(monkeypatch):
    _patch_db(monkeypatch, [_row(order_id='current')])
    host = _SweepHost([], expected_quantity=2, finalized_units={1})
    host._delivery_retry_attempts = {'gone': {'count': 1, 'last_ts': time.time()}}

    assert await host._sweep_stuck_delivery_orders() == 1
    assert 'gone' not in host._delivery_retry_attempts
    assert 'current' in host._delivery_retry_attempts


# --------------------------------------------------------------------------
# 退避计时重置
# --------------------------------------------------------------------------
class _BackoffHost(xianyu_auth_recovery.XianyuAuthRecoveryMixin):
    cookie_id = 'backoff-ck'
    # 真实项目里该字典声明在组合类 XianyuAutoAsync 上，classmethod 通过 cls 访问
    _password_login_failure_backoff = {}


def test_reset_backoff_timer_keeps_consecutive_count():
    cls = _BackoffHost
    cls._password_login_failure_backoff.clear()
    cls.set_password_login_failure_backoff('backoff-ck', 'slider_failed', 300)
    before = cls.get_password_login_failure_backoff('backoff-ck')
    assert before['consecutive_count'] == 1

    host = _BackoffHost()
    host.reset_password_login_backoff_timer('backoff-ck')

    after = cls.get_password_login_failure_backoff('backoff-ck')
    assert after['consecutive_count'] == 1, '滑块通过不能抹掉连续风控失败计数'
    assert after['same_reason_count'] == 0, '同因连击应从基础时长重新开始'
    assert after['seconds'] == 0
    # until 记的是「已到期」的当前时刻，不能写 0（否则会被保留窗口判定为陈账丢弃）
    assert after['until'] > 0
    assert host._get_active_password_login_failure_backoff() is None
    cls._password_login_failure_backoff.clear()


def test_reset_backoff_timer_is_noop_without_state():
    cls = _BackoffHost
    cls._password_login_failure_backoff.clear()
    _BackoffHost().reset_password_login_backoff_timer('missing-ck')
    assert 'missing-ck' not in cls._password_login_failure_backoff


# --------------------------------------------------------------------------
# 终态确认收尾
# --------------------------------------------------------------------------
class _CompleteHost(DeliveryMixin):
    cookie_id = 'complete-ck'

    def __init__(self):
        self.persisted = None
        self.logged = None

    def _persist_delivery_finalization_state(self, **kwargs):
        self.persisted = kwargs

    def _record_delivery_log(self, **kwargs):
        self.logged = kwargs


def test_terminal_confirm_completes_unit():
    host = _CompleteHost()
    host._complete_delivery_unit_after_terminal_confirm(
        order_id='order-1',
        item_id='item-1',
        buyer_id='buyer-1',
        delivery_meta={'success': True},
        delivery_unit_index=2,
        error='ORDER_STATUS_ERROR: 订单状态不正确',
        buyer_nick='买家昵称',
    )

    assert host.persisted['status'] == 'finalized'
    assert host.persisted['last_error'] == 'ORDER_STATUS_ERROR: 订单状态不正确'
    assert host.persisted['delivery_meta']['delivery_unit_index'] == 2
    assert host.logged['status'] == 'success'
    assert host.logged['buyer_nick'] == '买家昵称'
    assert '平台订单已处于终态' in host.logged['reason']


# --------------------------------------------------------------------------
# 无会话补偿发货：发送失败 vs 发送后失败 的状态落盘口径
# --------------------------------------------------------------------------
class _RecoveredHost(SendMixin):
    """只装配 `_send_recovered_delivery_without_sid` 需要的最小依赖。"""

    cookie_id = 'recovered-ck'
    _safe_str = staticmethod(lambda e: str(e))

    def __init__(self, *, send_error=None, finalize_error=None):
        self._order_locks = defaultdict(asyncio.Lock)
        self._lock_usage_times = {}
        self._send_error = send_error
        self._finalize_error = finalize_error
        self.sent = []
        self.persisted = []
        self.logs = []
        self.released = []

    def can_auto_delivery(self, order_id):
        return True

    def is_lock_held(self, order_id):
        return False

    def _get_pending_delivery_finalization_meta(self, order_id, unit_index=1):
        return None

    async def _auto_delivery(self, item_id, item_title=None, order_id=None,
                             send_user_id=None, chat_id=None, include_meta=False, **_kwargs):
        return {
            'success': True,
            'content': 'RECOVERED-CARD',
            'delivery_steps': ['RECOVERED-CARD'],
            'rule_id': 1,
            'card_type': 'text',
            'delivery_unit_index': 1,
        }

    def _build_delivery_steps(self, content, description=''):
        return [content]

    async def send_delivery_steps_once(self, buyer_id, item_id, delivery_steps):
        if self._send_error:
            raise RuntimeError(self._send_error)
        self.sent.append((buyer_id, item_id, list(delivery_steps)))

    def _mark_data_reservation_sent_if_needed(self, delivery_meta):
        return True

    def _release_data_reservation_if_needed(self, delivery_meta, error=None):
        self.released.append({'error': error})
        return True

    def _persist_delivery_finalization_state(self, *, order_id, item_id, buyer_id,
                                             delivery_meta, channel, status,
                                             last_error=None):
        self.persisted.append({'status': status, 'last_error': last_error})
        return True

    async def _finalize_delivery_after_send(self, delivery_meta=None, order_id=None, item_id=None):
        if self._finalize_error:
            raise RuntimeError(self._finalize_error)
        return {'success': True}

    def _sync_order_delivery_progress(self, **kwargs):
        return {'aggregate_status': 'shipped'}

    def _activate_delivery_lock(self, lock_key, delay_minutes=10):
        return None

    def _record_delivery_log(self, **kwargs):
        self.logs.append(kwargs)

    async def _run(self, order_id):
        return await self._send_recovered_delivery_without_sid(
            {'quantity': 1},
            order_id=order_id,
            item_id='item-1',
            buyer_id='buyer-1',
            source='测试补偿',
        )


@pytest.mark.asyncio
async def test_recovered_delivery_post_send_failure_records_sent_not_failed():
    """卡券已发出后的收尾异常必须记 sent——记 failed 会让重试重发同一张卡。"""
    host = _RecoveredHost(finalize_error='auto_confirm blew up')

    ok = await host._run('order-post-send')

    assert ok is False
    assert host.sent, '卡券应当已经发出'
    statuses = [p['status'] for p in host.persisted]
    assert 'failed' not in statuses, f'发送后失败不能落 failed: {statuses}'
    assert host.persisted[-1]['status'] == 'sent'
    assert host.persisted[-1]['last_error'] == 'auto_confirm blew up'
    assert host.released == [], '卡券已发出，不应释放数据预占'


@pytest.mark.asyncio
async def test_recovered_delivery_send_failure_records_failed():
    """发送本身失败时卡券没出去，必须记 failed 以便重试补发。"""
    host = _RecoveredHost(send_error='websocket closed')

    ok = await host._run('order-send-fail')

    assert ok is False
    assert host.sent == []
    assert host.persisted[-1]['status'] == 'failed'
    assert host.persisted[-1]['last_error'] == 'websocket closed'
    assert len(host.released) == 1


@pytest.mark.asyncio
async def test_recovered_delivery_success_records_finalized():
    host = _RecoveredHost()

    ok = await host._run('order-ok')

    assert ok is True
    assert host.sent
    assert host.persisted[-1]['status'] == 'finalized'
    assert host.logs[-1]['status'] == 'success'
