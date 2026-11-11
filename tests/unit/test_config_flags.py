"""配置完整性守卫：UI 会读写（get_flag/set_flag）的开关必须登记进参数白名单，否则 save 会 KeyError。"""

from __future__ import annotations

from app.core.config import params as P  # 参数清单


def test_ui_flags_are_registered() -> None:  # UI 开关都已登记（可被 save_values 接受）
    for name, default in (  # 与 settings/general.py 一致的开关
        ("agent.context_builder_enabled", True),  # ContextBuilder 注入
        ("agent.dispatch_context_enabled", False),  # dispatch 上文拼接
    ):  # 遍历
        param = P.BY_KEY.get(name)  # 查注册
        assert param is not None, f"UI 开关 {name} 未登记进 params.py（save 会 KeyError）"  # 必须已登记
        assert param.cast is bool  # 是布尔开关
        assert param.default == default  # 默认值一致


def test_all_saved_keys_known() -> None:  # 常见写入键都在白名单（防 vector.url 类漏登记）
    for key in ("vector.url", "agent.compaction.slot"):  # 含这几类写入键
        assert key in P.BY_KEY  # 已登记