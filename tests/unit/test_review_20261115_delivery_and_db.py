"""2026-11-15 第二轮 review 回归：重复发货、迁移可重入、残留事务、越权登录、告警降噪。

五处缺陷的共同后果是"看起来一切正常，实际已经在损失"：

1. `[记得及时发货]` 这类提醒会在发货完成很久后重复到达，而内存冷却(10 分钟)
   和进程内 `delivery_sent_orders` 在重启后都失效；`_get_pending_delivery_finalization_meta`
   只认 'sent'，'finalized' 返回 None，于是同一个已完成的单元会被重新取内容再发一次
   （API 卡券等于重复下单，数据卡券等于重复占用）。
2. notification_channels 的 1.2 升级在 CREATE 与 RENAME 之间被打断后，下次启动
   重跑会撞 "table already exists"，init_db raise -> 永久启动崩溃循环。
3. 共享连接上任何一次写失败都不回滚，残留事务让批量写入的 BEGIN TRANSACTION
   直接抛错并被外层吞掉，整批同步静默变成 0 条。
4. `/password-login` 非刷新模式不校验账号归属，可覆盖他人的账号并把 user_id 改绑。
5. 未配置通知渠道时每次通知都打一条 warning，把真正重要的告警冲掉。
"""
from __future__ import annotations

import asyncio

import pytest
from loguru import logger

import xianyu_delivery_mixin
from xianyu_delivery_mixin import DeliveryMixin


# --------------------------------------------------------------------------
# 1. 防重复发货：已 finalized 的单元必须被识别出来
# --------------------------------------------------------------------------

class _DeliveryHost(DeliveryMixin):
    cookie_id = "review-1926782908"
    _safe_str = staticmethod(lambda e: str(e))


class _FakeDb:
    def __init__(self, states=None, error=None):
        self._states = states
        self._error = error
        self.calls = []

    def get_delivery_finalization_states(self, order_id):
        self.calls.append(order_id)
        if self._error:
            raise self._error
        return list(self._states or [])


def _patch_db(monkeypatch, fake):
    monkeypatch.setattr(xianyu_delivery_mixin, '_db_package', lambda: fake)


def test_finalized_units_are_collected(monkeypatch):
    fake = _FakeDb(states=[
        {'unit_index': 1, 'status': 'finalized'},
        {'unit_index': 2, 'status': 'sent'},
        {'unit_index': 3, 'status': 'finalized'},
        {'unit_index': 4, 'status': 'failed'},
    ])
    _patch_db(monkeypatch, fake)

    assert _DeliveryHost()._get_finalized_delivery_unit_indexes('order-1') == {1, 3}


def test_finalized_units_fail_open_when_db_errors(monkeypatch):
    """读库失败必须放行：数据库抖动不能反过来拦住真实发货。"""
    _patch_db(monkeypatch, _FakeDb(error=RuntimeError('database is locked')))

    assert _DeliveryHost()._get_finalized_delivery_unit_indexes('order-1') == set()


def test_no_order_id_does_not_touch_db(monkeypatch):
    fake = _FakeDb(states=[{'unit_index': 1, 'status': 'finalized'}])
    _patch_db(monkeypatch, fake)

    assert _DeliveryHost()._get_finalized_delivery_unit_indexes('') == set()
    assert fake.calls == []


@pytest.mark.parametrize(
    'finalized,quantity,expected',
    [
        ({1}, 1, True),
        ({1, 2, 3}, 3, True),
        ({1, 2}, 3, False),
        (set(), 1, False),
        ({1}, 3, False),
        ({1}, 0, True),        # 数量异常时按 1 处理
        ({1}, None, True),
    ],
)
def test_all_finalized_decision(finalized, quantity, expected):
    assert _DeliveryHost()._delivery_units_all_finalized(finalized, quantity) is expected


# --------------------------------------------------------------------------
# 2. 迁移可重入：残留临时表不能让服务起不来
# --------------------------------------------------------------------------

