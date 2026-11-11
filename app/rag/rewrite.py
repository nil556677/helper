"""查询改写：复用 `llm` 槽位（**全方案唯一用大模型的地方**），失败一律回退原句。

三条硬约束（方案 §3）：
1. **只做形式变换**：不改事实、不加背景；
2. **第 1 条永远是原问题**：防跑偏的锚，校验不过就整批丢弃；
3. **硬要素必须保留**：编号/金额/日期丢了就丢弃该条（这是确定性校验）。

写缓存、限超时、不重试：改写是"可选增强"，任何失败都不能阻断检索。
"""

from __future__ import annotations  # 类型注解延迟求值

import json  # 解析模型输出
import re  # 正则
from dataclasses import dataclass, field  # 结果容器
from typing import Any  # 任意值

from app.agent.llm import DEFAULT_PARAMS  # 共用默认参数
from app.agent.llm.gateway import ModelGateway  # 统一模型网关
from app.agent.llm.types import ModelRequest, ModelUnavailable  # 请求 / 槽位不可用
from app.agent.contexts.prompts import REWRITE_SYSTEM_UID, get  # 统一改写提示模板
from app.agent.contracts import ErrorCode  # 错误码单源
from app.agent.tools.cache import TtlCache  # 共享 TTL 缓存（按条 ttl）
from app.core.retry import RetryPolicy, classify_error  # 重试策略 / 错误分类
from app.rag import hard  # 硬要素（校验"有没有改丢"）
from app.rag.normalize import normalize  # 规则归一化

#: 提示词版本：改提示词必须改它（进缓存键）。
PROMPT_VERSION = "rewrite-v2"  # 版本号

#: 最多产出几条（含原句）。
MAX_QUERIES = 3  # 上限

#: 变体最大长度。
MAX_CHARS = 40  # 上限

#: 单次调用超时（秒）。
TIMEOUT_S = 0.8  # 超时

#: 成功缓存 TTL（秒）。
CACHE_TTL = 30 * 24 * 3600  # 30 天

#: 失败缓存 TTL（秒）：故障期别每条都打模型。
FAIL_TTL = 60  # 60 秒

#: 指代词：出现就说明"缺主语"。
_PRONOUNS = ("它", "这个", "那个", "该", "此", "上面", "刚才", "这份", "上述", "其")  # 指代

#: 无实体的疑问说法。
_VAGUE = ("怎么办", "怎么弄", "如何", "什么要求", "有哪些", "需要什么")  # 含糊

#: 实体线索：数字、引号、书名号。
_ENTITY = re.compile(r"\d|「|」|“|”|\"|《|》")  # 实体线索

#: 业务英文术语 → 中文（简单改写用规则，不进 LLM；只放高置信映射）。
_TERMS: dict[str, str] = {  # 术语词典
    "annual leave": "年假",  # 年假
    "sick leave": "病假",  # 病假
    "overtime": "加班",  # 加班
    "salary": "薪资",  # 薪资
    "bonus": "奖金",  # 奖金
    "probation": "试用期",  # 试用期
    "paternity leave": "陪产假",  # 陪产假
    "maternity leave": "产假",  # 产假
    "salary advance": "预支工资",  # 预支
}  # 词典结束

#: 拉丁字母词检测：跨语言/术语判定用。
_LATIN = re.compile(r"[A-Za-z]{2,}")  # 拉丁词

#: 含糊动词（意图不明）：怎么+动作动词 / 如何 / 能不能 / 要不要。
#: 排除"怎么样"这类疑问语气（目标明确时不写改）。
_VAGUE_RE = re.compile(r"怎么(?=办|做|清|弄|搞|算|处理|申请|重置|设置|结|续|查|标记)|如何|能不能|要不要")  # 含糊


def _latin_tokens(text: str) -> list[str]:  # 取拉丁词（小写去重）
    """抽出 query 里的英文词，用于跨语言判定。"""
    return list({word.lower() for word in _LATIN.findall(text)})  # 小写去重

SYSTEM_PROMPT = get(REWRITE_SYSTEM_UID).render()  # 渲染统一改写提示

#: 缓存：成功 30 天 / 失败 60 秒（按条 ttl），容量 1024 防内存无限增长。
_CACHE = TtlCache(ttl=CACHE_TTL, max_entries=1024)  # 共享缓存原语

#: 改写统一走模型网关；临时性失败（限流/5xx/超时/连接）重试并发 RETRY 事件到前端，配置错不重试。
from app.rag._emit import forward as _forward_emit  # 事件转发（有 run 才发）

