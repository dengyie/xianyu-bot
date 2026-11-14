"""2026-11-15 深度 review 回归：后台任务/心跳的"静默死亡"必须有收口。

- `_create_tracked_task` 只 discard 不取 exception：任务崩溃只会在回收时由
  asyncio 打一句没有账号上下文的 "never retrieved"。
- `heartbeat_loop` 连续 3 次发送失败就 break，而业务流看门狗在心跳过期时直接
  continue —— 两者叠加会留下一条"连接还在、心跳已死、看门狗不管"的死连接。
- Cookie 写库失败时 `update_cookie_account_info` 吞异常只返回 False，原先只有
  except 分支发通知，等于永不通知。
"""
from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from XianyuAutoAsync import XianyuLive
import xianyu_cookie_mixin


def _capture():
    records = []
    sink_id = logger.add(lambda msg: records.append(msg.record), level="DEBUG")
    return records, sink_id


def _messages(records, needle):
    return [r["message"] for r in records if needle in r["message"]]


def _live_stub(**attrs):
    host = XianyuLive.__new__(XianyuLive)
    host.cookie_id = "review-1926782908"
    host.background_tasks = set()
    host.heartbeat_task = None
    host.ws = None
    for key, value in attrs.items():
        setattr(host, key, value)
    return host


# --------------------------------------------------------------------------
# tracked task 异常必须带账号上下文落日志
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tracked_task_exception_is_logged_with_cookie_id():
    host = _live_stub()
    records, sink_id = _capture()
    try:
        async def _boom():
            raise RuntimeError("模拟后台任务崩溃")

        host._create_tracked_task(_boom())
        await asyncio.sleep(0.05)
        await asyncio.sleep(0)
    finally:
        logger.remove(sink_id)

    hits = _messages(records, "后台任务异常退出")
    assert hits, "任务异常必须被取用并记录"
    assert "review-1926782908" in hits[0]
    assert "模拟后台任务崩溃" in hits[0]
    assert host.background_tasks == set(), "完成后仍要从追踪集合里移除"


# --------------------------------------------------------------------------
# 心跳任务退出必须触发重连
# --------------------------------------------------------------------------

def _ws(closed=False):
    return type("_Ws", (), {"closed": closed})()


@pytest.mark.asyncio
async def test_heartbeat_task_exit_forces_reconnect():
    scheduled = []
    host = _live_stub(ws=_ws(closed=False))
    host._create_tracked_task = lambda coro: scheduled.append(coro)
    host._force_websocket_reconnect = lambda reason: reason

    async def _exited():
        return None

    task = asyncio.create_task(_exited())
    host.heartbeat_task = task
    await task
    host._on_heartbeat_task_done(task)

    assert scheduled, "心跳任务无人接管时必须排一次强制重连"


@pytest.mark.asyncio
async def test_heartbeat_task_cancel_does_not_reconnect():
    scheduled = []
    host = _live_stub(ws=_ws(closed=False))
    host._create_tracked_task = lambda coro: scheduled.append(coro)

    task = asyncio.create_task(asyncio.sleep(30))
    host.heartbeat_task = task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    host._on_heartbeat_task_done(task)

    assert scheduled == [], "正常取消(重连/停机)不该再补一次重连"


@pytest.mark.asyncio
async def test_heartbeat_task_exit_with_closed_ws_does_not_reconnect():
    scheduled = []
    host = _live_stub(ws=_ws(closed=True))
    host._create_tracked_task = lambda coro: scheduled.append(coro)

    async def _exited():
        return None

    task = asyncio.create_task(_exited())
    host.heartbeat_task = task
    await task
    host._on_heartbeat_task_done(task)

    assert scheduled == [], "主循环正在处理断开时不要重复触发"


# --------------------------------------------------------------------------
# Cookie 写库失败必须通知
# --------------------------------------------------------------------------

class _CookieHost:
    cookie_id = "review-1926782908"
    user_id = "1926782908"
    cookies_str = "unb=1; cookie2=2"
    _safe_str = staticmethod(lambda e: str(e))

    def __init__(self, success):
        self.success = success
        self.notifications = []

    async def send_token_refresh_notification(self, message, notification_type):
        self.notifications.append((notification_type, message))
        return True


@pytest.mark.asyncio
async def test_cookie_db_write_failure_sends_notification(monkeypatch):
    host = _CookieHost(success=False)

    class _Db:
        def update_cookie_account_info(self, *args, **kwargs):
            return False

    monkeypatch.setattr(xianyu_cookie_mixin, '_db_package', lambda: _Db())

    await xianyu_cookie_mixin.CookieMixin.update_config_cookies(host)

    types = [kind for kind, _ in host.notifications]
    assert 'db_update_failed' in types, f"写库失败必须通知,实际: {host.notifications}"


@pytest.mark.asyncio
async def test_cookie_db_write_success_stays_quiet(monkeypatch):
    host = _CookieHost(success=True)

    class _Db:
        def update_cookie_account_info(self, *args, **kwargs):
            return True

    monkeypatch.setattr(xianyu_cookie_mixin, '_db_package', lambda: _Db())

    await xianyu_cookie_mixin.CookieMixin.update_config_cookies(host)

    assert host.notifications == []


@pytest.mark.asyncio
async def test_heartbeat_task_exit_with_disabled_account_does_not_reconnect():
    """账号被禁用是心跳循环的正常退出条件，重连只会变成空转。"""
    scheduled = []
    mgr = type("_Mgr", (), {"get_cookie_status": lambda self, cid: False})()
    host = _live_stub(ws=_ws(closed=False), _cookie_mgr=mgr)
    host._create_tracked_task = lambda coro: scheduled.append(coro)

    async def _exited():
        return None

    task = asyncio.create_task(_exited())
    host.heartbeat_task = task
    await task
    host._on_heartbeat_task_done(task)

    assert scheduled == []


@pytest.mark.asyncio
async def test_start_heartbeat_task_attaches_supervision_callback():
    """三个心跳启动点(首次连接、Cookie 刷新前后)必须共用同一个带收口的入口。"""
    host = _live_stub(ws=_ws(closed=True))

    async def _fake_loop(ws):
        return None

    host.heartbeat_loop = _fake_loop
    task = host._start_heartbeat_task(host.ws)

    assert host.heartbeat_task is task
    assert task._callbacks, "心跳任务必须挂上退出收口，否则异常退出无人接手"
    await task
