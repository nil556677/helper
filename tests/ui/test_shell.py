"""外壳与页面交互测试（**模拟用户真的点**）。

聊天区现在是**模拟数据**（用户要求：先优化界面），所以这些用例钉三件事：

1. 界面把各种**内容格式**渲染对了（Markdown 表格、代码块、引用角标、过程行、报错色）；
2. **多会话**能新建、能切、切回来历史还在、第一句问题成为标题；
3. 界面**不撒谎**：模拟数据必须明确标注出来。

真正的"渲染成什么样"由 DOM 探针在真实 WebEngine 里再验一遍（见 `.tmp/shot.py`）——
用户模拟里没有浏览器，看不到 markdown-it 的产物。
"""

from __future__ import annotations

import asyncio

import pytest
from nicegui import ui
from nicegui.testing import User

from app.ui.screens import _chat_mock
from app.ui.state import conversations

SAMPLE_TITLE = '示例对话（模拟数据）'


def _import_now(name: str, data: bytes) -> int:
    """走真实路径导入一份资料：暂存 → 排队 → inline 跑完（界面测试用）。"""
    from app import tasks as tasks_app
    from app.services import knowledge as knowledge_service
    from app.core.jobs import registry as jobs_registry
    from app.core.jobs.runner import Runner

    tasks_app.register_kinds()
    payload = knowledge_service.stage_upload(name, data)
    job_id = tasks_app.enqueue_import(payload, dedupe_key=knowledge_service.upload_dedupe_key(data))
    Runner(inline=True).drain()
    jobs_registry.clear()  # 注册表是进程级的：别把状态留给下一个用例
    return job_id


