"""设置 · 任务：正在跑 / 筛选 + 分页的列表（进度、取消、重试、暂停）。

**为什么单独一页**（用户定的）：任务系统是全项目的——导入、向量化、OCR、重建都从这儿看。
界面上不出现"队列实现细节"（泳道、租约、重试次数）这类开发术语：用户要的是
"在干什么、还要多久、出什么事了、我能做什么"。

**版式（重设计：数据可能很多 → 必须分页）**：

1. **正在跑**：单独一张卡、**不分页**——它是"现在在干什么"的答案，无论筛选到哪一组都要看见；
   而且它受泳道并发限制（cpu 1 + io 2），永远不会多到需要翻页；
2. **筛选**：进行中 / 失败 / 已完成 / 全部——队列积几百条、历史积上万条时，先分组再翻页；
3. **分页**：页码只决定 SQL 的 `LIMIT/OFFSET`，总数用一条 `count(*)`（不是"全查出来再切"）。

**为什么页面自己轮询**（0.5 秒）：任务跑在**工作线程**里，进度写进数据库；界面不直接接收线程
通知（那样会让任务生命周期和窗口绑在一起）。轮询成本可以忽略：两条走索引的查询。

**刷新只重画变了的东西**：页码/每页条数变了才重画分页控件，行内容变了才重画行——
否则每 0.5 秒重建一次会让"每页条数"下拉框在用户点开时被抽走。
"""

from __future__ import annotations

from typing import Any

from nicegui import run, ui

from app.services import tasks as tasks_service
from app.ui.components.auto_refresh import AutoRefresh
from app.ui.components.dialog import confirm, custom, notify
from app.ui.components.format import human_duration, human_time
from app.ui.components.pager import PAGE_SIZES, PageState, render_pager

#: 刷新间隔（秒）。进度写库有 0.2 秒节流，所以 0.5 秒够跟得上。
REFRESH_S = 1.0


def _progress_bar(value: float, *, uncertain: bool) -> None:
    """细进度条（**不做动画**：界面是软件渲染，动画会掉帧）。"""
    with ui.element("div").classes("hp-progress"):
        if uncertain:
            ui.element("div").classes("hp-progress-indeterminate")
        else:
            ui.element("div").classes("hp-progress-fill").style(
                f"width: {min(100.0, max(0.0, value * 100)):.0f}%"
            )


def _metrics(job: tasks_service.TaskView) -> str:
    """右侧那行小字：3/20 · 2 次失败 · 已完成 · 正在取消…"""
    if job.status == "running":
        if job.cancelling:
            return "正在取消…"
        if job.total:
            pct = max(0, min(100, round(job.done * 100 / job.total)))
            return f"{job.done}/{job.total} · {pct}%"
        return "进行中…"
    if job.status == "pending":
        return f"等待中（第 {job.queue_position} 位）" if job.queue_position else "等待中"
    if job.status == "failed":
        parts = ["失败"]
        if job.error_label:
            parts.append(job.error_label)  # 分类（§10.1 那套词汇）：一眼知道是哪类问题
        if job.attempts:
            parts.append(f"第 {job.attempts} 次")
        return " · ".join(parts)
    if job.status == "cancelled":
        return "已取消"
    return "完成"


