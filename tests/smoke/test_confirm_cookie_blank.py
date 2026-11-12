"""自动确认发货链路的 Set-Cookie 合并：空值不能覆盖关键会话字段。"""

import asyncio

import pytest

from secure_confirm_decrypted import SecureConfirm


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