_GATEWAY = ModelGateway(slot="llm", policy=RetryPolicy(), emit=_forward_emit)  # 网关（瞬时错误可重试 + 事件）

#: 改写专用参数：温度 0、小输出、800ms 超时。
_REWRITE_PARAMS = DEFAULT_PARAMS.with_overrides(  # 派生
    temperature=0.0,  # 稳
    max_tokens=128,  # 小
    timeout_s=TIMEOUT_S,  # 800ms
    max_retries=0,  # 无重试
)  # 参数结束


@dataclass(frozen=True)
class RewriteResult:
    """一次改写的结果。"""

    queries: list[str] = field(default_factory=list)  # 第 1 条 = 原问题（归一化后）
    reason: str = "skipped"  # used/unchanged/skipped/unconfigured/timeout/rejected/failed
    model: str = ""  # 模型名（轨迹用）

    @property
    def changed(self) -> bool:  # 有没有变体
        """是否真的产生了额外查询。"""
        return len(self.queries) > 1  # 多于一条


def trigger(question: str, history: list[dict[str, Any]] | None) -> str:  # 是否要改写
    """返回触发原因；空串表示不需要改写（省一次模型调用）。

    生产分层（简单→规则，复杂→LLM）：
    - term：含业务英文术语 → 规则直接补中文等价，不进 LLM
    - cross_lang：整句以外文为主 → LLM 双语/翻译
    - pronoun / vague / multi_turn：意图含糊或代指 → LLM 消歧/补全
    """
    text = str(question or "").strip()  # 原问题
    if not text:  # 空
        return ""  # 不改
    tokens = _latin_tokens(text)  # 英文词
    known_term = [eng for eng in _TERMS if re.search(rf"\b{re.escape(eng)}\b", text, re.IGNORECASE)]  # 命中术语(含多词)
    if known_term:  # 业务术语命中
        return "term"  # 规则改
    if tokens and sum(len(w) for w in tokens) >= 4:  # 拉丁字母占主体（整句外文/夹杂）
        return "cross_lang"  # LLM 双语
    if any(word in text for word in _PRONOUNS) and not _ENTITY.search(text):  # 有指代且无实体
        return "pronoun"  # LLM 补全
    if _VAGUE_RE.search(text) and not _ENTITY.search(text):  # 含糊动词（排除"怎么样"）
        return "vague"  # LLM 消歧
    if history:  # 多轮
        return "multi_turn"  # LLM 补主语
    return ""  # 不改


def _term_replace(text: str) -> str:  # 规则：术语→中文等价
    """把 query 里命中的英文术语替换成中文；无命中原样返回。"""
    out = text  # 逐步替换
    for eng, zh in sorted(_TERMS.items(), key=lambda kv: -len(kv[0])):  # 长词优先
        pattern = re.compile(rf"\b{re.escape(eng)}\b", re.IGNORECASE)  # 词边界
        out = pattern.sub(zh, out)  # 替换
    return out  # 返回


def _cache_key(question: str, history: list[dict[str, Any]] | None, model: str) -> str:  # 缓存键
    """原问题 + 上一轮问题 + 模型 + 提示词版本。"""
    previous = str((history or [{}])[-1].get("content") or "") if history else ""  # 上一轮
    return f"{question}\x00{previous}\x00{model}\x00{PROMPT_VERSION}"  # 拼键


def _read_cache(key: str) -> list[str] | None:  # 读缓存
    """过期由 TtlCache 处理；命中返回列表。"""
    hit = _CACHE.get(key)  # 取（可能 None）
    return list(hit) if hit is not None else None  # 命中转列表


def _write_cache(key: str, queries: list[str], ttl: float) -> None:  # 写缓存
    """按本次 ttl 写入（成功/失败不同窗口）。"""
    _CACHE.set(key, list(queries), ttl=float(ttl))  # 逐条 ttl 写入


def _extract_json(raw: str) -> list[str] | None:  # 抠 JSON
    """从模型输出里抠出字符串数组；抠不出来返回 None。"""
    match = re.search(r"\[.*\]", str(raw or ""), re.S)  # 找数组
    if match is None:  # 没有
        return None  # 失败
    try:  # 解析
        data = json.loads(match.group(0))  # JSON
    except ValueError:  # 坏 JSON
        return None  # 失败
    if not isinstance(data, list):  # 不是数组
        return None  # 失败
    return [str(item).strip() for item in data if isinstance(item, str)]  # 只留字符串