def _row(job: tasks_service.TaskView, *, picked: set[int], on_pick: Any) -> None:
    """一行任务（最前面是"选中"开关：批量动作用它）。"""
    failed = job.status == "failed"
    metrics = _metrics(job)
    chosen = job.id in picked
    # 副标题别和状态列重复：失败时给原因，其余给 note——但"在跑且知道总数"时，
    # 状态列已经在报 3/20 了，再写一遍 3/20 批 只是噪音
    hint = job.error if failed and job.error else job.note
    if hint == metrics or (job.status == "running" and job.total):
        hint = ""
    with ui.row().classes("hp-row-item w-full items-center gap-3 no-wrap"):
        # 用图标按钮而不是 ui.checkbox：按钮在测试里点得动（checkbox 的 update:model-value 点不到）
        ui.button(
            icon="check_box" if chosen else "check_box_outline_blank",
            on_click=lambda job_id=job.id: on_pick(job_id),
        ).props("flat dense round size=sm").classes("hp-pick").mark(f"task-pick-{job.id}").tooltip(
            "取消选中" if chosen else "选中"
        )
        ui.icon(job.icon).classes("text-base hp-card-icon")
        with ui.column().classes("hp-col-name gap-0"):
            title = job.title + (f" · {job.name}" if job.name else "")
            ui.label(title).classes("hp-col-name")
            if hint:
                # 行里只放一句话；**完整原因与每一次的经过**在"详情"里（hover 就能看全文）
                ui.label(hint).classes("hp-field-help").tooltip(hint)
        if job.status == "running":
            with ui.element("div").classes("hp-progress-cell"):
                _progress_bar(float(job.progress), uncertain=not job.total)
        ui.label(metrics).classes("hp-col").style("width: 132px")
        # 耗时：跑过的任务都显示（run 完看"这次花了多久"，排查慢任务最直接）
        if job.duration_s is not None:
            ui.label(human_duration(job.duration_s)).classes("hp-col").style(
                "width: 76px"
            ).tooltip("这次执行花了多久")
        else:
            ui.label("—").classes("hp-col").style("width: 76px")
        ui.label(human_time(job.updated_at)).classes("hp-col").style("width: 72px")
        with ui.row().classes("hp-row-actions items-center gap-1 no-wrap"):
            if job.status in ("running", "pending"):
                ui.button(icon="close", on_click=lambda job_id=job.id: _cancel(job_id)).props(
                    "flat dense round size=sm"
                ).classes("hp-task-cancel").mark(f"task-cancel-{job.id}").tooltip("取消")
            elif failed or job.status == "cancelled":
                ui.button(icon="replay", on_click=lambda job_id=job.id: _retry(job_id)).props(
                    "flat dense round size=sm"
                ).classes("hp-icon-warn hp-btn-icon").mark(f"task-retry-{job.id}").tooltip("重试")
            # 详情：**每个任务都有**（参数、结果、耗时、每一次失败/中断的完整经过都在里面，
            # 不用去翻日志文件）。以前只在失败时才给，想看"这条到底带了什么参数"就没入口。
            ui.button(icon="info", on_click=lambda job_id=job.id: _show_details(job_id)).props(
                "flat dense round size=sm"
            ).classes("hp-icon-view hp-btn-icon").mark(f"task-details-{job.id}").tooltip(
                "详情：参数、结果、耗时与每一次尝试"
            )


def _cancel(job_id: int) -> None:
    """取消（可恢复 → 不弹确认框；恢复走任务行的「重试」）。"""
    if not tasks_service.cancel(job_id):
        notify("这个任务已经结束了", kind="info")
        return

    # **不能给 Quasar notify 传 Python 函数**：actions 会被原样 JSON 序列化，
    # `handler: fn` 会抛 "Type is not JSON serializable: function"（实测，整条 toast
    # 发不出去）。取消本身已可恢复（任务行「重试」），这里只给一句提示。
    notify("已请求取消；可在任务行点「重试」恢复", kind="warning", timeout=3000)


def _retry(job_id: int) -> None:
    """重试（fails 归零 = 全新的机会）。"""
    if tasks_service.retry(job_id):
        notify("已重新排队", kind="positive")
    else:
        notify("这个任务不能重试", kind="info")


