"""Skill 注册表：发现(扫描 skill.yaml) + 校验(交叉 catalog、fail-fast) + 门控(默认关) + 组装(build_react)。

- 一切 skill 最终以「工具 + 提示」呈现给执行层（ReAct / plan+execute run_expert），**调用走工具**；
- `EXTRA_TOOLS` 是给 MCP(P1) 预留的可注入工具缝：skill 的 role 可引用本地或 MCP 导入的工具。
"""

from __future__ import annotations  # 延迟求值注解

import os  # 门控环境变量
from pathlib import Path  # 路径
from typing import Any  # 任意值

from app.agent.skills.models import SkillManifest  # 模型

__all__ = ["EXTRA_TOOLS", "assemble", "discover", "enabled", "resolve_tools", "validate"]  # 导出

#: MCP 等扩展可注入的额外工具：name -> tool（P1 接入点；skill 引用的工具可来自这里）。
EXTRA_TOOLS: dict[str, Any] = {}  # 注入缝


def skill_dirs(extra: list[str] | None = None) -> list[Path]:  # skill 清单目录（默认内置 bundled）
    return [Path(__file__).parent / "bundled", *(Path(p) for p in (extra or []))]  # 内置 + 额外


def discover(extra: list[str] | None = None) -> list[SkillManifest]:  # 扫描并解析 skill.yaml（prompt 是路径则内联）
    out: list[SkillManifest] = []  # 结果
    for root in skill_dirs(extra):  # 每个根
        if not root.is_dir():  # 不存在
            continue  # 跳过
        for yaml_file in root.glob("**/skill.yaml"):  # 递归找
            m = _load(yaml_file)  # 解析
            if m is None:  # 无效
                continue  # 跳过
            m.prompt = _resolve_prompt(m.prompt, yaml_file.parent)  # 路径 → 内联文本（无则保持）
            out.append(m)  # 追加
    return out  # 返回


def _resolve_prompt(prompt_or_path: str, base: Path) -> str:  # 若是相对路径则读文件内联
    if not prompt_or_path or not str(prompt_or_path).strip().endswith((".md", ".txt")):  # 非路径
        return prompt_or_path  # 原样
    target = base / str(prompt_or_path).strip()  # 拼接
    try:  # 读
        return target.read_text(encoding="utf-8") if target.is_file() else prompt_or_path  # 内联或原样
    except Exception:  # 读失败
        return prompt_or_path  # 原样


def validate(manifest: SkillManifest) -> list[str]:  # 校验（返回错误列表，空=通过）
    """id 非空唯一由注册处查；这里查：工具存在、副作用/角色/审批合法、版本>0。"""
    errors: list[str] = []  # 错误
    if not manifest.id:  # 无 id
        errors.append("id 不能为空")  # 记
    if not manifest.description:  # 无描述
        errors.append(f"{manifest.id or '?'}: 缺 description")  # 记
    if int(manifest.version) < 1:  # 版本非法
        errors.append(f"{manifest.id}: version 必须 >=1")  # 记
    if manifest.side_effect not in ("none", "reversible", "irreversible"):  # 副作用非法
        errors.append(f"{manifest.id}: side_effect 非法 {manifest.side_effect}")  # 记
    for role in manifest.tools:  # 逐个工具角色
        if role.role not in ("primary", "support", "optional"):  # 角色非法
            errors.append(f"{manifest.id}: {role.name} role 非法")  # 记
        if role.approval not in ("none", "auto", "manual"):  # 审批非法
            errors.append(f"{manifest.id}: {role.name} approval 非法")  # 记
        if role.name not in _catalog() and role.name not in EXTRA_TOOLS:  # 工具不存在
            errors.append(f"{manifest.id}: 工具 {role.name} 不在 tool_catalog/EXTRA_TOOLS")  # 记
    return errors  # 返回


def enabled(manifest: SkillManifest) -> bool:  # 门控（默认关；环境变量显式开）
    """`HELPER_SKILL_ENABLED` = 逗号分隔的 skill id 白名单；单条可 `HELPER_SKILL_<ID>=on` 覆盖。"""
    if manifest.enabled and manifest.id in _env_list("HELPER_SKILL_ENABLED"):  # manifest 开 + 白名单含
        return True  # 开
    return str(os.environ.get(f"HELPER_SKILL_{manifest.id.upper()}", "")).strip().lower() in ("1", "true", "on", "yes")  # 显式开


def resolve_tools(manifest: SkillManifest) -> list[Any]:  # 按名解析工具（本地 catalog + EXTRA_TOOLS）
    catalog = _catalog()  # 本地
    tools: list[Any] = []  # 结果
    for role in manifest.tools:  # 逐个
        tool = catalog.get(role.name) or EXTRA_TOOLS.get(role.name)  # 本地优先，扩展次之
        if tool is not None:  # 找到
            tools.append(tool)  # 收集
    return tools  # 返回


def assemble(manifest: SkillManifest, model: Any | None = None) -> Any | None:  # 组装成可执行 agent 图
    """用 skill 的提示 + 解析到的工具，经 build_react 组一个 ReAct 图；未启用/缺工具/无模型 → None。"""
    if not enabled(manifest):  # 门控关
        return None  # 不组装
    tools = resolve_tools(manifest)  # 工具
    if not tools:  # 无工具
        return None  # 空
    if model is None:  # 缺省取统一模型
        from app.agent.llm.provider import get_chat_model  # 模型

        model = get_chat_model()  # 取
    if model is None:  # 无模型
        return None  # 空
    from app.agent.react.graph import build_react  # ReAct 图

    return build_react(model, tools, manifest.prompt or manifest.description)  # 组图


def registered(extra: list[str] | None = None) -> dict[str, SkillManifest]:  # 注册表（发现+校验，fail-fast）
    """校验不过 → 抛 ValueError（启动期报错，不静默）。"""
    out: dict[str, SkillManifest] = {}  # id -> manifest
    for m in discover(extra):  # 逐个
        errors = validate(m)  # 校验
        if errors:  # 有错
            raise ValueError(" | ".join(errors))  # fail-fast
        if m.id in out:  # 重名
            raise ValueError(f"skill 重名: {m.id}")  # fail-fast
        out[m.id] = m  # 注册
    return out  # 返回


def _load(yaml_file: Path) -> SkillManifest | None:  # 解析单个 skill.yaml
    import yaml  # YAML（项目已有依赖）

    try:  # 读取
        data = yaml.safe_load(yaml_file.read_text(encoding="utf-8")) or {}  # 内容
        return SkillManifest(**data)  # 建模
    except Exception:  # 坏文件
        return None  # 跳过（发现期不崩，校验期不在此）


def _catalog() -> dict[str, Any]:  # 本地工具目录
    from app.agent.tools.catalog import tool_catalog  # 目录

    return tool_catalog()  # 返回


def _env_list(name: str) -> set[str]:  # 逗号分隔环境变量
    raw = str(os.environ.get(name, "")).strip()  # 值
    return {x.strip() for x in raw.split(",") if x.strip()} if raw else set()  # 集合
