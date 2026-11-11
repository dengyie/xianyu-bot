"""生产 pin 的 slidex 必须携带「指纹一致性」修复。

2026-09-19 深挖结论（运维文档「滑块硬拒绝根因深挖」节）：密码登录滑块
20+ 天 400+ 次全败（阿里 punish 页 error:hwR4mj）的根因是有头浏览器指纹
自相矛盾——userAgent=池内 Chrome/119（Windows）vs navigator.platform=真实
Linux x86_64 vs userAgentData.brands=真实内核 Chromium/147 vs
plugins=数字数组。风控 JS 读三个只读属性即零成本判自动化，轨迹再准也没用。

修复落在 slidex 6b3190a（_get_headful_stealth_script + userAgentData 一致性
+ 删除数字 plugins 注入）。本文件钉死两件事：

1. pin 只有 requirements.txt 一个事实源——Dockerfile 必须从它 grep 出来安装，
   绝不能再硬编码第二个 commit（硬编码正是 2026-11-11「改一次 pin 让 pip 层
   digest 变化、连带 600MB 浏览器层整层重建、线上跨境重下卡死」的根因）；
2. 实际安装的 slidex 必须真的带该修复——任何人把 pin 挪到没有修复的版本
   （含上游回退），这里直接失败。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

PIN_PATTERN = r"slidex @ git\+https://github\.com/dengyie/slidex\.git@([0-9a-f]+)"
GREP_PATTERN = r"grep -E '\^slidex\[\[:space:\]\]\*@' requirements\.txt"


def test_slidex_pin_only_in_requirements():
    """pin 单点事实源：requirements.txt 有，Dockerfile 不得再硬编码。"""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert re.search(PIN_PATTERN, requirements), "requirements.txt 缺少 slidex pin"
    assert not re.search(PIN_PATTERN, dockerfile), (
        "Dockerfile 又硬编码了 slidex pin：两个事实源必然漂移，且会让 pip 层"
        "随 pin 变更失效、连带浏览器层整层重建"
    )
    # 删掉硬编码后必须仍从 requirements.txt 取值，否则等于压根没装 slidex
    assert re.search(GREP_PATTERN, dockerfile), (
        "Dockerfile 未从 requirements.txt 提取 slidex pin 安装"
    )


@pytest.fixture(scope="module")
def slider():
    from slidex.stealth import XianyuSliderStealth

    return XianyuSliderStealth(user_id="ut-pin-consistency")


FEATURES = {
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    ),
    "platform": "Win32",
    "vendor": "Google Inc.",
    "locale": "zh-CN",
    "is_mobile": False,
    "viewport_width": 1920,
    "viewport_height": 1200,
}


def test_pinned_slidex_has_headful_consistency_script(slider):
    from slidex.stealth import XianyuSliderStealth

    assert hasattr(XianyuSliderStealth, "_get_headful_stealth_script"), (
        "安装的 slidex 缺少 _get_headful_stealth_script——pin 回退到了没有指纹修复的版本"
    )

    script = slider._get_headful_stealth_script(FEATURES)
    assert "Navigator.prototype, 'platform'" in script
    assert "Navigator.prototype, 'userAgentData'" in script
    assert "getHighEntropyValues" in script
    # brands 必须与 UA 池版本（119）一致，不能漏出真实内核版本
    assert '"version": "119"' in script
    # UA-CH 高层平台名必须是 Windows（真实 Chrome 从不发 "Win32"）
    assert 'platform: "Windows"' in script
    assert 'platform: "Win32"' not in script
    # webdriver 必须是 present 的 false（undefined 是缺失语义，可探测）
    assert "Navigator.prototype, 'webdriver', () => false" in script


def test_pinned_slidex_applies_network_fingerprint_in_headful(slider):
    """有头页面也要走 CDP UA/UA-CH 网络层覆盖（否则 Sec-CH-UA 头仍是
    真实内核派生值）。旧接口名 _apply_headless_network_fingerprint 的
    headless 门禁版本视为回退。"""
    import slidex.stealth as stealth_module
    from slidex.stealth import XianyuSliderStealth

    assert hasattr(XianyuSliderStealth, "_apply_network_fingerprint"), (
        "缺少 _apply_network_fingerprint——pin 回退到了有头不统一网络层的版本"
    )
    assert not hasattr(XianyuSliderStealth, "_apply_headless_network_fingerprint"), (
        "旧的 _apply_headless_network_fingerprint 仍在——headless 门禁回来了"
    )
    src = Path(stealth_module.__file__).read_text(encoding="utf-8")
    assert "platformName" in src, "platformName 拆分缺失（platform vs UA-CH 高层平台名）"


def test_pinned_slidex_has_no_numeric_plugins_anywhere():
    import slidex.stealth as stealth_module

    source = Path(stealth_module.__file__).read_text(encoding="utf-8")
    assert "1, 2, 3, 4, 5" not in source, (
        "安装的 slidex 仍有数字数组 plugins 注入——指纹破绽回来了"
    )