def _show_details(job_id: int) -> None:
    """任务详情：状态、失败分类、以及**每一次尝试的经过**（含完整报错）。

    为什么要它：行里只能放一句话，而排查要看的是时间线——第几次、什么分类、那次的完整原因。
    以前这些只在 `logs/helper.log` 里，用户得自己去翻文件。
    """
    job = tasks_service.detail(job_id)
    if job is None:
        notify("这个任务已经不在库里了", kind="info")
        return

    def _body(close: Any) -> None:
        """正文（必须画在卡片里：custom() 只建壳，content 回调里画）。"""
        ui.label(
            f"{job.status_label} · 第 {job.attempts}/{job.max_attempts} 次尝试"
            f" · 入队 {human_time(job.created_at)}"
        ).classes("hp-field-help")  # 状态行
        # 时间三件套 + 耗时：排查"这条任务等了多久、跑了多久"最直接的一行
        ui.label(
            " · ".join(
                [
                    f"开始 {human_time(job.started_at, empty='还没开始')}",  # 开始
                    f"结束 {human_time(job.finished_at, empty='还没结束')}",  # 结束
                    f"耗时 {human_duration(job.duration_s)}",  # 耗时
                ]
            )
        ).classes("hp-field-help")  # 时间行

        # 参数：所有任务统一在这里展开（任务类声明的参数形状，入队时已校验过）
        if job.params:
            with ui.expansion("参数", icon="tune", value=True).classes("w-full hp-sources"):
                for item in job.params:  # 逐个参数
                    with ui.row().classes("w-full items-start gap-2 no-wrap"):
                        ui.label(item.key).classes("hp-field-name").style("min-width: 96px;")  # 名
                        ui.label(item.value).classes("hp-field-help").style(  # 值
                            "word-break: break-all;"
                        ).tooltip(item.full)  # 悬停看全文

        # 结果：任务跑完统一写进来的那句话（TaskResult.note）
        if job.result:
            ui.label(f"结果：{job.result}").classes("hp-field-name")  # 结果
        if job.error:
            ui.label(f"{job.error_label}：{job.error}" if job.error_label else job.error).classes(
                "hp-note-error"
            )  # 错误

        if not job.events:
            ui.label("这个任务没有失败或中断记录。").classes("hp-field-help")  # 空态
        for event in job.events:  # 逐条事件
            with ui.column().classes("w-full gap-0 hp-event"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.label(event.kind_label).classes("hp-field-name")  # 类型
                    if event.attempt:
                        ui.label(f"第 {event.attempt} 次").classes("hp-field-help")  # 次数
                    ui.space()  # 撑开
                    ui.label(human_time(event.at)).classes("hp-field-help")  # 时间
                if event.message:
                    ui.label(event.message).classes("hp-field-help")  # 消息
                if event.detail:
                    with ui.expansion("完整信息（报错原文）").classes("w-full hp-sources"):
                        ui.label(event.detail).classes("hp-detail-mono")  # 原文

        with ui.row().classes("hp-actions w-full justify-end"):  # 底部动作
            ui.button("关闭", on_click=close).props("flat dense no-caps").classes(
                "hp-row-button w-auto"
            )  # 关闭

    dialog, _close = custom(title=f"{job.title} · 任务 #{job.id}", content=_body)  # 统一壳（正文在卡片里）
    dialog.open()  # 打开


def render() -> None:
    """画「任务」分区。

    R2 例外（函数偏长）：任务页是"轮询 + 大量控件"的组合，且交互（暂停/批量/筛选/分页/
    组切换）都共享这块卡上的元素引用，拆成独立函数会传 6+ 个引用。内部的
    `refresh`/行画法/空态本就是独立小函数（各自 <50 行），`render` 只做"搭骨架 + 编排"。

    版式（与知识库页**同一套骨架**：卡头 → 工具栏 → 主体 → 页脚，见 §13.8）：

    - **页面级动作**（暂停队列 / 重试全部失败 / 清理已完成）放页面右上——它们动的是整个队列，
      不属于"这个列表"（PatternFly 的放置原则）；
    - **正在跑**单独一张卡、不分页：它是"现在在干什么"的答案，受泳道并发限制，永远不会多；
    - **列表卡**：工具栏（筛选在左、自动刷新在右）→ 行 → 空状态 → 页脚（区间 + 页码 + 每页条数）。
    """
    state = PageState(size=PAGE_SIZES[0])
    current: dict[str, Any] = {"group": "active"}
    auto = AutoRefresh(interval_s=REFRESH_S)
    picked: set[int] = set()  # 批量选择（跨刷新保留：行会被重画）
    page_ids: list[int] = []

    with ui.column().classes("w-full gap-4"):
        with ui.row().classes("w-full items-center gap-2"):
            status = ui.label("").classes("hp-status")  # 队列状态（标题在左栏，不再重复）
            ui.space()  # 撑开
            # 页面级动作统一放**页顶右侧**（暂停/重试/清理 + 自动刷新），列表卡里只管列表
            pause_button = ui.button("暂停队列", icon="pause").props(
                "flat dense no-caps"
            ).classes("hp-row-button w-auto").mark("task-pause")
            ui.button("重试全部失败", icon="replay", on_click=lambda: _retry_all()).props(
                "flat dense no-caps"
            ).classes("hp-row-button w-auto")
            ui.button("清理已完成", icon="cleaning_services", on_click=lambda: _clear()).props(
                "flat dense no-caps"
            ).classes("hp-row-button w-auto")
            ui.button("清理存储", icon="auto_delete", on_click=lambda: _cleanup_storage()).props(
                "flat dense no-caps"
            ).classes("hp-row-button w-auto").mark("task-cleanup").tooltip(
                "清理孤儿副本、暂存残留、合并 WAL、清历史（排成一条维护任务）"
            )
            auto_box = ui.row().classes("items-center gap-2 no-wrap")  # 自动刷新位置（页顶）
        hint = ui.label("").classes("hp-field-help")

        running_box = ui.column().classes("hp-card w-full gap-2 hp-tasks-running")

        with ui.column().classes("hp-card w-full gap-2 hp-list-card hp-tasks-list"):
            with ui.row().classes("hp-card-head w-full no-wrap"):
                ui.icon("list_alt").classes("text-base hp-card-icon")
                ui.label("任务列表").classes("hp-card-title")
                count_label = ui.label("").classes("hp-field-help")
                ui.space()  # 撑开（自动刷新已上移到页顶右侧）
            with ui.row().classes("hp-toolbar w-full items-center gap-2"):
                # 筛选：数据多的时候先分组再翻页（"进行中"是默认视角）。
                # 用**带 marker 的普通按钮**而不是 ui.toggle：toggle 的内部按钮在测试里点不动
                # （跟之前 ui.item 的坑同类），而且按钮 + 高亮类跟左栏导航的样式一致。
                filter_buttons: dict[str, Any] = {}
                for key, label in tasks_service.GROUP_LABELS:
                    button = ui.button(label, on_click=lambda group=key: _switch_group(group)).props(
                        "flat dense no-caps"
                    ).classes("hp-group-btn w-auto").mark(f"task-group-{key}")
                    filter_buttons[key] = button
                ui.space()
                ui.button("全选本页", icon="select_all", on_click=lambda: _select_page()).props(
                    "flat dense no-caps"
                ).classes("hp-row-button w-auto").mark("task-select-page")
            # 批量动作条：没选中时整条不显示（成熟后台的通行做法，省得占着地方）
            with ui.row().classes("hp-actions w-full hp-pick-bar") as pick_bar:
                pick_label = ui.label("").classes("hp-field-help")
                ui.button("批量取消", icon="close", on_click=lambda: _batch_cancel()).props(
                    "flat dense no-caps"
                ).classes("hp-row-button w-auto").mark("task-batch-cancel")
                ui.button("批量重试", icon="replay", on_click=lambda: _batch_retry()).props(
                    "flat dense no-caps"
                ).classes("hp-row-button w-auto").mark("task-batch-retry")
                ui.button("批量删除", icon="delete_outline", on_click=lambda: _batch_delete()).props(
                    "flat dense no-caps"
                ).classes("hp-row-button w-auto").mark("task-batch-delete")
                ui.button("取消选择", icon="deselect", on_click=lambda: _clear_picks()).props(
                    "flat dense no-caps"
                ).classes("hp-row-button w-auto").mark("task-batch-clear")
            items_box = ui.column().classes("w-full gap-0")
            empty = ui.label("").classes("hp-field-help")
            with ui.column().classes("hp-list-footer w-full"):
                pager_box = ui.column().classes("w-full gap-0")

    # ---------------- 动作 ----------------

    async def _retry_all() -> None:
        """重试全部失败：DB 批量更新，放线程池，别卡界面。"""
        count = await run.io_bound(tasks_service.retry_all_failed)
        if count is None:  # 取消/退出中
            return
        notify(
            f"已重新排队 {count} 条" if count else "没有失败的任务", kind="positive" if count else "info"
        )
        refresh(force=True)

    def _clear() -> None:
        """清理已完成：**破坏性操作，先确认**（和批量删除一致）。"""
        confirm(
            "清理已完成的任务记录？",
            "会删掉所有已完成 / 失败 / 已取消的记录（正在跑和排队的不动），不可恢复。",
            _do_clear,
            confirm_label="清理",
            danger=True,
            confirm_mark="task-clear-confirm",
        )

    async def _do_clear() -> None:
        """真正执行清理（DB 删除放线程池）。"""
        count = await run.io_bound(tasks_service.clear_finished)
        if count is None:  # 取消/退出中
            return
        notify(f"清理了 {count} 条历史记录", kind="positive")
        refresh(force=True)

    def _cleanup_storage() -> None:
        """清理存储：**排成一条维护任务**（不是当场做——扫目录可能很慢）。"""
        from app.services import maintenance as maintenance_service

        job_id = maintenance_service.enqueue_cleanup("manual")
        notify(f"已排队清理存储（任务 #{job_id}）", kind="positive")
        refresh(force=True)

    def toggle_pause() -> None:
        tasks_service.resume() if paused["value"] else tasks_service.pause()
        refresh(force=True)

    def _switch_group(group: str) -> None:
        current["group"] = group if group in tasks_service.STATUS_GROUPS else "active"
        state.reset()  # 换了筛选就等于换了一套页，从第一页看起
        picked.clear()  # 换了视角就别留着看不见的选择（"我到底选了什么"最烦人）
        refresh(force=True)

    def _toggle_pick(job_id: int) -> None:
        picked.discard(job_id) if job_id in picked else picked.add(job_id)
        refresh(force=True)

    def _clear_picks() -> None:
        picked.clear()
        refresh(force=True)

    def _select_page() -> None:
        """全选**本页**（不跨页累积：跨页选择最容易让人搞不清自己选了什么）。"""
        picked.update(page_ids)
        refresh(force=True)

    def _batch_cancel() -> None:
        count = tasks_service.cancel_many(picked)
        notify(
            f"已请求取消 {count} 条" if count else "选中的任务都不能取消",
            kind="warning" if count else "info",
        )
        picked.clear()
        refresh(force=True)

    def _batch_retry() -> None:
        count = tasks_service.retry_many(picked)
        notify(
            f"已重新排队 {count} 条" if count else "选中的任务都不能重试",
            kind="positive" if count else "info",
        )
        picked.clear()
        refresh(force=True)

    def _batch_delete() -> None:
        """删历史记录：破坏性操作，确认一次（在跑/排队的删不掉，服务层挡着）。"""

        async def go() -> None:
            """DB 批量删除放线程池，删完刷新。"""
            await run.io_bound(tasks_service.delete_many, picked)
            picked.clear()  # 删完清空选择
            refresh(force=True)  # 刷新

        confirm(
            f"删掉选中的 {len(picked)} 条记录？",
            "只删已完成 / 失败 / 取消的记录（正在跑和排队的不动），事件记录一起删。",
            go,
            confirm_label="删掉",
            danger=True,
            confirm_mark="task-batch-confirm",
        )

    pause_button.on_click(toggle_pause)

    # ---------------- 刷新 ----------------

    paused = {"value": False}
    seen: dict[str, Any] = {"rows": None, "pager": None}

    def rows_mark(data: tasks_service.TaskOverview) -> tuple[Any, ...]:
        return (
            data.group,
            data.paused,
            data.degraded,
            tuple(sorted(picked)),  # 选中态变了要重画（图标要跟着换）
            tuple(
                (job.id, job.status, job.done, job.total, job.note, job.error)
                for job in data.running
            ),
            tuple(
                (job.id, job.status, job.done, job.total, job.note, job.error)
                for job in data.items
            ),
        )

    def pager_mark(data: tasks_service.TaskOverview) -> tuple[Any, ...]:
        return (data.group, data.total, data.page, data.size)

    def refresh(*, force: bool = False) -> None:
        data = tasks_service.overview(group=current["group"], page=state.page, size=state.size)
        state.total = int(data.total)
        state.page = int(data.page)
        paused["value"] = bool(data.paused)
        auto.touch()  # 记一次"刚查过库"：时间戳与"自动刷新已关"的提示都靠它

        active = bool(data.stats.active) and not data.paused
        status.text = "已暂停" if data.paused else ("运行中" if data.stats.active else "空闲")
        status.classes(add="hp-status-ok") if active else status.classes(remove="hp-status-ok")
        status.update()
        count_label.text = f"{data.total} 条"
        count_label.update()
        # 批量动作条：选中才算事情做（没选中就不占地方）
        pick_label.text = f"已选 {len(picked)} 项"
        pick_label.update()
        pick_bar.set_visibility(bool(picked))
        page_ids[:] = [job.id for job in data.items]
        pause_button.text = "恢复队列" if data.paused else "暂停队列"
        pause_button.update()
        hint.text = data.hint
        hint.update()
        for key, button in filter_buttons.items():
            button.classes(add="hp-group-btn-active") if key == current["group"] else button.classes(
                remove="hp-group-btn-active"
            )

        # 正在跑：不分页（受全局并发上限约束就那么几条），内容变了才重画
        # 并发数也要进重画判据：不然"并发 1/3 → 0/3"要等下一次内容变化才刷新
        running_mark = (
            tuple((job.id, job.done, job.note) for job in data.running),
            data.concurrency.running,
            data.concurrency.limit,
        )
        if force or running_mark != seen.get("running"):
            seen["running"] = running_mark
            running_box.clear()
            with running_box:
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label(f"正在跑（{len(data.running)}）").classes("hp-card-title")
                    ui.space()
                    ui.label(
                        f"并发 {data.concurrency.running}/{data.concurrency.limit}"
                        + (" · ONNX 串行" if data.concurrency.serialized else "")
                    ).classes("hp-field-help").mark("task-concurrency")
                if not data.running:
                    ui.label("现在没有任务在跑。导入资料、重建索引时，这里会显示进度。").classes(
                        "hp-field-help"
                    )
                for job in data.running:
                    _row(job, picked=picked, on_pick=_toggle_pick)

        # 分页控件：只有页码/总数/每页条数变了才重画（否则下拉框会在用户点开时被抽走）
        marker = pager_mark(data)
        if force or marker != seen.get("pager"):
            seen["pager"] = marker
            pager_box.clear()
            with pager_box:
                render_pager(state, on_change=refresh)

        # 行：内容变了才重画
        marker = rows_mark(data)
        if force or marker != seen.get("rows"):
            seen["rows"] = marker
            items_box.clear()
            with items_box:
                for job in data.items:
                    _row(job, picked=picked, on_pick=_toggle_pick)
            empty.text = "" if data.items else _empty_text(current["group"])
            empty.update()

    def _empty_text(group: str) -> str:
        return {
            "active": "现在没有进行中的任务。",
            "failed": "没有失败的任务。",
            "finished": "还没有已完成的任务。",
            "all": "还没有任何任务。",
        }.get(group, "没有任务。")

    refresh(force=True)
    with auto_box:  # 自动刷新放工具栏右侧（回填到上面留好的位置，保证骨架顺序）
        auto.render(refresh)
