"""设置 · 检索追踪：总览（KPI 条 + 失败原因 + 最近）+ 列表（筛选/分页）+ 详情。

**设计（资深 UI 视角重做）**：
- 去掉了单独的页头标题 + 大段说明——左栏已经标了分区名，那两行只是白占高度；
- 顶部一条**工具条**同时放下页签和自动刷新，少一行标题；
- 总览不再并排放三张各自带标题的范围卡，改成**范围切换 + 六个 KPI 格**，一眼扫完；
- 详情弹窗默认 460px 太窄，多列元信息挤得没法看——这里放宽到 760px 重新排版。
"""

from __future__ import annotations  # 类型注解延迟求值

import json  # 复制详情 / 路由解析
from typing import Any  # 任意值

from nicegui import ui  # 界面

from app.services import trace as trace_service  # 视图模型
from app.ui.components.auto_refresh import AutoRefresh  # 自动刷新
from app.ui.components.card import card_head  # 卡片头（图标 + 标题 + 右侧）
from app.ui.components.dialog import custom, notify  # 弹窗 / 通知
from app.ui.components.format import human_duration, human_time  # 时间格式化
from app.ui.components.pager import PageState, render_pager  # 分页
from app.ui.components.status import RUN_STATUS_LABELS  # 状态文案（唯一出处）

#: 状态中文名（唯一出处见 app/ui/components/status.py；此处只做再导出兼容）。
_STATUS_LABELS = RUN_STATUS_LABELS

#: 总览的范围切换。
_RANGES = ((1, "今天"), (7, "近 7 天"), (30, "近 30 天"))  # (天, 文案)

#: 列表的时间筛选（默认跟总览的近 7 天一致，不另起一套心智）。
_DAYS = (("今天", 1), ("7 天", 7), ("30 天", 30))  # 下拉项

#: 列表的状态筛选。
_STATUS = (("全部", "all"), ("成功", "ok"), ("失败", "error"), ("等待", "waiting"), ("已取消", "cancelled"))  # 下拉项

#: 范围 → 文案的便捷查找。
_RANGE_LABEL = dict(_RANGES)  # 范围 → 文案


def _num(value: int) -> str:  # 数字缩写
    """1234 → 1.2k，方便 KPI 格 / 列表里放得下。"""
    number = int(value or 0)  # 取整
    return f"{number / 1000:.1f}k" if number >= 1000 else str(number)  # 缩写


def _ms(value: int | None) -> str:  # 毫秒文案
    """没有值显示 —，有值转成 1.2s。"""
    return human_duration(value / 1000) if value is not None else "—"  # 转换


def _meta(run: Any) -> dict[str, Any]:  # 解析元数据
    try:  # 容错
        return json.loads(run.metadata_json or "{}")  # 解析
    except Exception:  # 坏数据
        return {}  # 空


#: Span 类型 → 配色（节点蓝 / 模型紫 / 检索绿 / 工具橙 / 其它灰）。
_KIND_COLOR = {  # 配色表
    "node": "#3182ce",  # 节点 蓝
    "llm": "#805ad5",  # 模型 紫
    "retrieval": "#38a169",  # 检索 绿
    "tool": "#dd6b20",  # 工具 橙
}  # 结束


def _span_scale(spans: list[Any]) -> tuple[float, float]:  # 时间轴起止（秒）
    """所有 span 的最小开始 → 最大结束，用来把条按真实时间对齐。"""
    starts = [float(s.started_at) for s in spans if s.started_at]  # 起点集
    if not starts:  # 没有
        return 0.0, 1.0  # 兜底
    ends = [float(s.started_at) + float(s.duration_ms or 0) / 1000 for s in spans if s.started_at]  # 终点集
    return min(starts), max(ends)  # 起止


