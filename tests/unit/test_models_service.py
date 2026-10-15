"""模型槽位：存取、只留尾号、连接测试的分类。

这些用例钉住三件事：

1. **api key 只以密文入库**（库里翻不到明文，对外只给尾号）；
2. 保存粒度是**每个槽位各自一份**（动一个不影响另一个；不传 key 就不动已存的 key）；
3. **连接失败必须分类**（key 错 / 地址错 / 模型名错 / 超时…），因为"失败了"三个字没法照着改。
"""

from __future__ import annotations

import urllib.error
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.db.connection import get_connection
from app.services import models

PLAIN_KEY = "sk-test-abcdefghijklmnop-1234"


@pytest.fixture
def slots_db(fake_root: Path) -> Iterator[None]:
    """一个建好表、目录隔离在 ``.tmp/`` 下的空库。"""
    from app.core import paths
    from app.core.db import schema
    from app.core.db.connection import close_thread_connection

    paths.ensure_dirs()
    schema.init_db()
    yield
    close_thread_connection()


def _fake_post(result: tuple[int, str] | Exception):
    """替掉 HTTP 那一层：返回给定的 (状态码, 响应体)，或抛给定异常。"""

    def post(url: str, payload: dict, api_key: str, timeout: float) -> tuple[int, str]:
        if isinstance(result, Exception):
            raise result
        return result

    return post


def test_save_then_load_keeps_tail_not_plaintext(slots_db: None) -> None:
    """保存后：三项手填值回来了，key 只给尾号，**明文既不在返回值里也不在库里**。"""
    models.save(
        "reasoning",
        provider="火山方舟",
        base_url="https://ark.example.com/api/v3",
        model="doubao-seed-1.6",
        api_key=PLAIN_KEY,
    )

    item = next(i for i in models.load_all() if i["key"] == "reasoning")
    assert item["provider"] == "火山方舟"
    assert item["base_url"] == "https://ark.example.com/api/v3"
    assert item["model"] == "doubao-seed-1.6"
    assert item["key_tail"] == PLAIN_KEY[-4:]
    assert item["has_key"] is True
    assert item["configured"] is True
    assert PLAIN_KEY not in repr(item)  # 对外结构里绝不能带明文

    row = get_connection().execute("SELECT api_key_cipher FROM llm_slots").fetchone()
    blob = bytes(row["api_key_cipher"])
    assert PLAIN_KEY.encode() not in blob  # 库里也只有密文


def test_save_without_key_keeps_existing_one(slots_db: None) -> None:
    """不传 key（``None``）时只改手填三项，**不动已存的 key**。"""
    models.save("reasoning", provider="A", base_url="https://a.example/v1", model="m1", api_key=PLAIN_KEY)
    models.save("reasoning", provider="B", base_url="https://b.example/v1", model="m2")

    item = next(i for i in models.load_all() if i["key"] == "reasoning")
    assert item["provider"] == "B"
    assert item["key_tail"] == PLAIN_KEY[-4:]
    assert item["has_key"] is True


def test_empty_key_clears_it(slots_db: None) -> None:
    """传空串 = 清掉 key（尾号也一起清）。"""
    models.save("reasoning", provider="A", base_url="https://a.example/v1", model="m1", api_key=PLAIN_KEY)
    models.save("reasoning", provider="A", base_url="https://a.example/v1", model="m1", api_key="")

    item = next(i for i in models.load_all() if i["key"] == "reasoning")
    assert item["has_key"] is False
    assert item["key_tail"] == ""


def test_slots_are_independent(slots_db: None) -> None:
    """两个槽位互不影响（各自保存）。"""
    models.save("reasoning", provider="R", base_url="https://r.example/v1", model="m-r", api_key=PLAIN_KEY)
    models.save("ocr", provider="O", base_url="https://o.example/v1", model="m-o")  # 这个还没填 key

    items = {i["key"]: i for i in models.load_all()}
    assert items["reasoning"]["model"] == "m-r" and items["reasoning"]["has_key"] is True
    assert items["ocr"]["model"] == "m-o" and items["ocr"]["has_key"] is False


def test_slot_list_is_the_two_we_agreed(slots_db: None) -> None:
    """现在只有两个槽位（推理 / OCR），顺序也固定——加槽位是显式动作。"""
    assert models.slot_keys() == ("reasoning", "ocr")
    assert [i["title"] for i in models.load_all()] == ["推理模型", "OCR 模型"]


