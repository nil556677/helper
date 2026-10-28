"""用户级步骤：按 group 聚合 + 步骤 id 从代码位置派生且必须唯一。"""

from __future__ import annotations  # 类型注解延迟求值

import ast  # 静态扫描源码（编译期查重复 id）
import sys  # 取调用帧（验证派生）
from pathlib import Path  # 源文件路径
from types import FrameType  # 帧类型

import pytest  # 测试框架

from app.agent.contracts import (  # 契约层
    GROUP_LABELS,  # 组名
    GROUP_ORDER,  # 组顺序
    DuplicateIdError,  # 重复 id 异常
    Namer,  # 命名器
    StageGroup,  # 分组
)
from app.agent.stages import stage  # 被测装饰器
from app.agent.steps import grouped  # 被测聚合

_APP = Path(__file__).resolve().parents[2] / "app"  # app 包根目录


def test_grouped_merges_by_group() -> None:  # 合并
    """同组步骤（语义/关键词）合成一行，不是按手写 id 查表。"""
    steps = [  # 原始
        {"id": "nodes.rag.graph.dense_node", "group": "search", "title": "查找资料", "detail": "找到 40 段", "status": "done", "ms": 10},  # 语义
        {"id": "nodes.rag.graph.sparse_node", "group": "search", "title": "查找资料", "detail": "找到 50 段", "status": "done", "ms": 20},  # 关键词
        {"id": "nodes.rag.graph.rerank_node", "group": "filter", "title": "筛选重点", "detail": "选出 6 段", "status": "done", "ms": 30},  # 精排
    ]  # 列表结束
    result = grouped(steps)  # 聚合
    assert [item["group"] for item in result] == ["search", "filter"]  # 组顺序
    assert [item["title"] for item in result] == ["查找资料", "筛选重点"]  # 组名来自契约层
    assert result[0]["ms"] == 30  # 耗时累加
    assert "找到 40 段" in result[0]["detail"]  # 明细保留
    assert "找到 50 段" in result[0]["detail"]  # 明细保留


def test_grouped_marks_failed_and_orders() -> None:  # 失败与顺序
    """一组里有失败就整组标失败；输出按契约层的固定顺序。"""
    steps = [  # 原始（故意打乱顺序）
        {"group": "generate", "title": "生成回答", "status": "done", "ms": 5},  # 生成
        {"group": "prepare", "title": "理解问题", "status": "failed", "ms": 1},  # 准备（失败）
    ]  # 列表结束
    result = grouped(steps)  # 聚合
    assert [item["group"] for item in result] == ["prepare", "generate"]  # 固定顺序
    assert result[0]["status"] == "failed"  # 失败透传


def test_grouped_keeps_steps_without_group() -> None:  # 旧数据
    """没有 group 的旧步骤原样保留（不猜、不吞）。"""
    result = grouped([{"title": "旧步骤", "detail": "", "status": "done"}])  # 聚合
    assert result[0]["title"] == "旧步骤"  # 保留


def test_group_contract_covers_all_groups() -> None:  # 契约自洽
    """顺序表与组名表必须一一对应，否则界面会少画一组。"""
    assert set(GROUP_ORDER) == set(StageGroup)  # 顺序覆盖全部
    assert set(GROUP_LABELS) == set(StageGroup)  # 组名覆盖全部


def test_stage_id_derived_from_caller() -> None:  # id 派生
    """id = 模块.函数(+短名)，作者不写 id。"""
    with stage("第一步", group=StageGroup.SEARCH) as info:  # 主步骤
        assert info.stage_id.endswith("test_agent_steps.test_stage_id_derived_from_caller")  # 自动派生
    with stage("第二步", group=StageGroup.SEARCH, name="second") as info:  # 同函数第二个步骤
        assert info.stage_id.endswith("test_stage_id_derived_from_caller.second")  # 短名后缀


def test_stage_rejects_duplicate_id_in_one_function() -> None:  # 重复 id
    """同一函数里两处都不写 name，就是真撞车：第一次跑到就抛。"""
    with stage("甲", group=StageGroup.PREPARE):  # 第一处
        pass  # 空体
    with pytest.raises(DuplicateIdError):  # 期望异常
        stage("乙", group=StageGroup.PREPARE)  # 第二处（同函数同 id）


