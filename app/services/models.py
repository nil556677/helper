"""模型槽位的读写与连接测试（**UI-free**，可以直接被 pytest 调用）。

**配置全在数据库**：每个槽位一行写进 ``llm_slots``；api key 只存 **DPAPI 密文 + 尾号**，
明文既不落盘也不进日志。``config.json`` 只留"打开数据库之前就必须知道"的两项
（日志级别、数据目录）——见方案 §12。

**为什么只有两个槽位**：大模型负责回答与查询重写，OCR 模型负责把扫描件 / 图片转成文字。
以后加槽位 = 这里加一行（表里 ``slot`` 是主键、没写死取值约束，**不用迁移库**）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.llm import store as slot_store
from app.core.llm.openai_compat import ProbeResult, probe_chat, probe_ocr
from app.core.security import SecurityError, protect, unprotect

#: 尾号显示几个字符（只够辨认"是不是我那一把"，不足以还原）
KEY_TAIL_CHARS = 4

#: 明文 key 最短长度（各家都远长于这个；太短几乎肯定是粘错了）
KEY_MIN_CHARS = 8


@dataclass(frozen=True)
class Slot:
    """一个模型槽位。"""

    key: str
    """写进数据库的槽位名（``llm_slots.slot``）。"""

    title: str
    purpose: str
    example: str
    probe: str
    """用哪个探测函数：``chat`` 或 ``ocr``。"""


#: 现在的三个槽位。加槽位就在这后面加一行。
SLOTS: tuple[Slot, ...] = (
    Slot(
        key="llm",
        title="大模型",
        purpose="回答、查询重写等任务统一使用的大模型，走 OpenAI 兼容协议",
        example="火山方舟 / doubao-seed-1.6",
        probe="chat",
    ),
    Slot(
        key="llm-compact",
        title="压缩摘要模型",
        purpose="压缩远期记忆（会话摘要）用；不配置则沿用「大模型」",
        example="火山方舟 / doubao-seed-1.6",
        probe="chat",
    ),
    Slot(
        key="ocr",
        title="OCR 模型",
        purpose="扫描件 / 图片转文字；没有它，扫描版资料进不来",
        example="火山方舟 / doubao-vision-ocr",
        probe="ocr",
    ),
)

_PROBES = {"chat": probe_chat, "ocr": probe_ocr}


def slot_keys() -> tuple[str, ...]:
    """全部槽位名。"""
    return tuple(slot.key for slot in SLOTS)


def _slot(slot_key: str) -> Slot:
    for slot in SLOTS:
        if slot.key == slot_key:
            return slot
    raise ValueError(f"不认识的模型槽位：{slot_key}（可选：{'、'.join(slot_keys())}）")


def _row(slot_key: str) -> Any:
    return slot_store.get(slot_key)


@dataclass(frozen=True)
class SlotView:
    """一个槽位在界面上的样子（槽位元信息 + 已存配置）。

    **绝不包含明文 key**：只有尾号 ``key_tail``——界面靠它告诉用户"存着的是哪一把"。
    """

    key: str
    title: str
    purpose: str
    example: str
    provider: str
    base_url: str
    model: str
    key_tail: str
    has_key: bool
    configured: bool


@dataclass(frozen=True)
class ConnectionOutcome:
    """一次「测试连接」的结果。"""

    ok: bool
    kind: str
    detail: str
    elapsed_ms: int


def load_all() -> tuple[SlotView, ...]:
    """读出全部槽位（槽位元信息 + 已存配置）。

    **绝不返回明文 key**：只给尾号（``key_tail``）——界面就靠它告诉用户"存着的是哪一把"。
    """
    rows = {str(row["slot"]): row for row in slot_store.list_all()}
    items: list[SlotView] = []
    for slot in SLOTS:
        row = rows.get(slot.key)
        base_url = str(row["base_url"]) if row else ""
        model = str(row["model"]) if row else ""
        items.append(
            SlotView(
                key=slot.key,
                title=slot.title,
                purpose=slot.purpose,
                example=slot.example,
                provider=str(row["provider"]) if row else "",
                base_url=base_url,
                model=model,
                key_tail=str(row["key_tail"] or "") if row else "",
                has_key=bool(row and row["api_key_cipher"]),
                configured=bool(base_url.strip() and model.strip()),
            )
        )
    return tuple(items)


def _validate(provider: str, base_url: str, model: str) -> tuple[str, str, str]:
    """清洗并校验三个手填字段。"""
    provider, base_url, model = provider.strip(), base_url.strip(), model.strip()
    if not base_url:
        raise ValueError("接口地址不能为空")
    if not model:
        raise ValueError("模型名不能为空")
    if not base_url.lower().startswith(("http://", "https://")):
        raise ValueError("接口地址要以 http:// 或 https:// 开头")
    return provider, base_url, model


def save(
    slot_key: str,
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: str | None = None,
) -> None:
    """保存一个槽位（每个槽位各自保存，互不影响）。

    参数：
        slot_key: 槽位名（见 :data:`SLOTS`）。
        provider: 服务商（手填，仅用于展示与排查）。
        base_url: 接口地址。
        model: 模型名。
        api_key: ``None`` = **不动**已存的 key；空串 = 清掉；其他 = 换成它
            （加密后只存密文与尾号）。
    异常：
        ValueError: 槽位名不认识，或必填项为空 / 明显不合法。
        SecurityError: 加密失败（比如不是 Windows）。
    """
    slot = _slot(slot_key)
    provider, base_url, model = _validate(provider, base_url, model)

    if api_key is not None and api_key.strip() and len(api_key.strip()) < KEY_MIN_CHARS:
        raise ValueError(f"api key 太短（少于 {KEY_MIN_CHARS} 个字符），像是没粘全")

    if api_key is None:
        # 不动密钥列：只更新手填的三项
        slot_store.upsert_meta(slot.key, provider=provider, base_url=base_url, model=model)
        return

    key = api_key.strip()
    if not key:
        # 传空串 = 清掉 key（尾号也一起清）
        slot_store.clear_key(slot.key, provider=provider, base_url=base_url, model=model)
        return

    slot_store.upsert_with_key(
        slot.key,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key_cipher=protect(key.encode("utf-8")),
        key_tail=key[-KEY_TAIL_CHARS:],
    )


def test_connection(
    slot_key: str,
    *,
    timeout: float | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> ConnectionOutcome:
    """真发一次极小的请求，把"通不通"验出来。

    参数：
        slot_key: 槽位名。
        timeout: 覆盖探测超时（测试用）。
        base_url / model / api_key: **可选的"当前输入"覆盖**——界面里改了输入框还没保存时，
            直接拿屏幕上的值测，而不是测库里那份旧的（否则会让人误判"配置没问题"）。
            ``api_key`` 传明文即可，只在本函数内存里用，不落盘。
    ``kind`` 见 :data:`app.core.llm.openai_compat.KIND_LABELS`——**失败会分类**，
    用户照着 ``detail`` 就能改。
    """
    slot = _slot(slot_key)  # 槽位
    row = _row(slot_key)  # 库里那一行（可能还没保存过）
    url = (base_url if base_url is not None else str(row["base_url"]) if row else "").strip()  # 地址
    mdl = (model if model is not None else str(row["model"]) if row else "").strip()  # 模型名
    if not url or not mdl:  # 必填缺失
        return ConnectionOutcome(
            ok=False, kind="config", detail="先填好接口地址与模型名并保存", elapsed_ms=0
        )

    key = (api_key or "").strip()  # 屏幕上填的新 key（可能为空）
    if key:  # 有当前输入 → 直接用它
        plain = key  # 明文
    elif row is not None and row["api_key_cipher"]:  # 没填新的 → 用库里存着的
        try:
            plain = unprotect(bytes(row["api_key_cipher"])).decode("utf-8")  # 解密
        except SecurityError as exc:  # 换用户/换机器/密文损坏
            return ConnectionOutcome(
                ok=False,
                kind="auth",
                detail=f"存着的密钥解不开：{exc}。请重新粘贴一次 key",
                elapsed_ms=0,
            )
    else:  # 既没新 key 也没存过
        return ConnectionOutcome(ok=False, kind="config", detail="还没填 api key", elapsed_ms=0)

    probe = _PROBES[slot.probe]  # 探测方式
    kwargs = {} if timeout is None else {"timeout": timeout}  # 超时
    result: ProbeResult = probe(url, mdl, plain, **kwargs)  # 真发一次
    return ConnectionOutcome(
        ok=result.ok,
        kind=result.kind,
        detail=result.detail,
        elapsed_ms=result.elapsed_ms,
    )


@dataclass(frozen=True)
class LLMRuntime:
    """一次调用要用的已解密配置（**只给内部用，绝不进日志/界面**）。"""

    base_url: str
    model: str
    api_key: str


def llm_runtime() -> LLMRuntime | None:
    """读 ``llm`` 槽位并解密 key；没配全/解不开返回 ``None``（重写直接跳过）。"""
    row = _row("llm")
    if row is None:
        return None
    base_url = str(row["base_url"] or "").strip()
    model = str(row["model"] or "").strip()
    cipher = row["api_key_cipher"]
    if not base_url or not model or not cipher:
        return None
    try:
        api_key = unprotect(bytes(cipher)).decode("utf-8")
    except (SecurityError, OSError, ValueError):
        return None
    return LLMRuntime(base_url=base_url, model=model, api_key=api_key)
