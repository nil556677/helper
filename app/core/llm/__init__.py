"""云端模型：OpenAI 兼容适配 + 模型槽位。

现在只有一个能力：**把"配好没有"验出来**（见 :mod:`app.core.llm.openai_compat`）。
P3 才在这里加真正的对话、流式输出与提示词组装。
"""

from app.core.llm.openai_compat import ProbeResult, probe_chat, probe_ocr

__all__ = ["ProbeResult", "probe_chat", "probe_ocr"]
