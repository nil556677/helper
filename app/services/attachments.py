"""聊天附件：落盘、元数据、可读文本（不碰资料库）。"""

from __future__ import annotations  # 类型注解延迟求值

import shutil  # 删除目录
from pathlib import Path  # 路径
from typing import Any  # 任意
from uuid import uuid4  # 目录 id

from app.core import paths  # 数据目录

MAX_BYTES = 20 * 1024 * 1024  # 单个附件上限
TEXT_LIMIT = 20_000  # 送进模型的最大字符
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log", ".py", ".yml", ".yaml", ".toml", ".ini"}  # 可读文本
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}  # 图片


def root() -> Path:  # 附件根
    """data/attachments。"""
    folder = paths.data_dir() / "attachments"  # 目录
    folder.mkdir(parents=True, exist_ok=True)  # 建
    return folder  # 返回


def prepare(name: str) -> tuple[Path, str]:  # 准备落盘
    """返回目标路径与净化后的名字。"""
    safe = paths.safe_filename(name)  # 净化
    folder = root() / uuid4().hex  # 一轮一个目录
    folder.mkdir(parents=True, exist_ok=False)  # 建
    return folder / safe, safe  # 返回


def _kind(name: str) -> str:  # 判断类型
    """image / text / file。"""
    suffix = Path(name).suffix.lower()  # 后缀
    if suffix in IMAGE_SUFFIXES:  # 图片
        return "image"  # 图片
    if suffix in TEXT_SUFFIXES:  # 文本
        return "text"  # 文本
    return "file"  # 其他


def finalize(path: Path, name: str) -> dict[str, Any]:  # 完成后元数据
    """算大小/类型/可读文本；失败不抛。"""
    size = path.stat().st_size if path.is_file() else 0  # 大小
    kind = _kind(name)  # 类型
    text = ""  # 文本
    truncated = False  # 是否截断
    if kind == "text" and path.is_file():  # 文本
        try:  # 读
            raw = path.read_text(encoding="utf-8", errors="replace")  # 全文
            text = raw[:TEXT_LIMIT]  # 截断
            truncated = len(raw) > TEXT_LIMIT  # 标记
        except OSError:  # 读失败
            text = ""  # 放弃
    return {  # 元数据
        "id": path.parent.name,  # 目录 id
        "name": name,  # 名字
        "path": str(path),  # 绝对路径
        "size": size,  # 字节
        "kind": kind,  # 类型
        "text": text,  # 可读文本
        "truncated": truncated,  # 截断
    }  # 结束


def remove(item: dict[str, Any]) -> None:  # 删除附件
    """只删 attachments 目录内的。"""
    raw = str(item.get("path") or "")  # 路径
    if not raw:  # 空
        return  # 跳过
    path = Path(raw)  # 路径
    base = root().resolve()  # 根
    try:  # 防越界
        if path.resolve().parent.parent != base:  # 不在根下
            return  # 跳过
    except OSError:  # 解析失败
        return  # 跳过
    shutil.rmtree(path.parent, ignore_errors=True)  # 删整轮目录


def public(item: dict[str, Any]) -> dict[str, Any]:  # 前端/落库用
    """去掉绝对路径与正文。"""
    return {  # 公开字段
        "id": item.get("id"),  # id
        "name": item.get("name"),  # 名字
        "size": item.get("size"),  # 大小
        "kind": item.get("kind"),  # 类型
        "path": item.get("path"),  # 本地路径（图片预览用）
        "truncated": bool(item.get("truncated")),  # 截断
    }  # 结束


def load_text(item: dict[str, Any]) -> str:  # 按元数据读正文（E0.4）
    """正文不随 state/checkpoint 走：需要时按 path 从磁盘读（截断，失败空）。"""
    raw = str(item.get("path") or "")  # 路径
    if not raw:  # 无路径
        return ""  # 空
    try:  # 读
        return Path(raw).read_text(encoding="utf-8", errors="replace")[:TEXT_LIMIT]  # 读+截断
    except OSError:  # 读失败
        return ""  # 空


def prompt_block(items: list[dict[str, Any]]) -> str:  # 拼进提示词
    """只拼可读文本；图片给一句说明。"""
    blocks: list[str] = []  # 块
    for item in items:  # 逐条
        name = str(item.get("name") or "附件")  # 名字
        text = str(item.get("text") or "").strip()  # 文本
        if text:  # 有文本
            blocks.append(f"【{name}】\n{text}")  # 文本块
        elif item.get("kind") == "image":  # 图片
            blocks.append(f"【{name}】图片附件（当前模型未启用图片理解）")  # 说明
    return "\n\n".join(blocks)  # 合并