def validate(raw: str, question: str) -> list[str] | None:  # 校验
    """返回 [原问题, *变体]；不合格返回 None（整批丢弃）。"""
    items = _extract_json(raw)  # 抠
    if not items:  # 空
        return None  # 失败
    numbers = hard.extract(question)  # 原问题的硬要素
    required = [*numbers["clause"], *numbers["date"], *numbers["amount"]]  # 必须保留的
    variants: list[str] = []  # 变体
    for item in items:  # 逐条
        text = item.strip().strip("`\"'")  # 去包裹
        if not text or text == question:  # 空/等于原句
            continue  # 跳过
        if len(text) > MAX_CHARS or "\n" in text:  # 太长/多行
            continue  # 跳过
        if text.startswith(("抱歉", "无法", "作为", "我不能")):  # 解释性输出
            continue  # 跳过
        found = hard.extract(text)  # 变体的硬要素
        pool = {*found["clause"], *found["date"], *found["amount"]}  # 变体有的
        if any(value not in pool for value in required):  # 丢了原问题的要素
            continue  # 丢弃该条
        if any(_new_numbers(text, question)):  # 新增了原句没有的数字
            continue  # 丢弃该条
        if text not in variants:  # 不重复
            variants.append(text)  # 记录
        if len(variants) >= MAX_QUERIES - 1:  # 够了
            break  # 停
    return [question, *variants]  # 原句永远第一


def _new_numbers(candidate: str, question: str) -> list[str]:  # 新增数字
    """变体里出现、原问题没有的数字（防止改写瞎编金额/编号）。"""
    old = set(re.findall(r"\d+(?:\.\d+)?", question))  # 原句数字
    return [value for value in re.findall(r"\d+(?:\.\d+)?", candidate) if value not in old]  # 新的


async def rewrite(  # 主入口（异步，走统一网关）
    question: str,
    *,
    history: list[dict[str, Any]] | None = None,
) -> RewriteResult:
    """归一化 → 触发判断 → 规则(术语) 或 网关(model) → 校验 → 缓存；任何失败都回退原句。"""
    original = normalize(question)  # 规则归一化（不花模型）
    reason = trigger(original, history)  # 要不要改
    if not reason:  # 不需要
        return RewriteResult([original], "skipped")  # 原句
    name = "llm"  # 槽位名（网关走 llm 槽）
    # —— 规则层：业务术语 → 直接补中文等价，不进 LLM（简单改写）——
    if reason == "term":  # 规则能确定等价
        replaced = _term_replace(original)  # 替换
        if replaced and replaced != original:  # 确实变了
            return RewriteResult([original, replaced], "term_map", "rule")  # 规则改（省一次模型）
    key = _cache_key(original, history, name)  # 缓存键
    cached = _read_cache(key)  # 读缓存
    if cached:  # 命中
        return RewriteResult(cached, "used", name)  # 直接用
    previous = str((history or [{}])[-1].get("content") or "") if history else ""  # 上一轮问题
    payload = json.dumps({"prev_question": previous, "query": original}, ensure_ascii=False)  # 输入当数据
    try:  # 调统一网关（异步、无重试）
        result = await _GATEWAY.generate(  # 一次生成
            ModelRequest(  # 请求
                model="llm",  # 槽位
                messages=[{"role": "user", "content": payload}],  # 数据输入
                system=SYSTEM_PROMPT,  # 系统提示
            ),  # 请求结束
            _REWRITE_PARAMS,  # 改写参数
        )  # 生成结束
        raw = result.text  # 文本
    except Exception as exc:  # 网关/模型不可用/超时/网络
        _write_cache(key, [original], FAIL_TTL)  # 失败短缓存
        reason_out = _classify_reason(exc)  # 统一判 reason
        return RewriteResult([original], reason_out, name)  # 回退原句
    queries = validate(raw, original)  # 校验
    if queries is None:  # 不合格
        _write_cache(key, [original], FAIL_TTL)  # 短缓存
        return RewriteResult([original], "rejected", name)  # 回退
    _write_cache(key, queries, CACHE_TTL)  # 成功缓存
    return RewriteResult(queries, "used", name)  # 返回


def _classify_reason(exc: BaseException) -> str:  # 失败原因分类
    """网关异常 → RewriteResult.reason：unconfigured/timeout/failed。"""
    if isinstance(exc, ModelUnavailable):  # 槽位没配
        return "unconfigured"  # 没配
    if classify_error(exc) == str(ErrorCode.TIMEOUT):  # 超时
        return "timeout"  # 超时
    return "failed"  # 其他


def better_rank(decision: str) -> int:  # 判决排序
    """pass > weak > reject（多查询时挑最好的那一路）。"""
    return {"pass": 2, "weak": 1, "reject": 0}.get(decision, 0)  # 排序值
