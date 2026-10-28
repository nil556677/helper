"""聊天页的滚动跟随（浏览器侧贴底 + 延迟滚动 + 定位某一轮）。"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from nicegui import ui

from app.ui.screens.chat.state import _SCROLL_TASKS

_SCROLL_JS = """
<script>
(() => {
  const setup = () => {  // 装一次：拿到滚动容器与内容元素
    const area = document.querySelector('.hp-chat-scroll');  // 聊天滚动区
    if (!area) return false;  // 页面还没画出来
    const box = area.querySelector('.q-scrollarea__container');  // 真正滚动的那个 div
    const content = area.querySelector('.q-scrollarea__content');  // 内容层
    if (!box || !content) return false;  // 结构还没到位
    window.__hpFollow = true;  // 默认贴底
    let queued = false;  // 本帧是否已经排过滚动
    const bottom = () => {  // 真正滚到底
      if (window.__hpFollow) box.scrollTop = box.scrollHeight;  // 贴到底（强制一次布局）
    };
    const schedule = () => {  // 每帧最多滚一次：MutationObserver 一次回调可能带几百条变动
      if (queued) return;  // 本帧已排
      queued = true;  // 占位
      requestAnimationFrame(() => { queued = false; bottom(); });  // 下一帧统一滚
    };
    box.addEventListener('scroll', () => {  // 读者自己滚动
      const gap = box.scrollHeight - box.scrollTop - box.clientHeight;  // 离底距离
      window.__hpFollow = gap < 4;  // 小于 4px 算"还在底部"（DSH 用 1px 容差）
    }, { passive: true });  // 被动监听，别拖慢滚动
    new ResizeObserver(schedule).observe(content);  // 内容变高就跟随（合并到帧）
    new MutationObserver(schedule).observe(content, { childList: true, subtree: true });  // 新节点也跟随
    window.__hpBottom = bottom;  // 暴露给 Python 调用
    return true;  // 装好了
  };
  if (!setup()) {  // 首次可能还没渲染
    let tries = 0;  // 已重试次数
    const timer = setInterval(() => {  // 重试安装
      if (setup() || ++tries >= 20) clearInterval(timer);  // 装上就停；试满 4 秒也没装上就放弃（别留个永转的 200ms 定时器）
    }, 200);  // 每 200ms 试一次
  }
})();
</script>
"""


async def _delayed_scroll(scroll: Any, delay: float = 0.08) -> None:  # 延迟滚一次
    """新行刚插进 DOM 时高度还没算完，立刻滚会停在旧位置；等一拍再滚。"""
    await asyncio.sleep(delay)  # 等浏览器排版
    with contextlib.suppress(Exception):  # 元素可能已被重建/删除
        scroll.scroll_to(percent=1.0)  # 滚到底


def _schedule_scroll(scroll: Any) -> None:  # 排一个延迟滚动
    """不阻塞当前流程地补一次滚动（排版完成后再滚）。"""
    task = asyncio.create_task(_delayed_scroll(scroll))  # 建任务
    _SCROLL_TASKS.add(task)  # 持有引用
    task.add_done_callback(_SCROLL_TASKS.discard)  # 完成即移除


def _set_follow(on: bool) -> None:  # 开关"贴底跟随"
    """改浏览器侧的跟随状态（DSH：提交输入即恢复跟随；往上翻即退出）。"""
    script = "window.__hpFollow = %s; window.__hpBottom && window.__hpBottom();" % (
        "true" if on else "false"
    )  # JS 片段
    try:  # 拿不到客户端上下文就跳过（测试/无头）
        from nicegui import context  # 局部导入

        client = context.client  # 当前客户端
        if client is None or not client.has_socket_connection:  # 页面还没连上
            return  # 首屏由 JS 默认的 follow=true 处理
    except Exception:  # 没有上下文
        return  # 跳过
    with contextlib.suppress(Exception):  # 发送失败不该影响发送流程
        ui.run_javascript(script)  # 建完就发：NiceGUI 自己在后台跑，不要 await


def _scroll_to_box(box: Any, delay: float = 0.06) -> None:  # 滚到某一轮
    """加载更早之后把视口锚回原来那条（DSH 的"分页锚点"简化版）。"""
    async def _run() -> None:  # 异步壳
        await asyncio.sleep(delay)  # 等重排
        with contextlib.suppress(Exception):  # 元素可能已被删
            if box is not None and not box.is_deleted:  # 还在
                box.run_method('scrollIntoView', {'block': 'start'})  # 滚到它的顶部

    task = asyncio.create_task(_run())  # 建任务
    _SCROLL_TASKS.add(task)  # 持有引用
    task.add_done_callback(_SCROLL_TASKS.discard)  # 完成即移除
