"""ONNX 嵌入适配器与 core.embed 的逐位一致性（需要真实模型文件）。

单独成文件：本模块**不使用 fake_root**，否则 paths.models_dir() 会指向临时目录。
"""

from __future__ import annotations

from pathlib import Path

import pytest

MODELS = Path(__file__).resolve().parents[2] / "models"


@pytest.mark.skipif(
    not (MODELS / "bge-m3.int8.onnx").is_file(),
    reason="bge-m3 模型未就绪",
)
def test_embeddings_parity_with_core():
    from app.core.embed import embed_text
    from app.integrations.embeddings import OnnxBgeM3Embeddings

    adapter = OnnxBgeM3Embeddings()
    text = "合同违约金怎么算"
    expected = [float(x) for x in embed_text(text)]
    got = adapter.embed_query(text)
    assert len(got) == len(expected) == 768
    assert max(abs(a - b) for a, b in zip(got, expected, strict=False)) < 1e-5
