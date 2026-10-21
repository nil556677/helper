"""统一的按钮动作包装：点击即 loading → 线程池执行 → 有明确落点。

**为什么要有它**：审查（docs/设置界面体验审查.md）里"点了没反应/界面冻住/连点排重"
是同一类病的三个症状——按钮不进 loading、活儿在界面线程里跑、完成后没有落点。
这里把"正确姿势"写成两个函数，所有改数据的按钮都走它们：

- :func:`run_action`：当场执行（秒级，但可能碰盘/碰网）——丢线程池，按钮转圈，
  成功/失败都有通知；
- :func:`queue_action`：入队执行（长活，走任务系统或后台线程）——按钮禁用并改成
  "已排队…"，通知里给出"去哪看进度"。

两个都保证：**回调里的异常不会静默**（notify negative），**按钮一定会恢复**（finally）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import run

from app.ui.components.dialog import notify


async def run_action(
    button: Any,
    work: Callable[[], Any],
    *,
    done: str = "完成",
    fail: str = "操作失败",
    kind: str = "positive",
    after: Callable[[Any], None] | None = None,
) -> None:
    """把一个"当场做"的动作接到按钮上：转圈 → 线程池 → 通知 → 恢复。

    参数：
        button: 触发按钮（进 loading 并禁用，结束恢复）。
        work: 要做的事（同步函数；在 ``run.io_bound`` 里执行，不卡界面）。
        done: 成功通知的文案（work 返回 str 时会拼在它后面）。
        fail: 失败通知的前缀（后面拼异常信息）。
        kind: 成功通知的类型（positive / warning…）。
        after: 成功后的界面收尾（如 ``refresh``）；失败时不调。
    """
    button.props("loading")
    button.disable()
    try:
        result = await run.io_bound(work)
    except Exception as exc:
        notify(f"{fail}：{exc}", kind="negative")
        return
    finally:
        button.props(remove="loading")
        button.enable()
    message = f"{done}：{result}" if isinstance(result, str) and result else done
    notify(message, kind=kind)
    if after is not None:
        after()


async def queue_action(
    button: Any,
    enqueue: Callable[[], Any],
    *,
    queued: str = "已提交后台任务",
    fail: str = "提交失败",
    where: str = "任务页可见进度",
    after: Callable[[Any], None] | None = None,
) -> None:
    """把一个"入队"动作接到按钮上：禁用 + "已排队…" → 通知 → 恢复。

    与 :func:`run_action` 的区别：这里**不等结果**，enqueue 应当很快（只是插一条任务）；
    防连点是主要目的——用户连点五次不应该排五个重复任务。
    """
    button.disable()
    original_text = getattr(button, "text", "")
    if original_text:
        button.text = f"{original_text}…"
        button.update()
    try:
        job_id = await run.io_bound(enqueue)
    except Exception as exc:
        notify(f"{fail}：{exc}", kind="negative")
        return
    finally:
        button.enable()
        if original_text:
            button.text = original_text
            button.update()
    suffix = f"（任务 #{job_id}，{where}）" if isinstance(job_id, int) else f"（{where}）"
    notify(f"{queued}{suffix}", kind="positive")
    if after is not None:
        after()
