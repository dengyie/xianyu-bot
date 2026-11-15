"""sid 兜底查单必须按商品/买家过滤候选订单。

背景：买家「我已付款，等待你发货」的提醒消息常常不含 order_id，只能靠 sid
回查。同一个 sid 下买家同时挂着两笔待发货订单时，不过滤就只会取
updated_at 最新的一笔；调用方随后用商品一致性校验把它拒掉（return），
另一笔该发的订单就这么被静默跳过，买家永远收不到货。

修复原则：过滤优先，未命中再回退到仅按 sid 查询——不比改动前更差。
"""
from __future__ import annotations

import pytest

import xianyu_trading_mixins
from xianyu_trading_mixins import OrderMixin


class _Host(OrderMixin):
    cookie_id = 'c1'
    myid = '999'

    @staticmethod
    def _safe_str(error):
        return str(error)

    @staticmethod
    def _is_trustworthy_buyer_id(buyer_id):
        value = str(buyer_id or '').strip()
        return bool(value) and value not in {'0', 'unknown', 'None'}

    def _has_delivery_progress_evidence(self, order_id):
        return True


class _FakeDb:
    """按是否带过滤条件返回不同结果，并记录每一次查询参数。"""

    def __init__(self, filtered_rows=None, unfiltered_rows=None):
        self.filtered_rows = list(filtered_rows or [])
        self.unfiltered_rows = list(unfiltered_rows or [])
        self.calls = []

    def find_recent_orders_by_match_context(self, **kwargs):
        self.calls.append(kwargs)
        if 'item_id' in kwargs or 'buyer_id' in kwargs:
            return list(self.filtered_rows)
        return list(self.unfiltered_rows)


def _patch(monkeypatch, fake):
    monkeypatch.setattr(xianyu_trading_mixins, '_db_host', lambda: fake)


def _order(order_id, item_id='111', buyer_id='buyer1', status='pending_ship'):
    return {
        'order_id': order_id,
        'item_id': item_id,
        'buyer_id': buyer_id,
        'order_status': status,
    }


def test_filters_by_item_and_buyer_before_picking(monkeypatch):
    fake = _FakeDb(
        filtered_rows=[_order('correct')],
        unfiltered_rows=[_order('wrong-item', item_id='222')],
    )
    _patch(monkeypatch, fake)

    result = _Host()._lookup_delivery_order_by_sid(
        'sid1', item_id='111', buyer_id='buyer1'
    )

    assert result['match_type'] == 'pending_ship'
    assert result['order']['order_id'] == 'correct'
    assert len(fake.calls) == 1, '过滤命中时不应该再做回退查询'
    assert fake.calls[0]['item_id'] == '111'
    assert fake.calls[0]['buyer_id'] == 'buyer1'


def test_falls_back_to_sid_only_when_filters_miss(monkeypatch):
    """过滤没命中必须回退：DB 里的 item_id/buyer_id 可能是空的历史数据。"""
    fake = _FakeDb(filtered_rows=[], unfiltered_rows=[_order('legacy', item_id=None)])
    _patch(monkeypatch, fake)

    result = _Host()._lookup_delivery_order_by_sid(
        'sid1', item_id='111', buyer_id='buyer1'
    )

    assert result['order']['order_id'] == 'legacy'
    assert len(fake.calls) == 2
    assert fake.calls[0].get('item_id') == '111'
    assert 'item_id' not in fake.calls[1]
    assert 'buyer_id' not in fake.calls[1]


@pytest.mark.parametrize('placeholder', ['', None, '未知商品', '0', 'null', 'None'])
def test_placeholder_item_id_is_not_used_as_filter(monkeypatch, placeholder):
    fake = _FakeDb(unfiltered_rows=[_order('any')])
    _patch(monkeypatch, fake)

    _Host()._lookup_delivery_order_by_sid('sid1', item_id=placeholder)

    assert len(fake.calls) == 1
    assert 'item_id' not in fake.calls[0]


@pytest.mark.parametrize('buyer_id', ['', None, '999', '0', 'unknown'])
def test_unusable_buyer_id_is_not_used_as_filter(monkeypatch, buyer_id):
    fake = _FakeDb(unfiltered_rows=[_order('any')])
    _patch(monkeypatch, fake)

    _Host()._lookup_delivery_order_by_sid('sid1', buyer_id=buyer_id)

    assert len(fake.calls) == 1
    assert 'buyer_id' not in fake.calls[0]


def test_without_context_behaviour_is_unchanged(monkeypatch):
    fake = _FakeDb(unfiltered_rows=[_order('only')])
    _patch(monkeypatch, fake)

    result = _Host()._lookup_delivery_order_by_sid('sid1')

    assert result['order']['order_id'] == 'only'
    assert len(fake.calls) == 1
    assert fake.calls[0]['sid'] == 'sid1'


def test_empty_sid_never_queries(monkeypatch):
    fake = _FakeDb()
    _patch(monkeypatch, fake)

    assert _Host()._lookup_delivery_order_by_sid('')['match_type'] == 'missing'
    assert fake.calls == []