def test_validation_rejects_obviously_wrong_input(slots_db: None) -> None:
    """空地址、没有协议头、过短的 key、不认识的槽位——都要当场拒绝并说清原因。"""
    with pytest.raises(ValueError, match="接口地址不能为空"):
        models.save("reasoning", provider="", base_url="  ", model="m")
    with pytest.raises(ValueError, match="模型名不能为空"):
        models.save("reasoning", provider="", base_url="https://a.example/v1", model=" ")
    with pytest.raises(ValueError, match="http"):
        models.save("reasoning", provider="", base_url="ark.example.com", model="m")
    with pytest.raises(ValueError, match="太短"):
        models.save("reasoning", provider="", base_url="https://a.example/v1", model="m", api_key="short")
    with pytest.raises(ValueError, match="不认识的模型槽位"):
        models.save("vision", provider="", base_url="https://a.example/v1", model="m")


def test_test_connection_reports_missing_config(slots_db: None) -> None:
    """没填全就点测试：直接告诉缺什么，不发请求。"""
    assert models.test_connection("reasoning")["kind"] == "config"

    models.save("reasoning", provider="A", base_url="https://a.example/v1", model="m")
    result = models.test_connection("reasoning")
    assert result["kind"] == "config"
    assert "api key" in result["detail"]


def test_test_connection_ok(slots_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """配好之后探测成功 → ok，并带上耗时。"""
    from app.core.llm import openai_compat

    monkeypatch.setattr(openai_compat, "_post", _fake_post((200, "{}")))
    models.save("reasoning", provider="A", base_url="https://a.example/v1", model="m", api_key=PLAIN_KEY)

    result = models.test_connection("reasoning")

    assert result["ok"] is True
    assert result["kind"] == "ok"
    assert "推理模型" in result["detail"]


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, '{"error": {"message": "invalid api key"}}', "auth"),
        (403, "{}", "auth"),
        (404, "{}", "not_found"),
        (400, '{"error": {"message": "model not found"}}', "bad_request"),
        (429, "{}", "rate_limit"),
        (500, "{}", "server"),
    ],
)
def test_failures_are_classified(slots_db: None, monkeypatch: pytest.MonkeyPatch, status: int, body: str, expected: str) -> None:
    """每种 HTTP 失败都要落到一个**能照着改**的分类上，并带上对方给的原话。"""
    from app.core.llm import openai_compat

    monkeypatch.setattr(openai_compat, "_post", _fake_post((status, body)))
    models.save("reasoning", provider="A", base_url="https://a.example/v1", model="m", api_key=PLAIN_KEY)

    result = models.test_connection("reasoning")

    assert result["ok"] is False
    assert result["kind"] == expected
    assert result["detail"]


def test_timeout_and_network_are_classified(slots_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时与"连不上"要分开报：一个多半是地址/网络，一个多半是对方太慢。"""
    from app.core.llm import openai_compat

    models.save("reasoning", provider="A", base_url="https://a.example/v1", model="m", api_key=PLAIN_KEY)

    monkeypatch.setattr(openai_compat, "_post", _fake_post(urllib.error.URLError(TimeoutError())))
    assert models.test_connection("reasoning")["kind"] == "timeout"

    monkeypatch.setattr(openai_compat, "_post", _fake_post(urllib.error.URLError("getaddrinfo failed")))
    assert models.test_connection("reasoning")["kind"] == "network"


def test_unknown_key_failure_says_to_paste_again(slots_db: None) -> None:
    """存着的密文解不开（换用户/换机器/损坏）时，要明说"重新粘一次"，而不是含糊报错。"""
    models.save("reasoning", provider="A", base_url="https://a.example/v1", model="m", api_key=PLAIN_KEY)
    from app.core.db.connection import transaction

    with transaction() as conn:
        conn.execute("UPDATE llm_slots SET api_key_cipher = ? WHERE slot = 'reasoning'", (b"broken",))

    result = models.test_connection("reasoning")

    assert result["kind"] == "auth"
    assert "重新粘贴" in result["detail"]


def test_endpoint_accepts_both_base_url_shapes() -> None:
    """接口地址填到 ``/v1`` 或整条 ``…/chat/completions`` 都要能用（别拼成两段）。"""
    from app.core.llm.openai_compat import _endpoint

    assert _endpoint("https://a.example/v1") == "https://a.example/v1/chat/completions"
    assert _endpoint("https://a.example/v1/") == "https://a.example/v1/chat/completions"
    assert (
        _endpoint("https://a.example/v1/chat/completions")
        == "https://a.example/v1/chat/completions"
    )


def test_ocr_probe_sends_a_real_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """OCR 探测必须真的带一张图——只发文本可能"成功"却没碰到图像通道。"""
    from app.core.llm import openai_compat

    captured: dict = {}

    def post(url: str, payload: dict, api_key: str, timeout: float) -> tuple[int, str]:
        captured.update(payload)
        return 200, "{}"

    monkeypatch.setattr(openai_compat, "_post", post)
    result = openai_compat.probe_ocr("https://a.example/v1", "m", "k")

    assert result.ok is True
    content = captured["messages"][0]["content"]
    assert any(part["type"] == "image_url" for part in content)
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