def test_notification_channel_type_migration_survives_leftover_temp_table():
    from db_manager import DBManager

    db = DBManager(db_path=":memory:")
    with db.lock:
        cur = db.conn.cursor()
        cur.execute("DROP TABLE IF EXISTS notification_channels")
        cur.execute('''
            CREATE TABLE notification_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('qq','ding_talk')),
                config TEXT NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cur.execute(
            "INSERT INTO notification_channels (name, user_id, type, config) VALUES ('c', 1, 'ding_talk', '{}')"
        )
        # 模拟"上次升级在 CREATE 与 RENAME 之间被打断"
        cur.execute("CREATE TABLE notification_channels_new (id INTEGER PRIMARY KEY AUTOINCREMENT, leftover TEXT)")
        db.conn.commit()

        assert db.upgrade_notification_channels_types(cur) is True
        db.conn.commit()

        types = [row[0] for row in cur.execute("SELECT type FROM notification_channels").fetchall()]
        assert types == ['dingtalk']
        leftovers = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='notification_channels_new'"
        ).fetchall()
        assert leftovers == []


# --------------------------------------------------------------------------
# 3. 残留事务：批量写入必须能重新开始
# --------------------------------------------------------------------------

def test_begin_transaction_recovers_from_stray_transaction():
    from db_manager import DBManager

    db = DBManager(db_path=":memory:")
    cur = db.conn.cursor()
    with pytest.raises(Exception):
        # CHECK 约束失败：隐式事务已经开始但没有回滚（旧写方法只 return False）
        cur.execute(
            "INSERT INTO notification_channels (name, user_id, type, config) "
            "VALUES ('bad', 1, 'not_a_channel', '{}')"
        )
    assert db.conn.in_transaction is True

    db._begin_transaction(cur)  # 旧写法: cannot start a transaction within a transaction

    cur.execute(
        "INSERT INTO notification_channels (name, user_id, type, config) VALUES ('ok', 1, 'qq', '{}')"
    )
    db.conn.commit()
    assert db.conn.in_transaction is False
    assert cur.execute("SELECT COUNT(*) FROM notification_channels").fetchone()[0] == 1


# --------------------------------------------------------------------------
# 4. 越权登录：非刷新模式也要校验归属
# --------------------------------------------------------------------------

def test_password_login_account_ownership(monkeypatch):
    from app.api.routers import accountlogin

    class _Db:
        rows = {
            'owned-by-other': {'user_id': 3},
            'owned-by-me': {'user_id': 2},
            'legacy-null-owner': {'user_id': None},
        }

        def get_cookie_details(self, account_id):
            return self.rows.get(account_id)

    monkeypatch.setattr(accountlogin.db_manager, 'db_manager', _Db())

    assert accountlogin._password_login_account_not_owned('owned-by-other', 2) is True
    assert accountlogin._password_login_account_not_owned('owned-by-me', 2) is False
    assert accountlogin._password_login_account_not_owned('legacy-null-owner', 2) is False
    # 未入库 = 新增账号，必须放行
    assert accountlogin._password_login_account_not_owned('brand-new-account', 2) is False
    assert accountlogin._password_login_account_not_owned('', 2) is False


# --------------------------------------------------------------------------
# 5. 未配置通知渠道的告警限频
# --------------------------------------------------------------------------

def test_missing_notification_channel_warns_once_per_window():
    from utils import notification_dispatcher as nd

    account_id = f"no-channel-{id(object())}"
    nd._NO_CHANNEL_WARNED_AT.pop(account_id, None)

    records = []
    sink_id = logger.add(lambda msg: records.append(msg.record), level="DEBUG")
    try:
        assert asyncio.run(nd.dispatch_account_notifications(account_id, '第一条')) is False
        assert asyncio.run(nd.dispatch_account_notifications(account_id, '第二条')) is False
    finally:
        logger.remove(sink_id)

    warnings = [
        r for r in records
        if r['level'].name == 'WARNING' and '未配置消息通知' in r['message'] and account_id in r['message']
    ]
    debug_msgs = [
        r for r in records
        if r['level'].name == 'DEBUG' and '未配置消息通知' in r['message'] and account_id in r['message']
    ]
    assert len(warnings) == 1, f"同一账号同一窗口内只应告警一次: {[w['message'] for w in warnings]}"
    assert len(debug_msgs) == 1
    nd._NO_CHANNEL_WARNED_AT.pop(account_id, None)
