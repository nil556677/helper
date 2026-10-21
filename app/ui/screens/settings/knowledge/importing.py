"""知识库 · 视图三：导入（独立页签）。

上传是**一次性动作**，不该和"浏览/筛选已有资料"挤在一起（用户明确要求单独一页）。
这一页只有两件事：**上传**，以及**导入之后会发生什么**（三步说明 + 去列表的入口）。

**NicEUI 3.18 的上传事件**（踩过的坑）：`UploadEventArguments` 只有 ``file``（``FileUpload``），
没有 ``event.name``/``event.content``。所以用 ``await event.file.save(路径)`` **流式落盘**，
再让服务层算哈希、判重、落占位行（大文件不整包进内存）。
"""

from __future__ import annotations

import contextlib
from typing import Any

from loguru import logger
from nicegui import events, run, ui

from app.services import knowledge as knowledge_service
from app.ui.components.dialog import notify
from app.ui.screens.settings.knowledge.widgets import card_head

#: 单个文件的上传上限（与服务端共用同一个数：前端拦一次，落盘后再校验一次）
MAX_UPLOAD_BYTES = knowledge_service.MAX_UPLOAD_BYTES

#: 导入之后的三步（顺序就是真实的管线顺序）
_STEPS: tuple[tuple[str, str, str], ...] = (
    ("looks_one", "排队", "文件先落到本机暂存区，任务表里排一条导入任务（关掉窗口也不丢）"),
    (
        "looks_two",
        "解析",
        "本地解析 → 识别标题结构 → 切成父块 / 子块 → 词法入库；完成后**立刻能搜到文字**",
    ),
    ("looks_3", "建向量", "后台分批建向量，全部写完才变「就绪」；中途关窗口也不会重来（断点续跑）"),
)


def render(container: ui.column, *, go_list: Any) -> None:
    """画导入页（上传卡 + 三步说明）。"""
    with container:
        import_card(on_done=lambda: None)
        with ui.card().classes("hp-card w-full gap-2"):
            card_head("route", "导入之后会发生什么")
            for icon, title, body in _STEPS:
                with ui.row().classes("hp-row-item w-full items-center gap-3"):
                    ui.icon(icon).classes("text-base hp-card-icon")
                    with ui.column().classes("gap-0 flex-1 min-w-0"):
                        ui.label(title).classes("hp-field-help")
                        ui.label(body).classes("hp-field-help")
            with ui.row().classes("w-full items-center"):
                ui.space()
                ui.button(
                    "去看资料列表", icon="format_list_bulleted", on_click=lambda: go_list()
                ).props("flat dense no-caps").mark("kb-goto-list")


def import_card(*, on_done: Any) -> None:
    """导入卡：上传 → 流式落 inbox → 排导入任务。

    参数：
        on_done: 一次上传处理完（成功/跳过/失败都算）之后的回调——调用方决定要不要刷新。
    """
    with ui.card().classes("hp-card w-full gap-3"):
        card_head("upload_file", "导入资料")
        ui.label(
            f"支持 {knowledge_service.supported_formats()}；可以一次选多个文件。"
            "扫描件（图片型 PDF）要 OCR 才能读，还没接。"
        ).classes("hp-field-help")

        async def on_upload(event: events.UploadEventArguments) -> None:
            """单个文件的上传：先查格式 → 落盘（限大）→ 登记（哈希/判重/占位）→ 排队导入。

            **失败路径都要收干净**（P7 审查）：
            - 超限 → 删半成品；
            - 入队失败 → 回滚刚落的占位行（否则列表里挂一条"待处理"到 24h）；
            - 任何异常 → 删暂存文件 + 给人话（内部异常只进日志）。
            """
            target = None
            doc_id: str | None = None
            try:
                file = event.file
                name = str(file.name or "unnamed")
                if not knowledge_service.is_supported_format(name):
                    # 不支持的格式：**拦在落盘/排队之前**，给一句人话（老式 Office 另存为、图片走 OCR）
                    hint = knowledge_service.unsupported_hint(name)
                    reason = (
                        f"：{hint}" if hint else f"。支持：{knowledge_service.supported_formats()}"
                    )
                    notify(f"「{name}」不是支持的格式{reason}", kind="negative")
                    return
                target = knowledge_service.inbox_target(name)
                await file.save(target)  # 流式落盘（大文件不整包进内存）
                # 服务端再校验一次大小：前端 max_file_size 只在浏览器侧生效，不能当唯一防线。
                knowledge_service.check_size(target)
                payload = await run.io_bound(knowledge_service.register_staged, name, target)
                if payload.get("skip"):
                    # 判重命中：这次落的暂存文件没有任务会来读，立刻删掉（别留给 1 小时后的清理）
                    with contextlib.suppress(OSError):
                        target.unlink(missing_ok=True)
                    notify(f"「{name}」的内容已经在资料库里了，跳过", kind="info")
                else:
                    doc_id = payload.get("doc_id")
                    try:
                        job_id = knowledge_service.enqueue_import(
                            payload,
                            dedupe_key=knowledge_service.upload_dedupe_key(payload["sha256"]),
                        )
                    except Exception:
                        # **入队失败 → 回滚占位行**：否则列表里会挂一条永远不动的"待处理"。
                        knowledge_service.discard_pending(doc_id)
                        doc_id = None
                        raise
                    notify(f"「{name}」已加入队列（任务 #{job_id}）", kind="positive")
            except knowledge_service.UploadTooLarge as exc:
                notify(f"「{name}」超过上传上限：{exc}", kind="negative")
            except Exception as exc:
                logger.exception("上传失败：{}", name)
                if isinstance(exc, OSError):
                    notify(f"「{name}」上传失败：写暂存文件出错（磁盘空间或权限）", kind="negative")
                else:
                    notify(f"「{name}」上传失败：{exc}", kind="negative")
            finally:
                # 走到这里还没进队列/没跳过 → 暂存文件没有任务会来读，删掉
                if target is not None and doc_id is None:
                    with contextlib.suppress(OSError):
                        target.unlink(missing_ok=True)
                on_done()

        ui.upload(
            label="选择文件…",
            multiple=True,
            auto_upload=True,
            max_file_size=MAX_UPLOAD_BYTES,
            on_upload=on_upload,
            on_rejected=lambda *_: notify(
                f"单个文件不能超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB", kind="negative"
            ),
        ).props("flat dense color=primary").classes("hp-upload w-full").mark("kb-upload")
        ui.label(
            "导入在后台排队进行，进度看 设置 → 任务。重复内容会自动跳过。"
            f"片段上限 {knowledge_service.chunk_limit()}。"
        ).classes("hp-field-help")