@pytest.fixture(autouse=True)
def from_first_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例都从第一个模拟场景开始（否则场景轮换会让断言随用例顺序变化）。"""
    monkeypatch.setattr(_chat_mock, '_cursor', 0)


async def test_home_renders_sample_turn_with_all_formats(user: User) -> None:
    """首屏是一个完整示例（并**明确标注是模拟数据**）；表格与代码块原样交给 Markdown 渲染。"""
    await user.open('/')

    await user.should_see(SAMPLE_TITLE)
    await user.should_see('聊天')  # 顶栏标题用"聊天"，不是"问答"
    await user.should_see('示例数据')
    await user.should_see('违约金的算法')  # 答案正文
    await user.should_see('出处')  # 引用角标那一行

    contents = [str(element.content or '') for element in user.find(kind=ui.markdown).elements]
    assert any('| 项目 |' in text for text in contents), contents  # 表格
    assert any('```python' in text for text in contents), contents  # 代码块


async def test_sent_question_gets_process_row_and_answer(user: User) -> None:
    """发一条消息：用户气泡 + 过程行（可收起）+ 答案都在。"""
    await user.open('/')

    user.find(marker='chat-input').type('违约金怎么算')
    user.find(marker='chat-send').click()

    await user.should_see('违约金怎么算')  # 用户气泡
    await user.should_see('检索本地资料')  # 过程行标题
    await user.should_see('设备参数对照表')  # 第二个场景的答案
    user.find(kind=ui.expansion)  # 过程行确实是一个可收起的元素


async def test_citation_opens_fragment_in_right_panel(user: User) -> None:
    """点引用角标 → 右栏显示该片段全文与出处（DSH 的引用也是点开侧栏）。"""
    await user.open('/')
    await user.should_see('出处')

    user.find(marker='cite-1').click()

    await user.should_see(marker='fragment-panel')
    await user.should_see('设备采购合同.md')
    await user.should_see('bm25')  # 只有右栏的出处信息里才有这个字样


async def test_new_conversation_is_empty_and_listed(user: User) -> None:
    """「新建对话」给一个空会话，并进左栏列表；老会话还在、内容不串。"""
    await user.open('/')
    await user.should_see(SAMPLE_TITLE)

    user.find(marker='new-chat').click()

    await user.should_see('这个对话还是空的')
    await user.should_see('新对话')  # 新会话（还没起名）
    await user.should_see(SAMPLE_TITLE)  # 老会话仍在列表里
    await user.should_not_see('违约金的算法')  # 但对话流已经是空会话的


async def test_switching_back_restores_history(user: User) -> None:
    """切回老会话，历史内容要回来。

    **用 marker 点**：会话那一行是 button（原来用 `ui.item`，但实测它在真实浏览器里点击不触发，
    见 app/shell.py 的注释），marker 精确定位到那一行本身。
    """
    await user.open('/')
    user.find(marker='new-chat').click()
    await user.should_see('这个对话还是空的')

    sample = next(c for c in conversations.conversations() if c.title == SAMPLE_TITLE)
    user.find(marker=f'conv-{sample.id}').click()

    await user.should_see('违约金的算法')
    await user.should_not_see('这个对话还是空的')


async def test_first_question_becomes_the_title(user: User) -> None:
    """新会话的第一句问题成为它的标题（左栏那一行）。"""
    await user.open('/')
    user.find(marker='new-chat').click()
    await user.should_see('这个对话还是空的')

    user.find(marker='chat-input').type('设备参数对照表怎么看')
    user.find(marker='chat-send').click()
    await user.should_see('设备参数对照表')
    await asyncio.sleep(0.3)  # 发送是 async 处理：等它把这一轮落进会话数据

    titles = [element.text for element in user.find(kind=ui.button).elements]
    assert conversations.current().title == '设备参数对照表怎么看', conversations.current().title
    assert '设备参数对照表怎么看' in titles, titles


async def test_miss_and_error_look_different(user: User) -> None:
    """连发几条走到"没命中"和"检索失败"两个场景：两者文案与样式必须能区分。

    这里**按渲染结果逐步等待**（而不是发固定次数）：场景是轮换的，固定次数会随
    "首屏示例那个定时器有没有先跑"而漂移，测试就会时红时绿。
    """
    await user.open('/')
    await user.should_see('违约金的算法', retries=20)  # 等首屏示例渲染完

    user.find(marker='chat-input').type('参数对照')
    user.find(marker='chat-send').click()
    await user.should_see('设备参数对照表', retries=20)

    user.find(marker='chat-input').type('随便问点什么')
    user.find(marker='chat-send').click()
    await user.should_see('没有在本地资料里找到相关内容', retries=20)

    user.find(marker='chat-input').type('再问一次')
    user.find(marker='chat-send').click()
    await user.should_see('这次检索没跑完', retries=20)

    error_labels = [
        element
        for element in user.find('这次检索没跑完').elements
        if 'hp-note-error' in element._classes
    ]
    assert error_labels, '报错要用语义色（hp-note-error），不能和"没找到"长一样'


async def test_settings_is_a_separate_page(user: User) -> None:
    """进设置：左栏换成设置分区、「返回聊天」出现，**聊天页内容必须消失**。"""
    await user.open('/')

    user.find(marker='pin-settings').click()

    await user.should_see('返回聊天')
    await user.should_see('通用')
    await user.should_see('模型配置')
    await user.should_see('知识库')  # 草图（形状先定下来，逻辑还没接）
    # 词库 / 追踪还没做，就先不摆进来——点进去是空的比不摆更糟
    await user.should_not_see('词库')
    await user.should_not_see('追踪')
    await user.should_not_see(SAMPLE_TITLE)  # 会话列表属于聊天模式


async def test_back_to_chat(user: User) -> None:
    """「返回聊天」回到聊天页，设置分区随之消失。"""
    await user.open('/')
    user.find(marker='pin-settings').click()
    await user.should_see('返回聊天')

    user.find(marker='back-to-chat').click()

    await user.should_see(SAMPLE_TITLE)
    await user.should_not_see('返回聊天')


async def test_settings_sections_switch(user: User) -> None:
    """设置分区能切：「通用」有真内容（日志级别、数据目录、版本）。"""
    await user.open('/')
    user.find(marker='pin-settings').click()

    user.find(marker='section-general').click()

    await user.should_see('日志级别')
    await user.should_see('数据目录')
    await user.should_see('helper')  # 关于那一段


async def test_tasks_page_renders_empty_state(user: User) -> None:
    """任务页的骨架：正在跑 + 筛选 + 分页页脚 + 自动刷新三件套；空列表给空状态而不是空白。"""
    await user.open('/')
    user.find(marker='pin-settings').click()
    user.find(marker='section-tasks').click()

    await user.should_see('正在跑（0）')
    await user.should_see('现在没有任务在跑')
    await user.should_see('空闲')  # 队列状态
    await user.should_see('现在没有进行中的任务。')

    # 骨架的四段都在（卡头 → 工具栏 → 主体 → 页脚）
    for marker in ('page-summary', 'auto-refresh', 'auto-stamp', 'manual-refresh'):
        assert user.find(marker=marker).elements, f'缺少 {marker}'
    for group in ('active', 'failed', 'finished', 'all'):
        assert user.find(marker=f'task-group-{group}').elements, f'缺少筛选按钮 {group}'

    controls = [
        element
        for element in user.find(kind=ui.button).elements
        if str(getattr(element, 'text', '')) in ('暂停队列', '重试全部失败', '清理已完成')
    ]
    assert len(controls) == 3, [str(getattr(e, 'text', '')) for e in controls]
    assert all(element.enabled for element in controls)


async def test_tasks_page_shows_a_queued_job(user: User) -> None:
    """有任务时：任务名、等待状态与取消按钮都要出来（"我的活排上了没有"）。"""
    from app import tasks as tasks_app
    from app.core.jobs import api, registry as jobs_registry

    tasks_app.register_kinds()
    try:
        api.enqueue("import", {"path": "（测试）", "name": "设备采购合同.md"}, dedupe_key="t:1")
        await user.open('/')
        user.find(marker='pin-settings').click()
        user.find(marker='section-tasks').click()

        await user.should_see('导入资料')
        await user.should_see('设备采购合同.md')
        await user.should_see('等待中')
    finally:
        jobs_registry.clear()


async def test_tasks_page_shows_page_range(user: User) -> None:
    """分页页脚报的是**区间**（"137 条 · 显示 21–40"），不是"第 2/7 页"。

    用户在长列表里想知道的是"我在数据的哪一段、这段有多少条"，页码回答不了这个。
    """
    from app import tasks as tasks_app
    from app.core.jobs import api, registry as jobs_registry

    tasks_app.register_kinds()
    try:
        for index in range(3):
            api.enqueue("import", {"path": "（测试）", "name": f"第{index + 1}份.md"}, dedupe_key=f"p:{index}")

        await user.open('/')
        user.find(marker='pin-settings').click()
        user.find(marker='section-tasks').click()

        await user.should_see('3 条 · 显示 1–3')
        # 每页 3 条时第 2 页只剩 0 条 → 区间也要跟着变（翻页是服务端做的）
        assert user.find(marker='page-summary').elements
    finally:
        jobs_registry.clear()


async def test_cancel_a_queued_job_from_the_page(user: User) -> None:
    """从界面取消一条排队中的任务：后端状态变了、切到「已完成」也看得到（前后端闭环）。"""
    from app import tasks as tasks_app
    from app.core.jobs import api, registry as jobs_registry

    tasks_app.register_kinds()
    try:
        job_id = api.enqueue("import", {"path": "（测试）", "name": "排队里的任务.md"}, dedupe_key="t:2")
        await user.open('/')
        user.find(marker='pin-settings').click()
        user.find(marker='section-tasks').click()
        await user.should_see('排队里的任务.md')

        user.find(marker=f'task-cancel-{job_id}').click()
        await asyncio.sleep(0.4)  # 取消走的是 async 处理
        assert api.get(job_id).status == 'cancelled', api.get(job_id).status

        # 取消后它不再属于「进行中」；切到「已完成」能看到它（分组筛选也是前后端闭环的一部分）
        user.find(marker='task-group-finished').click()
        await asyncio.sleep(0.3)
        await user.should_see('排队里的任务.md')
        await user.should_see('已取消')
    finally:
        jobs_registry.clear()


async def test_failed_job_shows_details_timeline(user: User) -> None:
    """失败的任务能在界面上看到**完整情况**：分类、第几次、以及每一次的经过。

    以前只有行内一句话，完整原因躺在 `logs/helper.log` 里，用户得自己去翻文件。
    """
    from app import tasks as tasks_app
    from app.core.jobs import api, registry as jobs_registry, store

    tasks_app.register_kinds()
    try:
        job_id = api.enqueue("import", {"path": "（测试）", "name": "失败的任务.md"}, dedupe_key="t:fail")
        store.claim_next(("import",), lease_s=60.0, worker="cpu@test")
        store.fail(
            job_id,
            error="暂不支持这种格式：老式 .xls 请另存为 .xlsx",
            retryable=False,
            kind="auth",
            detail="Traceback (most recent call last): …",
        )

        await user.open('/')
        user.find(marker='pin-settings').click()
        user.find(marker='section-tasks').click()
        user.find(marker='task-group-failed').click()  # 失败的在「失败」这一组
        await user.should_see('失败的任务.md')
        await user.should_see('密钥不对')  # 分类标签（复用 §10.1 的那套词汇）

        user.find(marker=f'task-details-{job_id}').click()
        await asyncio.sleep(0.3)
        await user.should_see('第 1 次')  # 时间线里第几次尝试
        await user.should_see('完整信息（报错原文）')  # 展开就能看 traceback
    finally:
        jobs_registry.clear()


async def test_bulk_retry_failed_jobs_from_the_page(user: User) -> None:
    """批量选择 + 批量重试：一次把多条失败任务重新排队（数据一多最痛的缺口）。"""
    from app import tasks as tasks_app
    from app.core.jobs import api, registry as jobs_registry, store

    tasks_app.register_kinds()
    try:
        ids = []
        for index in range(2):
            job_id = api.enqueue(
                "import", {"path": "（测试）", "name": f"失败的任务{index}.md"}, dedupe_key=f"bulk:{index}"
            )
            store.claim_next(("import",), lease_s=60.0, worker="cpu@test")
            store.fail(job_id, error="暂不支持这种格式", retryable=False, kind="unsupported")
            ids.append(job_id)

        await user.open('/')
        user.find(marker='pin-settings').click()
        user.find(marker='section-tasks').click()
        user.find(marker='task-group-failed').click()
        await user.should_see('失败的任务0.md')

        user.find(marker='task-select-page').click()  # 全选本页
        await asyncio.sleep(0.2)
        user.find(marker='task-batch-retry').click()
        await asyncio.sleep(0.3)

        assert [store.get_job(job_id).status for job_id in ids] == ["pending", "pending"]
    finally:
        jobs_registry.clear()


async def test_bulk_delete_documents_from_the_page(user: User) -> None:
    """知识库页：勾选 → 批量删除 → 确认 → 一份都不剩（副本与片段一起走）。"""
    from app import tasks as tasks_app
    from app.services import knowledge as knowledge_service
    from app.core.jobs import registry as jobs_registry
    from app.core.jobs.runner import Runner

    tasks_app.register_kinds()
    try:
        for index in range(2):
            data = f"# 资料{index}\n\n第 {index} 条内容。\n".encode()
            payload = knowledge_service.stage_upload(f"资料{index}.md", data)
            tasks_app.enqueue_import(payload, dedupe_key=knowledge_service.upload_dedupe_key(data))
        Runner(inline=True).drain()
        assert len(knowledge_service.documents()) == 2

        await user.open('/')
        user.find(marker='pin-settings').click()
        user.find(marker='section-knowledge').click()
        await user.should_see('资料0.md')

        user.find(marker='kb-select-page').click()
        await asyncio.sleep(0.2)
        user.find(marker='kb-batch-delete').click()
        await asyncio.sleep(0.2)
        user.find(marker='kb-batch-confirm').click()
        await asyncio.sleep(0.3)

        assert knowledge_service.documents() == []
    finally:
        jobs_registry.clear()


async def test_knowledge_page_lists_imported_files(user: User) -> None:
    """知识库页：导入过的资料要列出来（名称/片段数/状态），并带重建与删除两个动作。

    导入走**真实路径**：上传暂存 → 排队 → inline 跑完（`tests/unit/test_import_job.py`
    测那条链路的细节）；这里只钉"界面把真数据画出来了"。
    """
    _import_now("设备采购合同.md", "# 合同\n\n第一条 乙方应在 30 日内交付设备。".encode())

    await user.open('/')
    user.find(marker='pin-settings').click()
    user.find(marker='section-knowledge').click()

    await user.should_see('知识库')
    await user.should_see('设备采购合同.md')
    await user.should_see('就绪')
    await user.should_see('份资料')  # 概览那四格
    await user.should_see('个片段')

    # 上传控件在（导入入口），并且是个真控件而不是草图
    uploads = list(user.find(kind=ui.upload).elements)
    assert uploads, '导入入口应该是个上传控件'
    assert uploads[0].enabled, '上传控件不能是禁用的'


async def test_knowledge_page_shows_failure_reason(user: User) -> None:
    """导入失败的那一份也要列出来并写清原因（只弹通知的话关掉就找不到了）。"""
    _import_now("老表格.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

    await user.open('/')
    user.find(marker='pin-settings').click()
    user.find(marker='section-knowledge').click()

    await user.should_see('老表格.xls')
    await user.should_see('失败')
    await user.should_see('另存为')


async def test_model_slots_are_live_and_per_slot(user: User) -> None:
    """模型配置**可用**：两个槽位、每个槽位一个「测试连接」与「保存」。

    没配好时「测试连接」是**灰的**——点了只会得到一句"先填好…"，不如直接不给点（保存后才可点）。
    """
    await user.open('/')
    user.find(marker='pin-settings').click()
    user.find(marker='section-models').click()

    await user.should_see('推理模型')
    await user.should_see('OCR 模型')
    await user.should_see('未配置')

    connects = [
        element
        for element in user.find(kind=ui.button).elements
        if str(getattr(element, 'text', '')) == '测试连接'
    ]
    saves = [
        element
        for element in user.find(kind=ui.button).elements
        if str(getattr(element, 'text', '')) == '保存'
    ]
    assert len(connects) == 2, [str(getattr(e, 'text', '')) for e in connects]
    assert not any(element.enabled for element in connects), '还没配好时不该能点测试'
    assert all(element.enabled for element in saves), '保存随时可以点'
    assert len(saves) >= 2  # 两个槽位各一个（「通用」里还有个日志级别的保存）

    await user.should_not_see('温度')  # 高级参数不上界面（用库里的默认值）
    await user.should_see('llm_slots')  # 说明里写清配置进数据库


async def test_saving_a_slot_writes_to_database(user: User) -> None:
    """在界面上填一个槽位并保存：**数据库里真的有了**，而且只存尾号、不存明文。

    这条是端到端：界面上打字 → services → DPAPI 加密 → `llm_slots` 表。
    """
    from app.services import models as models_service

    await user.open('/')
    user.find(marker='pin-settings').click()
    user.find(marker='section-models').click()

    user.find(marker='slot-reasoning-provider').type('火山方舟')
    user.find(marker='slot-reasoning-model').type('doubao-seed-1.6')
    user.find(marker='slot-reasoning-base_url').type('https://ark.example.com/api/v3')
    user.find(marker='slot-reasoning-change').click()  # 解锁密钥输入框
    user.find(marker='slot-reasoning-key').type('sk-test-abcdefghijklmnop')
    user.find(marker='slot-reasoning-save').click()
    await asyncio.sleep(0.2)

    item = next(i for i in models_service.load_all() if i['key'] == 'reasoning')
    assert item['provider'] == '火山方舟'
    assert item['base_url'] == 'https://ark.example.com/api/v3'
    assert item['model'] == 'doubao-seed-1.6'
    assert item['key_tail'] == 'klmnop'[-4:]
    assert item['has_key'] is True
    assert 'sk-test-abcdefghijklmnop' not in repr(item)

    await user.should_see('已配置')
    # 配好之后「测试连接」才变成可点（配好前是灰的）
    connects = [
        element
        for element in user.find(kind=ui.button).elements
        if str(getattr(element, 'text', '')) == '测试连接'
    ]
    assert any(element.enabled for element in connects), '保存成功后测试连接应变成可点'


def test_mock_covers_multiple_scenarios() -> None:
    """模拟数据要覆盖多种情况（用户要求"各种模拟的"）：至少含表格、代码块、无命中、报错。"""
    scenarios = _chat_mock.SCENARIOS
    assert len(scenarios) >= 4
    blob = '\n'.join(turn.answer + turn.note for turn in scenarios)
    assert '|' in blob  # 表格
    assert '```' in blob  # 代码块
    assert any(turn.note_kind == 'error' for turn in scenarios)  # 报错
    assert any(not turn.answer for turn in scenarios)  # 没答案的场景


def test_conversation_store_behaviour() -> None:
    """会话状态本身的行为（不经过界面）：新建插到最前、切换、追加、自动命名。"""
    conversations.reset()
    first = conversations.new(title='甲')
    second = conversations.new()

    assert [c.title for c in conversations.conversations()] == ['新对话', '甲']  # 新的在前
    assert conversations.current().id == second.id

    conversations.select(first.id)
    assert conversations.current().id == first.id

    # 自动命名只发生在"还是默认标题"的会话上：切回 second（默认标题）再追加
    conversations.select(second.id)
    conversations.append(_entry('这是第一句问题，长度超过十八个字就应该被截断'))
    assert conversations.current().title.startswith('这是第一句问题')
    assert conversations.current().title.endswith('…')
    assert len(conversations.current().entries) == 1
    # 已经有标题的会话不会被改名
    assert next(c for c in conversations.conversations() if c.id == first.id).title == '甲'


def _entry(question: str):
    from app.ui.state.chat_model import Entry

    return Entry(question=question, turn=_chat_mock.SCENARIOS[0])
