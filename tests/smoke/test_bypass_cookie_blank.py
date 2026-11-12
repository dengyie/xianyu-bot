"""发货旁路模块（确认发货/免运费）的 Set-Cookie 合并：空值不能覆盖关键会话字段。

两个旁路类与主链写的是同一账号同一份 Cookie，空值分流共用
utils.xianyu_utils.split_blank_set_cookie_updates，这里同时锁定纯函数与两个类。
"""

import asyncio

import pytest

from secure_confirm_decrypted import SecureConfirm
from secure_freeshipping_decrypted import SecureFreeshipping
from utils.xianyu_utils import split_blank_set_cookie_updates


def test_split_blank_set_cookie_updates_pure_function():
    existing = {"unb": "123", "track": "t"}
    protected = ("unb", "cookie2")

    writes, removals, dropped = split_blank_set_cookie_updates(
        {"unb": "", "track": "", "cookie2": "new"}, existing, protected
    )

    assert writes == {"cookie2": "new"}
    assert removals == ["track"]
    assert dropped == ["unb"]


def test_split_blank_set_cookie_updates_protected_missing_allows_removal():
    """关键字段本来就没有值时，空值不该被「保护」，走删除。"""
    writes, removals, dropped = split_blank_set_cookie_updates(
        {"unb": "  "}, {}, ("unb",)
    )

    assert writes == {}
    assert removals == ["unb"]
    assert dropped == []


@pytest.fixture
def confirm(monkeypatch):
    inst = SecureConfirm(
        session=None,
        cookies_str="unb=1926782908; cookie2=abc123; _m_h5_tk=tok_123; track=1",
        cookie_id="unit-test",
        main_instance=None,
    )

    async def _noop():
        return None

    monkeypatch.setattr(inst, "_update_config_cookies", _noop)
    return inst


@pytest.fixture
def freeshipping(monkeypatch):
    inst = SecureFreeshipping(
        session=None,
        cookies_str="unb=1926782908; cookie2=abc123; track=1",
        cookie_id="unit-test",
    )

    async def _noop():
        return None

    monkeypatch.setattr(inst, "update_config_cookies", _noop)
    return inst


def _apply(inst, set_cookie_values):
    headers = {"set-cookie": set_cookie_values}
    return asyncio.run(inst._apply_response_cookie_updates(headers))


def test_blank_protected_field_keeps_existing_session_value(confirm):
    changed = _apply(confirm, ["unb=; Max-Age=0; Path=/"])

    assert changed is False
    assert confirm.cookies.get("unb") == "1926782908"
    assert "unb=1926782908" in confirm.cookies_str


def test_blank_unprotected_field_is_removed_not_written_empty(confirm):
    changed = _apply(confirm, ["track=; Max-Age=0; Path=/"])

    assert changed is True
    assert "track" not in confirm.cookies
    assert "track=" not in confirm.cookies_str


def test_non_empty_update_still_applies(confirm):
    changed = _apply(confirm, ["_m_h5_tk=tok_456; Path=/"])

    assert changed is True
    assert confirm.cookies.get("_m_h5_tk") == "tok_456"


def test_mixed_updates_only_touch_what_should_change(confirm):
    changed = _apply(confirm, ["unb=; Max-Age=0", "track=; Max-Age=0", "cookie2=new456"])

    assert changed is True
    assert confirm.cookies.get("unb") == "1926782908"  # 关键字段保留
    assert "track" not in confirm.cookies              # 非关键空值删除
    assert confirm.cookies.get("cookie2") == "new456"  # 正常更新


def test_freeshipping_blank_protected_field_keeps_value(freeshipping):
    changed = _apply(freeshipping, ["unb=; Max-Age=0; Path=/", "cookie2="])

    assert changed is False
    assert freeshipping.cookies.get("unb") == "1926782908"
    assert freeshipping.cookies.get("cookie2") == "abc123"


def test_freeshipping_blank_unprotected_field_removed(freeshipping):
    changed = _apply(freeshipping, ["track=; Max-Age=0; Path=/"])

    assert changed is True
    assert "track" not in freeshipping.cookies
