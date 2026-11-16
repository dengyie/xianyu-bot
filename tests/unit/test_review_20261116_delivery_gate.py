"""2026-11-16 review：简化发货路径 finalized 闸门回归测试。

目标：验证「已 finalized 的订单在订单锁内跳出发货」逻辑，不依赖完整方法链。
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

import xianyu_delivery_mixin
import xianyu_messaging_mixins
from xianyu_delivery_mixin import DeliveryMixin
from xianyu_messaging_mixins import SendMixin


class _SimpleDeliveryHost(SendMixin, DeliveryMixin):
    cookie_id = "review-20261116"

    def __init__(self, finalized_units=None):
        self._finalized_units = set(finalized_units or [])
        self.delivery_logs = []
        self._lock_usage_times = {}
        # 只需要 stub 这几个；闸门在锁内检查，在进入锁之前已完成
        self._stub_can_auto_delivery = True
        self._stub_lock_held = False
        self._stub_item_owned = True
        self._stub_blacklist = False
        self._stub_lock_obtained = False

    @staticmethod
    def _safe_str(e):
        return str(e)

    def can_auto_delivery(self, order_id):
        return self._stub_can_auto_delivery

    def is_lock_held(self, lock_key):
        return self._stub_lock_held

    async def _ensure_item_owned_by_current_account(self, item_id, log_prefix=''):
        return self._stub_item_owned

    def _check_buyer_blacklist_for_action(self, **kwargs):
        return self._stub_blacklist

    def _record_delivery_log(self, **kwargs):
        self.delivery_logs.append(kwargs)

    def _get_finalized_delivery_unit_indexes(self, order_id):
        return set(self._finalized_units)

    # _handle_simple_message_auto_delivery 会 _order_locks[order_id] 拿锁，
    # 用 __getitem__ mock 让它拿到一个 mock 对象；async with 时 mock 需支持 __aenter__
    @property
    def _order_locks(self):
        m = mock.MagicMock()
        ctx = mock.MagicMock()
        ctx.__aenter__ = mock.AsyncMock()
        ctx.__aexit__ = mock.AsyncMock()
        m.__getitem__ = mock.Mock(return_value=ctx)
        return m

    def _release_data_reservation_if_needed(self, *a, **k):
        pass


async def _call_simplified(order_id, finalized_units=None):
    host = _SimpleDeliveryHost(finalized_units=finalized_units)
    # 跳过通知回调（通知路径不测）
    with mock.patch.object(host, '_notify_buyer_about_delivery_ready', mock.AsyncMock()):
        await host._handle_simple_message_auto_delivery(
            None, order_id, 'item-1', 'buyer-1', 'chat-1', '12:00', 'msg-1'
        )
    return host


@pytest.mark.asyncio
async def test_finalized_order_is_skipped_in_simplified_path():
    """已 finalized 的订单在锁内短路，不进入 _auto_delivery。"""
    host = _SimpleDeliveryHost(finalized_units={1})
    # mock _auto_delivery 以检测是否被调用
    with mock.patch.object(host, '_auto_delivery', mock.AsyncMock()) as auto_mock:
        await host._handle_simple_message_auto_delivery(
            None, 'order-1', 'item-1', 'buyer-1', 'chat-1', '12:00', 'msg-1'
        )
    assert auto_mock.call_count == 0, '已 finalized 订单不得调用 _auto_delivery'
    assert any('已完成' in str(log.get('reason', '')) for log in host.delivery_logs), \
        '应记录 finalized 跳过日志'


@pytest.mark.asyncio
async def test_unfinalized_order_proceeds_in_simplified_path():
    """未 finalized 的订单正常进入发货流程。"""
    host = _SimpleDeliveryHost(finalized_units=set())
    with mock.patch.object(host, '_auto_delivery', mock.AsyncMock()) as auto_mock:
        await host._handle_simple_message_auto_delivery(
            None, 'order-2', 'item-1', 'buyer-1', 'chat-1', '12:00', 'msg-2'
        )
    assert auto_mock.call_count == 1, '未 finalized 订单应调用 _auto_delivery'


# ---- 对齐验证：delivery_mixin 统一路径的 finalized 闸门 ----

class _FakeDb:
    def __init__(self, states=None, error=None):
        self._states = states
        self._error = error

    def get_delivery_finalization_states(self, order_id):
        if self._error:
            raise self._error
        return list(self._states or [])


class _DeliveryHost(xianyu_delivery_mixin.DeliveryMixin):
    cookie_id = "review-20261116"
    _safe_str = staticmethod(lambda e: str(e))


def _patch_db(monkeypatch, fake):
    monkeypatch.setattr(xianyu_delivery_mixin, '_db_package', lambda: fake)


def test_delivery_mixin_finalized_gate_still_works(monkeypatch):
    """确保统一发货路径的 finalized 闸门未因改动受损。"""
    _patch_db(monkeypatch, _FakeDb(states=[
        {'unit_index': 1, 'status': 'finalized'},
        {'unit_index': 2, 'status': 'sent'},
    ]))
    assert _DeliveryHost()._get_finalized_delivery_unit_indexes('order-1') == {1}


def test_db_error_fails_open(monkeypatch):
    """读库失败时 finalized 闸门放行（不拦真实发货）。"""
    _patch_db(monkeypatch, _FakeDb(error=RuntimeError('locked')))
    assert _DeliveryHost()._get_finalized_delivery_unit_indexes('order-1') == set()