def _span_json(span: Any) -> None:  # 该步全量 JSON（弹窗）
    """点击时间条右边的 JSON：把该 span 的 input/output 全量弹出来。"""
    if not getattr(span, "output_json", None):  # 没有输出
        notify("该步没有输出数据", kind="info")  # 提示
        return  # 结束
    with ui.dialog() as dlg, ui.card().classes("w-full"):  # 弹窗
        ui.label(str(span.name or "—")).classes("hp-card-title")  # 标题
        ui.code(  # 全量 JSON
            json.dumps(json.loads(span.output_json or "{}"), ensure_ascii=False, indent=2),  # 输出
            language="json",  # 高亮
        ).classes("w-full q-pa-sm")  # 样式
    dlg.open()  # 打开


def _draw_waterfall(spans: list[Any]) -> None:  # 执行瀑布
    """横向时间条：宽度 ∝ 耗时、起点按时间对齐、颜色按类型；点 JSON 看该步。"""
    if not spans:  # 无
        return  # 跳过
    ordered = sorted(spans, key=lambda s: float(s.started_at or 0))  # 按时间排
    start, end = _span_scale(ordered)  # 区间
    total = max(1e-6, end - start)  # 总时长
    with ui.expansion(f"执行瀑布（{len(ordered)} 段）· 横条 ∝ 耗时", icon="timeline", value=True).classes("w-full hp-sources"):
        for span in ordered:  # 逐段
            dur = float(span.duration_ms or 0) / 1000  # 秒
            left = (float(span.started_at or 0) - start) / total * 100.0  # 起点 %
            width = max(0.5, dur / total * 100.0)  # 宽度 %
            color = _KIND_COLOR.get(span.kind or "", "#9ca3af")  # 类型色
            with ui.row().classes("w-full items-center gap-2 no-wrap"):  # 一行
                ui.label(str(span.kind or "—")).classes("hp-field-help").style("width: 52px;")  # 类型
                ui.label(str(span.name or "—")).classes("hp-field-name").style(  # 名
                    "width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                )  # 名
                with ui.element("div").classes("flex-1").style(  # 轨道
                    "height: 12px; background: #e5e7eb; border-radius: 3px; position: relative;"
                ):  # 轨道
                    ui.element("div").style(  # 条
                        f"height: 12px; border-radius: 3px; background: {color};",  # 样式
                        f"position: absolute; left: {left:.1f}%; width: {width:.1f}%;",  # 位置
                    )  # 条
                ui.label(_ms(span.duration_ms)).classes("hp-field-help").style("width: 60px;")  # 耗时
                ui.button("JSON", on_click=lambda _, s=span: _span_json(s)).props("flat dense no-caps")  # 该步 JSON


#: 决策链各阶段的人话标签。
_STAGE_LABEL = {  # 标签表
    "preprocessed": "输入 / 预处理",  # 归一/实体/时间/flags
    "route": "路由决策",  # RouteDecision
    "semantic_recall": "语义召回",  # 候选 + 三态
    "intent": "意图",  # IntentDecision
    "triage": "分诊",  # TriageDecision
    "verify": "校验",  # VerifyResult
    "clarify_options": "澄清选项",  # 澄清
}  # 结束


def _route_label(run: Any) -> str:  # 路由文案
    """从 route_json 里取个能读的短标签（解析失败就回退 rag）。"""
    try:  # 解析 JSON
        route = json.loads(run.route_json or "{}")  # 路由数据
    except Exception:  # 解析失败
        route = {}  # 空
    mode = str(route.get("mode") or "rag")  # 模式
    reason = str(route.get("reason") or "")  # 可选的说明
    return f"{mode} · {reason}" if reason else mode  # 组装


def _status_chip_class(status: str) -> str:  # 状态胶囊的样式
    """成功绿点、失败红点、其它中性灰点（已取消是用户主动停，不标红）。"""
    if status == "ok":  # 成功
        return "hp-status hp-status-ok"  # 绿
    if status in ("error", "failed"):  # 失败
        return "hp-status hp-status-bad"  # 红
    return "hp-status"  # 中性


def _kpi_tile(value: str, name: str) -> Any:  # 一个 KPI 格
    """返回值的标签（`.v`），供后面刷新数字用。"""
    tile = ui.column().classes("hp-stat-tile gap-0")  # 格子
    with tile:  # 内容
        label = ui.label(value).classes("v")  # 大数字
        ui.label(name).classes("n")  # 小标题
    return label  # 句柄


def _recent_row(row: trace_service.TraceRow) -> None:  # 总览里的最近一条
    """紧凑一行：问题 + 状态胶囊 + 耗时 + 时间（点整行看详情）。"""
    with _row_container(row.id):  # 可点
        ui.label(row.question or "（空）").classes("hp-col-name").style("flex: 1;")  # 问题
        ui.label(_ms(row.duration_ms)).classes("hp-field-help").style("width: 66px;")  # 耗时
        ui.label(_STATUS_LABELS.get(row.status, row.status)).classes(  # 状态
            _status_chip_class(row.status)  # 胶囊样式
        ).style("width: 58px;")  # 宽度
        ui.label(human_time(row.at)).classes("hp-field-help").style("width: 78px;")  # 时间


def _list_row(row: trace_service.TraceRow) -> None:  # 列表里的完整一行
    """列表列：时间 / 问题 / 路由 / 命中 / 耗时 / 首字 / tokens / 状态。"""
    with _row_container(row.id):  # 可点
        ui.label(human_time(row.at)).classes("hp-field-help").style("width: 88px;")  # 时间
        ui.label(row.question or "（空）").classes("hp-col-name").style("flex: 1; min-width: 180px;")  # 问题
        ui.label(row.route_label).classes("hp-field-help").style("width: 80px;")  # 路由
        ui.label(f"{row.hit_count}").classes("hp-field-help").style("width: 44px;")  # 命中数
        ui.label(_ms(row.duration_ms)).classes("hp-field-help").style("width: 60px;")  # 总耗时
        ui.label(_ms(row.ttft_ms)).classes("hp-field-help").style("width: 60px;")  # 首字耗时
        ui.label(_num(row.tokens)).classes("hp-field-help").style("width: 52px;")  # tokens
        status_text = _STATUS_LABELS.get(row.status, row.status)  # 文案
        if row.error_kind:  # 有错误类型：状态附上错误类型，一眼知道死在哪
            status_text = f"{status_text}·{row.error_kind}"  # 合成
        ui.label(status_text).classes(_status_chip_class(row.status)).style(  # 状态胶囊
            "max-width: 132px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
        )  # 样式


def _row_container(run_id: str) -> Any:  # 可点的一行
    """整行可点开详情（比角落一个小按钮好点）。"""
    row = ui.row().classes("hp-row-item w-full items-center gap-2 no-wrap px-3").style(
        "cursor: pointer; min-height: 34px;"
    )  # 行
    row.on("click", lambda _=None, rid=run_id: _detail(rid))  # 点击进详情
    return row  # 返回容器（配合 with 用）


def _detail(run_id: str) -> None:  # 详情弹窗
    """点开看这一轮的完整过程：头部 + KPI + 时间线 / 命中 / 事件 / 回答。"""
    view = trace_service.detail(run_id)  # 取详情
    if view is None:  # 已被清理
        notify("这条追踪已经不在库里了", kind="info")  # 提示
        return  # 结束
    run = view.run  # 运行摘要

    def _body(close: Any) -> None:  # 正文（画在卡片里）
        """层级：状态+问题 → 元信息 → KPI → 查询链/错误 → 折叠分块 → 底部动作。"""
        with ui.column().classes("w-full gap-3"):  # 整块正文
            with ui.row().classes("w-full items-center gap-2 no-wrap"):  # 头部行
                ui.label(_STATUS_LABELS.get(run.status, run.status)).classes(  # 状态胶囊
                    _status_chip_class(run.status)  # 颜色
                )  # 胶囊
                ui.label(run.question or "（空）").classes("hp-field-name").style(  # 问题
                    "font-size: 15px; line-height: 22px;"
                )  # 问题样式
            ui.label(  # 元信息一行
                f"{human_time(run.started_at)} · 模型 {run.model or '—'} · 路由 {_route_label(run)}"
            ).classes("hp-field-help")  # 元信息
            with ui.element("div").classes("hp-stats w-full"):  # KPI 条
                _kpi_tile(_ms(run.duration_ms), "总耗时")  # 总耗时
                _kpi_tile(_ms(run.ttft_ms), "首字")  # 首字
                _kpi_tile(  # token 用量
                    f"{_num(run.prompt_tokens)}+{_num(run.completion_tokens)}" if run.total_tokens else "—", "tokens"
                )  # 输入+输出
                _kpi_tile(str(len(view.hits)), "命中")  # 命中数
                _kpi_tile(str(len(view.spans)), "节点")  # 时间线段数
            _draw_waterfall(view.spans)  # 执行瀑布
            if run.normalized_query or run.rewritten_query:  # 有查询归一/改写
                ui.label(  # 查询链
                    f"查询：{run.normalized_query or run.question}"  # 归一
                    f" → {run.rewritten_query or '（未改写）'}"  # 改写
                ).classes("hp-field-help")  # 样式
            try:  # 记忆压缩错误（持久化 history + 本轮即时，合并去重）
                history = trace_service.compaction_errors(run.conversation_id)  # 历史 trace_events
                current = {}  # 本轮进程内
                try:  # 读进程 last_errors（即时，重启前的本轮）
                    from app.agent.memory.summarizer import last_errors  # 压缩失败记录

                    current = {c: r for c, r in last_errors().items() if c == run.conversation_id}  # 本会话
                except Exception:  # 读失败
                    current = {}  # 空
                combined = [  # 合并：历史优先，进程本轮补充且不与历史重复
                    {"conversation_id": h["conversation_id"], "error": h["error"], "at": h["at"]}
                    for h in history
                ]  # 历史
                known = {h["error"] for h in combined}  # 已含的错误
                for conv, reason in current.items():  # 进程本轮
                    if reason not in known:  # 未在历史
                        combined.append({"conversation_id": conv, "error": reason, "at": 0.0})  # 补
                if combined:  # 有错误
                    with ui.expansion(f"记忆压缩错误（{len(combined)}）", icon="memory", value=False).classes("w-full hp-sources"):
                        for entry in combined:  # 逐条
                            with ui.row().classes("w-full items-start gap-2 no-wrap hp-note-error"):  # 一行
                                ui.icon("error_outline").classes("text-base")  # 图标
                                when = human_time(entry["at"]) if entry["at"] else "本轮"  # 时间或本轮
                                ui.label(f"{when} · {entry['conversation_id']}：{entry['error']}").classes("hp-note-error").style(  # 详情
                                    "white-space: pre-wrap; word-break: break-word;"
                                )  # 样式
            except Exception:  # 兜底
                pass  # 不阻断详情
            try:  # 决策链/步骤：metadata 有坏数据也绝不能让整个详情空白
                metadata = _meta(run)  # 元数据
                decisions = metadata.get("decisions") or {}  # 决策链
                steps = metadata.get("steps") or []  # 逐步
                if decisions or steps:  # 有排障数据
                    with ui.expansion("决策链 / 步骤", icon="account_tree", value=True).classes("w-full hp-sources"):
                        for key, label in _STAGE_LABEL.items():  # 逐阶段
                            value = decisions.get(key)  # 该阶段决策
                            if not value:  # 没有
                                continue  # 跳过
                            with ui.expansion(f"{label}").classes("w-full hp-step"):  # 折叠
                                ui.code(json.dumps(value, ensure_ascii=False, indent=2)).classes("w-full")  # 原始 JSON
                        if steps:  # 逐步
                            with ui.expansion(f"逐步（节点做了什么，{len(steps)}）").classes("w-full hp-step"):
                                for s in steps:  # 逐条
                                    with ui.row().classes("w-full items-start gap-2 no-wrap"):  # 一行
                                        ui.label(str(s.get("node") or "")).classes("hp-field-name").style("width: 210px;")  # 节点
                                        ui.code(str(s.get("update") or "")[:500]).classes("w-full q-pa-sm")  # 返回 update
            except Exception as exc:  # 兜底
                ui.label(f"决策链渲染跳过：{exc}").classes("hp-field-help")  # 提示
            if run.error_detail:  # 有错误
                with ui.row().classes("w-full items-start gap-2 no-wrap hp-note-error"):  # 错误行
                    ui.icon("error_outline").classes("text-base")  # 图标
                    ui.label(f"{run.error_kind or '错误'}：{run.error_detail}").classes(  # 错误原文
                        "hp-note-error"
                    ).style("white-space: pre-wrap; word-break: break-word;")  # 换行
            if not (view.spans or view.hits or view.events or run.answer):  # 什么都没有
                ui.label("这一轮没有留下过程数据（可能刚创建或已被清理）。").classes("hp-field-help")  # 兜底

            if view.spans:  # 时间线
                with ui.expansion(f"时间线（{len(view.spans)} 段）", icon="timeline").classes("w-full hp-sources"):
                    for span in view.spans:  # 逐段
                        with ui.row().classes("w-full items-center gap-2 no-wrap"):  # 一行
                            ui.label(span.name or "—").classes("hp-field-name").style("width: 168px;")  # 名字
                            ui.label(_ms(span.duration_ms)).classes("hp-field-help").style("width: 68px;")  # 耗时
                            ui.label(span.status).classes("hp-field-help").style("width: 60px;")  # 状态
                            ui.space()  # 撑开
                            if span.output_json and span.output_json != "{}":  # 有输出摘要
                                ui.label(span.output_json[:140]).classes("hp-field-help").style(
                                    "flex: 1; overflow: hidden; text-overflow: ellipsis;"
                                )  # 摘要

            if view.hits:  # 命中
                with ui.expansion(f"命中（{len(view.hits)} 条）", icon="search").classes("w-full hp-sources"):
                    for hit in view.hits:  # 逐条
                        with ui.row().classes("w-full items-baseline gap-2 no-wrap"):  # 一行
                            ui.label(f"{hit.stage}#{hit.rank}").classes("hp-field-help").style("width: 92px;")  # 阶段名次
                            ui.label(f"{hit.score:.3f}" if hit.score is not None else "—").classes(  # 分数
                                "hp-field-help"
                            ).style("width: 60px;")  # 分数宽
                            ui.label(hit.source_name or "资料").classes("hp-field-name").style("width: 130px;")  # 来源
                            ui.label((hit.snippet or hit.text)[:150]).classes("hp-field-help").style(  # 正文
                                "flex: 1; overflow: hidden; text-overflow: ellipsis;"
                            )  # 摘要

            if view.events:  # 事件
                with ui.expansion(f"事件（{len(view.events)} 条）", icon="bolt").classes("w-full hp-sources"):
                    for event in view.events:  # 逐条
                        ui.label(f"[{event.level}] {event.code} {event.message}").classes(  # 一行
                            "hp-note-error" if event.level == "error" else "hp-field-help"
                        )  # 样式

            if run.answer:  # 回答
                with ui.expansion("回答", icon="article", value=True).classes("w-full hp-sources"):
                    ui.markdown(run.answer).classes("hp-markdown w-full q-pa-sm")  # 渲染

            with ui.row().classes("w-full justify-end gap-2 pt-1"):  # 底部动作
                ui.button("复制 JSON", icon="content_copy", on_click=lambda: _copy_json(run_id)).props(  # 复制
                    "flat dense no-caps"
                )  # 弱化
                ui.button("关闭", on_click=close).props("flat dense no-caps")  # 关闭

    dialog, _close = custom(title="检索追踪 · 详情", content=_body, width="760px")  # 统一壳（放宽宽度）
    dialog.open()  # custom() 只建壳，不自动打开


def _copy_json(run_id: str) -> None:  # 复制 JSON
    """把这一轮的完整数据拷到剪贴板（排障用）。"""
    data = trace_service.export_json(run_id)  # 导出
    if data is None:  # 找不到
        notify("找不到这条追踪", kind="info")  # 提示
        return  # 结束
    ui.clipboard.write(json.dumps(data, ensure_ascii=False, indent=2))  # 写剪贴板
    notify("已复制 JSON 到剪贴板", kind="positive")  # 提示


def render() -> None:  # 画分区
    """画「检索追踪」：顶部工具条（页签+自动刷新）+ 总览 / 列表。"""
    state = PageState(size=20)  # 列表分页
    filters: dict[str, Any] = {"days": 7, "status": "all", "query": "", "only_search": False}  # 列表筛选
    last: dict[str, Any] = {"errors": None, "recent": None, "list": None, "pager": None, "kpi": False}  # 内容签名
    scope: dict[str, int] = {"days": 7}  # 总览范围
    kpi: dict[str, Any] = {}  # KPI 值标签句柄
    auto = AutoRefresh(interval_s=1.0, idle_interval_s=3.0, idle_check=trace_service.has_running)  # 自动刷新

    def _refresh_kpi() -> None:  # 填 KPI 条
        """按当前范围算统计并写进六个格子（成功率单独算百分点）。"""
        stats = trace_service.stats(scope["days"])  # 统计
        total = int(stats.total)  # 总数
        rate = f"{stats.ok / total * 100:.0f}%" if total else "—"  # 成功率
        values = {  # 六格值
            "total": str(total),  # 总数
            "rate": rate,  # 成功率
            "failed": str(stats.failed),  # 失败
            "p50": _ms(stats.p50_ms),  # 中位耗时
            "p95": _ms(stats.p95_ms),  # P95
            "tokens": _num(stats.tokens),  # token 用量
        }  # 结束
        for key, label in kpi.items():  # 逐格
            assert key in values  # 签名对得上（纯防御）
            label.text = values[key]  # 写值
            label.update()  # 刷新
        scope_hint.text = f"{_RANGE_LABEL.get(scope['days'], '近 7 天')}的调用概况"  # 范围提示
        scope_hint.update()  # 刷新

    kpi_names = {  # KPI 名字
        "total": "总轮次",  # 总数
        "rate": "成功率",  # 成功占比
        "failed": "失败",  # 失败数
        "p50": "中位耗时",  # P50
        "p95": "P95 耗时",  # P95
        "tokens": "token 用量",  # 用量
    }  # 结束

    def _set_scope(days: int) -> None:  # 切总览范围
        """换范围只重算 KPI（失败原因固定看近 7 天，不跟着变）。"""
        scope["days"] = days  # 记录
        for btn in scope_btns:  # 刷新高亮
            active = bool(btn["days"] == days)  # 是否当前
            btn["el"].classes(  # 切换 active 样式
                add="hp-group-btn-active" if active else "",  # 加
                remove="" if active else "hp-group-btn-active",  # 去
            )  # 结束
            btn["el"].update()  # 刷新
        _refresh_kpi()  # 重算

    def _refresh() -> None:  # 刷新
        """总览与列表一起刷新；内容没变就不重建 DOM（避免每秒闪 + tooltip 丢锚点）。"""
        try:  # 单点失败不要拖垮整页
            _refresh_kpi()  # KPI 一直更新（数字小，重建便宜）

            errors = trace_service.error_top(7, 5)  # 失败原因 Top5（近 7 天）
            error_sig = tuple(errors)  # 变化签名
            if error_sig != last["errors"]:  # 变了才重建
                last["errors"] = error_sig  # 记下
                failure_box.clear()  # 清
                with failure_box:  # 重建
                    if not errors:  # 没有失败
                        ui.label("近 7 天没有失败记录。").classes("hp-field-help")  # 空态
                    for kind, count in errors:  # 逐条
                        with ui.row().classes("w-full items-center gap-2 no-wrap hp-row-item px-2"):  # 一行
                            ui.label(kind or "未知").classes("hp-note-error").style("flex: 1;")  # 类型
                            ui.label(f"{count} 次").classes("hp-field-help")  # 次数

            recent_rows, _total = trace_service.page(days=30, size=6)  # 最近 6 条
            recent_sig = tuple((row.id, row.status, row.duration_ms) for row in recent_rows)  # 签名
            if recent_sig != last["recent"]:  # 变了才重建
                last["recent"] = recent_sig  # 记下
                recent_box.clear()  # 清
                with recent_box:  # 重建
                    if not recent_rows:  # 空
                        ui.label("还没有追踪记录；发一轮提问后这里会出现。").classes("hp-field-help")  # 空态
                    for row in recent_rows:  # 逐条
                        _recent_row(row)  # 画

            filters.update(  # 收集列表筛选
                days=int(days.value or 7),  # 时间
                status=str(status.value or "all"),  # 状态
                query=str(query.value or ""),  # 搜索
                only_search=bool(only.value),  # 只看有检索
            )  # 结束
            rows, total = trace_service.page(  # 查列表
                days=filters["days"],  # 时间
                status=filters["status"],  # 状态
                query=filters["query"],  # 搜索
                only_search=filters["only_search"],  # 只看有检索
                page_no=state.page,  # 页码
                size=state.size,  # 每页
            )  # 查询结束
            state.total = total  # 总数
            state.clamp()  # 夹住页码
            list_sig = tuple((row.id, row.status, row.duration_ms, row.tokens) for row in rows)  # 签名
            if list_sig != last["list"]:  # 变了才重建
                last["list"] = list_sig  # 记下
                list_box.clear()  # 清
                with list_box:  # 重建
                    if not rows:  # 空
                        ui.label("没有符合条件的记录。").classes("hp-field-help")  # 空态
                    for row in rows:  # 逐条
                        _list_row(row)  # 画
            pager_sig = (state.page, state.total, state.size)  # 分页签名
            if pager_sig != last["pager"]:  # 变了才重建
                last["pager"] = pager_sig  # 记下
                pager_box.clear()  # 清
                with pager_box:  # 重建
                    render_pager(state, on_change=_refresh, noun="条")  # 分页
            auto.touch()  # 记一次刷新
        except Exception as exc:  # 异常
            auto.mark_failed()  # 标记
            notify(f"追踪刷新失败：{exc}", kind="negative")  # 提示

    def _reset() -> None:  # 筛选变化
        """回到第一页再刷新。"""
        state.reset()  # 重置页码
        _refresh()  # 刷新

    # ---- 布局 ----
    with ui.column().classes("w-full gap-3"):  # 页面
        # 顶部工具条：页签（左）+ 自动刷新（右）。不再放页头大标题——左栏已标了分区名。
        with ui.row().classes("w-full items-center gap-2 no-wrap hp-toolbar"):
            with ui.tabs().classes("hp-tabs").props("no-caps dense align=left") as tabs:  # 两个视图
                ui.tab("总览", icon="insights")  # 总览
                ui.tab("列表", icon="table_rows")  # 列表
            ui.space()  # 撑开
            auto.render(lambda: _refresh())  # 自动刷新（两个视图共用一份）

        with ui.tab_panels(tabs, value="总览").classes("w-full"):  # 面板
            with ui.tab_panel("总览"), ui.column().classes("w-full gap-3"):  # 总览列
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):  # 范围切换行
                        ui.label("范围").classes("hp-field-help")  # 标签
                        scope_btns = [  # 三个胶囊按钮（默认高亮近 7 天）
                            {
                                "days": days,  # 范围（天数）
                                "el": ui.button(  # 按钮
                                    label, on_click=lambda _d=days: _set_scope(_d)  # 点击切换
                                ).props("flat dense no-caps").classes(  # 样式
                                    "hp-group-btn" + (" hp-group-btn-active" if days == 7 else "")  # 默认高亮
                                ),  # 结束
                            }
                            for days, label in _RANGES  # _RANGES 是 (天数, 文案)，逐个解包
                        ]  # 结束
                        ui.space()  # 撑开
                        scope_hint = ui.label("").classes("hp-field-help")  # 范围说明
                    with ui.element("div").classes("hp-stats w-full"):  # KPI 条
                        for key, name in kpi_names.items():  # 六格
                            kpi[key] = _kpi_tile("—", name)  # 建格并记句柄
                    with ui.row().classes("w-full items-start gap-3 flex-wrap"):  # 两卡并排
                        with ui.column().classes("hp-card gap-2 flex-1").style("min-width: 240px;"):  # 失败原因
                            card_head("warning_amber", "失败原因", "近 7 天")  # 卡片头
                            failure_box = ui.column().classes("w-full gap-1")  # 列表容器
                        with ui.column().classes("hp-card gap-2 flex-1").style("min-width: 320px;"):  # 最近
                            card_head("history", "最近提问", "点一行看详情")  # 卡片头
                            recent_box = ui.column().classes("w-full gap-1")  # 列表容器

            with ui.tab_panel("列表"), ui.column().classes("w-full gap-3"):  # 列表列
                    with ui.row().classes("w-full items-center gap-2 no-wrap flex-wrap"):  # 筛选行
                        days = ui.select({value: label for label, value in _DAYS}, value=filters["days"], label="时间").props(  # 时间
                            "dense outlined"
                        ).classes("w-28")  # 时间
                        status = ui.select({value: label for label, value in _STATUS}, value="all", label="状态").props(  # 状态
                            "dense outlined"
                        ).classes("w-28")  # 状态
                        only = ui.switch("只看有检索", value=False).props("dense")  # 只看有检索
                        query = ui.input(placeholder="搜索问题…").props("dense outlined clearable").classes("w-56")  # 搜索
                    with ui.element("div").classes("hp-card gap-0 px-2 py-2"):  # 表格卡
                        with ui.row().classes("w-full items-center gap-2 no-wrap px-3").style(  # 表头
                            "opacity: .65; min-height: 28px;"
                        ):  # 表头
                            ui.label("时间").classes("hp-field-help").style("width: 88px;")  # 列
                            ui.label("问题").classes("hp-field-help").style("flex: 1;")  # 列
                            ui.label("路由").classes("hp-field-help").style("width: 80px;")  # 列
                            ui.label("命中").classes("hp-field-help").style("width: 44px;")  # 列
                            ui.label("耗时").classes("hp-field-help").style("width: 60px;")  # 列
                            ui.label("首字").classes("hp-field-help").style("width: 60px;")  # 列
                            ui.label("tokens").classes("hp-field-help").style("width: 52px;")  # 列
                            ui.label("状态").classes("hp-field-help").style("flex: 1; max-width: 132px;")  # 列
                        list_box = ui.column().classes("w-full gap-0")  # 行容器
                    pager_box = ui.column().classes("w-full pt-1")  # 分页容器

    # ---- 事件绑定 ----
    days.on_value_change(lambda _: _reset())  # 时间
    status.on_value_change(lambda _: _reset())  # 状态
    only.on_value_change(lambda _: _reset())  # 只看有检索
    query.on("keydown.enter", lambda _: _reset())  # 回车搜索

    _refresh()  # 首次渲染