def test_namer_rejects_lambda_frame() -> None:  # 非法位置
    """匿名函数/模块级别不配当节点，派生时直接报错。"""
    frame: FrameType = (lambda: sys._getframe(0))()  # 拿到 <lambda> 的帧
    with pytest.raises(ValueError):  # 期望异常
        Namer.from_frame(frame)  # 派生


def _stage_calls() -> list[tuple[str, str, bool]]:  # 静态扫描所有 stage() 调用
    """返回 [(派生 id, 相对位置, 是否声明了 group)]；不执行代码，纯读 AST。"""
    found: list[tuple[str, str, bool]] = []  # 结果
    for path in sorted(_APP.rglob("*.py")):  # 逐个源文件
        tree = ast.parse(path.read_text(encoding="utf-8"))  # 解析
        module = ".".join(path.relative_to(_APP.parent).with_suffix("").parts)  # 模块名
        relative = str(path.relative_to(_APP.parent)).replace("\\", "/")  # 相对位置
        for func, call in _walk_calls(tree):  # 逐个调用
            found.append(  # 记录
                (
                    Namer.stage_id(Namer.node_id(module, func), _short_name(call)),  # 派生 id
                    f"{relative}:{call.lineno}",  # 位置
                    any(keyword.arg == "group" for keyword in call.keywords),  # 是否声明分组
                )
            )  # 记录结束
    return found  # 返回


def _walk_calls(tree: ast.AST) -> list[tuple[str, ast.Call]]:  # 找出 stage() 调用
    """返回 [(所在函数名, 调用节点)]；嵌套函数取最近一层。"""
    result: list[tuple[str, ast.Call]] = []  # 结果

    def visit(node: ast.AST, func: str) -> None:  # 递归
        """进入函数体时更新「最近一层函数名」。"""
        for child in ast.iter_child_nodes(node):  # 逐个子节点
            name = func  # 继承当前函数名
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):  # 进入函数
                name = child.name  # 换成自己的名字
            if _is_stage_call(child):  # 是 stage() 调用
                result.append((name, child))  # 记录
            visit(child, name)  # 继续往下

    visit(tree, "")  # 从模块开始
    return result  # 返回


def _is_stage_call(node: ast.AST) -> bool:  # 判断是不是 stage(...)
    """只认直接调用名为 stage 的写法。"""
    return (
        isinstance(node, ast.Call)  # 是调用
        and isinstance(node.func, ast.Name)  # 名字调用
        and node.func.id == "stage"  # 名字是 stage
    )  # 返回判断


def _short_name(call: ast.Call) -> str:  # 取 name= 短名
    """只认字符串常量；非常量交给运行期注册表兜底。"""
    for keyword in call.keywords:  # 逐个关键字
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):  # name="xxx"
            return str(keyword.value.value)  # 短名
    return ""  # 没有短名


def test_stage_ids_unique_across_app() -> None:  # 编译期唯一性
    """全仓 stage() 派生的 id 必须唯一——两处撞车在 CI 就红，不用等跑起来。"""
    calls = _stage_calls()  # 静态派生全部 id
    assert calls, "没扫到任何 stage() 调用，扫描逻辑失效了"  # 防空扫
    seen: dict[str, str] = {}  # id -> 第一次出现的位置
    for stage_id, where, _ in calls:  # 逐个
        assert stage_id not in seen, f"重复的步骤 id：{stage_id}（{seen.get(stage_id)} 与 {where}）"  # 撞车
        seen[stage_id] = where  # 记录


def test_every_stage_declares_group() -> None:  # 分组在调用处声明
    """除「收尾」这种纯埋点步骤外，每处 stage() 都必须写 group（否则界面上看不到）。"""
    missing = [where for _, where, has_group in _stage_calls() if not has_group]  # 没声明的
    allowed = ("app/agent/nodes/finalize/",)  # 只有收尾允许不声明
    assert all(where.startswith(allowed) for where in missing), f"这些步骤没声明 group：{missing}"  # 检查
