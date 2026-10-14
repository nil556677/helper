"""观感：把 DSH 设计系统的**浅色** token 与数值抄成我们自己的 CSS 变量与组件样式。

**数值来源**：`@deepseek-ai/dsh-client-ui-theme`（DSH 前端的主题包）里真实定义的 CSS 变量——
原始色板 `--dsw-static-neutral-bluish-*`、别名 token `--dsw-alias-*`、
圆角 `--dsw-radius-*`（xs4 / sm8 / md12 / lg16）、阴影 `--dsw-shadow-lv1..2`、
markdown 排版（正文 `14px/24px`、strong `600`、行内代码 `12px/19px`、h1 `700 21px/30px`）。

**只做浅色**（按项目所有者要求）：DSH 那套深色色板不抄进来，也不做主题切换，
入口还把 NiceGUI 固定成浅色——不跟随系统深色，避免出现"系统是深色、界面半深半浅"的状态。

**变量命名的语义对齐 DSH 的别名**（bg-base / bg-layer-1 / label-primary|secondary|tertiary /
border-l1..l4 / interactive-bg-hover），名字用 `hp-`（helper）前缀避免与第三方样式撞名。

**字体**不用 DSH 的 Montserrat（那是随它发布的字体文件，不搬别人的字体），改用系统字体栈。
"""

from __future__ import annotations

from nicegui import ui

#: 全部样式。分区：token → 基础 → 左栏 → 顶栏 → 消息流 → 引用 → 输入区 → 右栏。
STYLE = """
/* ============ 1. 设计 token（浅色，数值取自 DSH 主题包） ============ */
:root {
  /* 底色与层次：DSH 浅色下 bg-layer-* 与 bg-base 同为纯白，层次靠描边与淡底表达 */
  --hp-bg-base: #ffffff;
  --hp-bg-layer-1: #ffffff;
  /* 交互淡底：DSH 的 interactive-bg-hover（带一点蓝的 6% 黑）与实心版 */
  --hp-bg-hover: #2631480f;
  --hp-bg-hover-solid: #f1f3f5;

  --hp-label-primary: #0f1115;
  --hp-label-secondary: #61666b;
  --hp-label-tertiary: #81858c;

  --hp-border-l1: #0000000a;
  --hp-border-l2: #0000001a;
  --hp-border-l3: #0000001f;
  --hp-border-l4: #00000029;

  --hp-brand: #0f1115;
  --hp-accent: #4176e6;

  --hp-radius-xs: 4px;
  --hp-radius-sm: 8px;
  --hp-radius-md: 12px;
  --hp-radius-lg: 16px;

  --hp-shadow-lv1: 0 2px 4px 0 #0000000d;
  --hp-shadow-lv2: 0 4px 12px 0 #00000005, 0 2px 8px 0 #0000000a;

  --hp-font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  --hp-font-family-code: "Cascadia Mono", "JetBrains Mono", Consolas, "Courier New", monospace;
  --hp-font-size: 14px;
  --hp-line-height: 24px;
  --hp-font-size-sm: 12px;
  --hp-line-height-sm: 20px;
  --hp-measure: 760px;
}

/* ============ 2. 基础 ============ */
html, body { height: 100%; }
body {
  background: var(--hp-bg-base);
  color: var(--hp-label-primary);
  font-family: var(--hp-font-family);
  font-size: var(--hp-font-size);
  line-height: var(--hp-line-height);
  -webkit-font-smoothing: antialiased;
}
/* 桌面应用：页面本身不滚动，滚动交给各栏里的滚动区 */
.nicegui-content { padding: 0 !important; gap: 0 !important; height: 100vh; overflow: hidden; }
::selection { background: color-mix(in srgb, var(--hp-accent) 28%, transparent); }

/* 细滚动条：DSH 的滚动条很细，不抢视觉 */
*::-webkit-scrollbar { width: 8px; height: 8px; }
*::-webkit-scrollbar-track { background: transparent; }
*::-webkit-scrollbar-thumb { background: var(--hp-border-l3); border-radius: 4px; }
*::-webkit-scrollbar-thumb:hover { background: var(--hp-border-l4); }

.q-separator { background: var(--hp-border-l1) !important; }

/* 通用小工具类 */
.hp-hint { color: var(--hp-label-tertiary); font-size: var(--hp-font-size-sm); line-height: var(--hp-line-height-sm); }
.hp-measure { width: 100%; max-width: var(--hp-measure); margin-left: auto; margin-right: auto; }

/* ============ 3. 左栏（导航） ============ */
.hp-sidebar { background: var(--hp-bg-base); border-right: .5px solid var(--hp-border-l2) !important; }
.hp-brand { color: var(--hp-label-primary); font-weight: 600; letter-spacing: .08em; }

/* 左栏里"整行可点、左对齐"的按钮（「新建对话」「返回聊天」等） */
.hp-row-button {
  border-radius: var(--hp-radius-md);
  color: var(--hp-label-secondary);
  justify-content: flex-start;
  min-height: 38px;
  transition: background-color .12s ease, color .12s ease;
}
.hp-row-button:hover { background: var(--hp-bg-hover); color: var(--hp-label-primary); }

/* 会话列表（左栏）：一行一个会话，标题超长省略号。
   注意这些"行"是 **button**（不是 ui.item）：实测 `ui.item` 的点击在真实浏览器里不触发。 */
.hp-conv-row { justify-content: flex-start; }
.hp-conv-row .q-btn__content {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}
/* 当前选中那一行（会话列表 / 功能导航 / 设置分区共用） */
.hp-row-active { background: var(--hp-bg-hover-solid) !important; color: var(--hp-label-primary); }

/* ============ 4. 顶栏 ============ */
.hp-topbar { background: var(--hp-bg-base); border-bottom: .5px solid var(--hp-border-l2); }
.hp-topbar-title { font-weight: 500; color: var(--hp-label-primary); }

/* ============ 5. 消息流 ============ */
.hp-bubble-user {
  align-self: flex-end;
  max-width: 85%;
  background: var(--hp-bg-hover);
  border-radius: var(--hp-radius-lg);
  padding: 10px 14px;
  white-space: pre-wrap;
  /* 长 URL / 长路径必须能断行，否则会把气泡撑破、横向溢出 */
  overflow-wrap: anywhere;
}

/* ============ 6. 引用 ============ */
.hp-cite {
  padding: 1px 7px;
  border: .5px solid var(--hp-border-l1);
  border-radius: var(--hp-radius-xs);
  background: var(--hp-bg-hover);
  color: var(--hp-label-secondary);
  font-size: var(--hp-font-size-sm);
  line-height: var(--hp-line-height-sm);
  cursor: pointer;
  transition: background-color .12s ease, color .12s ease;
}
.hp-cite:hover { background: var(--hp-bg-hover-solid); color: var(--hp-label-primary); }

.hp-sources {
  border: .5px solid var(--hp-border-l1);
  border-radius: var(--hp-radius-md);
  overflow: hidden;
}
.hp-sources .q-item { min-height: 34px; color: var(--hp-label-secondary); }
.hp-sources .q-item:hover { background: var(--hp-bg-hover); }

/* ============ 7. 输入区 ============ */
.hp-composer { background: var(--hp-bg-base); border-top: .5px solid var(--hp-border-l2); }
.hp-composer-box {
  background: var(--hp-bg-layer-1);
  border: .5px solid var(--hp-border-l2);
  border-radius: var(--hp-radius-lg);
  box-shadow: var(--hp-shadow-lv1);
  padding: 4px 6px 4px 10px;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.hp-composer-box:focus-within { border-color: var(--hp-border-l4); box-shadow: var(--hp-shadow-lv2); }
/* 去掉 Quasar 输入框自带的底线与内边距，让整个盒子看起来像"一个输入框" */
.hp-composer-box .q-field__control { background: transparent !important; padding: 0 !important; }
.hp-composer-box .q-field__control:before,
.hp-composer-box .q-field__control:after { display: none !important; }
.hp-composer-box .q-field__native { color: var(--hp-label-primary); }
.hp-composer-box .q-field__native::placeholder { color: var(--hp-label-tertiary); }
.hp-send { background: var(--hp-brand) !important; color: var(--hp-bg-base) !important; }
.hp-send:hover { opacity: .88; }

/* ============ 8. 右栏（贴边面板） ============ */
.hp-panel { background: var(--hp-bg-base); border-left: .5px solid var(--hp-border-l2) !important; }
.hp-icon-button {
  border-radius: var(--hp-radius-sm);
  color: var(--hp-label-secondary);
  min-height: 30px; min-width: 30px;
}
.hp-icon-button:hover { background: var(--hp-bg-hover); color: var(--hp-label-primary); }

/* ============ 9. 设置页 ============ */
/* 设置是主区里的一个完整页面：内容居中限宽，分区导航在左栏（由外壳负责，见 app/shell.py） */
.hp-page-title { font-size: 17px; line-height: 26px; font-weight: 600; color: var(--hp-label-primary); }
/* 状态标签（如"还没做"）：小、克制，不抢标题的注意力 */
.hp-tag {
  font-size: 11px;
  line-height: 18px;
  padding: 0 6px;
  border-radius: var(--hp-radius-xs);
  background: var(--hp-bg-hover-solid);
  border: .5px solid var(--hp-border-l2);
  color: var(--hp-label-tertiary);
}
/* 槽位卡片：设置里"一个槽位一块"的形状（模型配置、以后的检索槽位都用它） */
.hp-slot {
  width: 100%;
  border: .5px solid var(--hp-border-l2);
  border-radius: var(--hp-radius-sm);
  padding: 12px 14px;
  background: var(--hp-bg-base);
}

/* ============ 10. 聊天内容（轮次结构 / 过程行 / 片段 / Markdown 渲染） ============ */
/* 结构照 DSH 的 Chat：一轮 = 用户气泡 + 过程行（**可收起**）+ 答案（**永不收起**），
   间距也用它的规格：过程行之间 6px、组标题与内容 8px、答案与过程行 12px。 */
.hp-turn { display: flex; flex-direction: column; gap: 12px; }

/* 过程行：文字 tertiary、悬停 secondary；收起时不占地方 */
.hp-process { width: 100%; background: transparent; }
.hp-process .q-expansion-item__container > .q-item { min-height: 26px; padding: 0; color: inherit; }
.hp-process .q-item__section--main { padding: 0; }
.hp-process .q-expansion-item__content { padding: 0; }
.hp-process-row { color: var(--hp-label-tertiary); font-size: 13px; }
.hp-process-row:hover { color: var(--hp-label-secondary); }
.hp-process-detail { color: var(--hp-label-tertiary); font-size: 12px; line-height: 20px; }

/* 运行中的微光扫过（DSH 的 TextShimmer 简化版：一道从左到右的高光） */
.hp-shimmer {
  background-image: linear-gradient(90deg, var(--hp-label-tertiary) 0%, var(--hp-label-tertiary) 35%,
                    var(--hp-label-primary) 50%, var(--hp-label-tertiary) 65%, var(--hp-label-tertiary) 100%);
  background-size: 300% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: hp-shimmer 1.6s linear infinite;
}
@keyframes hp-shimmer { from { background-position: 150% 0; } to { background-position: -150% 0; } }
@media (prefers-reduced-motion: reduce) { .hp-shimmer { animation: none; background-image: none; color: var(--hp-label-tertiary); } }

/* 引用角标：是按钮，所以能 Tab 到、回车能开（不是只能鼠标点的 div） */
.hp-cite {
  min-width: 22px; min-height: 22px;
  padding: 0 6px;
  border-radius: var(--hp-radius-xs);
  background: var(--hp-bg-hover-solid);
  color: var(--hp-label-secondary);
  font-size: 12px;
}
.hp-cite:hover { background: var(--hp-bg-hover); color: var(--hp-label-primary); }

/* 报错文字：用语义色，和"没找到"（hp-hint 灰）必须区分开 */
.hp-note-error { color: #c0392b; font-size: 13px; line-height: 22px; }

/* Markdown 渲染（片段与以后的模型回答都走它）：标题、列表、行内代码、代码块、表格、引用 */
.hp-markdown { font-size: var(--hp-font-size); line-height: var(--hp-line-height); color: var(--hp-label-primary); }
.hp-markdown > *:first-child { margin-top: 0; }
.hp-markdown > *:last-child { margin-bottom: 0; }
.hp-markdown h1, .hp-markdown h2, .hp-markdown h3, .hp-markdown h4 {
  font-weight: 600; margin: 12px 0 6px; line-height: 1.4;
}
.hp-markdown h1 { font-size: 17px; }
.hp-markdown h2 { font-size: 16px; }
.hp-markdown h3 { font-size: 15px; }
.hp-markdown p { margin: 6px 0; }
.hp-markdown ul, .hp-markdown ol { margin: 6px 0; padding-left: 22px; }
.hp-markdown ul { list-style: disc; }
.hp-markdown ol { list-style: decimal; }
.hp-markdown li { margin: 2px 0; }
.hp-markdown a { color: var(--hp-accent); text-decoration: underline; text-underline-offset: 2px; }
.hp-markdown code {
  font-family: var(--hp-font-family-code);
  font-size: 12px; line-height: 19px;
  background: var(--hp-bg-hover-solid);
  border: .5px solid var(--hp-border-l2);
  border-radius: var(--hp-radius-xs);
  padding: 1px 4px;
}
/* 代码块。**注意**：NiceGUI 的 markdown 组件用浏览器内置的 ``setHTML()`` 做消毒
   （见 nicegui/elements/markdown.js），它的默认白名单会**丢掉 <pre>**、但保留里面的 <code>
   以及 pygments 包出来的 .codehilite 容器。所以块级样式主要挂 .codehilite，
   并用 white-space: pre 把换行与缩进保住（没有 <pre> 时全靠它）。 */
.hp-markdown .codehilite,
.hp-markdown pre {
  background: var(--hp-bg-hover-solid);
  border: .5px solid var(--hp-border-l2);
  border-radius: var(--hp-radius-sm);
  padding: 10px 12px;
  margin: 8px 0;
  overflow-x: auto;      /* 长代码横向滚，不把整页撑宽 */
  font-family: var(--hp-font-family-code);
  font-size: 12px;
  line-height: 19px;
}
.hp-markdown .codehilite code,
.hp-markdown pre code {
  background: none;
  border: 0;
  padding: 0;
  white-space: pre;
  font-family: inherit;
  font-size: inherit;
  line-height: inherit;
}
.hp-markdown table {
  border-collapse: collapse;
  width: 100%;
  margin: 8px 0;
  font-size: 13px;
  display: block;        /* 窄屏时表格自己横向滚，不撑破布局 */
  overflow-x: auto;
}
.hp-markdown th, .hp-markdown td {
  border: .5px solid var(--hp-border-l3);
  padding: 4px 8px;
  text-align: left;
  white-space: nowrap;
}
.hp-markdown th { background: var(--hp-bg-hover-solid); font-weight: 600; }
.hp-markdown blockquote {
  margin: 8px 0; padding: 2px 0 2px 10px;
  border-left: 2px solid var(--hp-border-l4);
  color: var(--hp-label-secondary);
}
.hp-markdown hr { border: 0; border-top: .5px solid var(--hp-border-l2); margin: 12px 0; }

/* ============ 11. 外壳的高度（必须是"确定高度"） ============ */
/* 实测：整页链条 `.q-layout` / `.q-page` / `.nicegui-content` 的高度**都是 0**，界面一直靠
   "内容自然高度"撑着。单页看着没事，但只要有一层的孩子也是 flex-1/h-full，就会互相依赖、
   一起塌成 0——设置页的滚动区就是这么被裁成**白板**的（真踩过，DOM 探针量出来的）。

   这里给中栏一个用 `vh` 表达的确定高度：`vh` 相对**视口**，不受父元素 0 高度影响。
   注意别用 Tailwind 的 `h-screen`：NiceGUI 那份构建里没有这个类，写了等于没写（也实测过）。 */
.hp-main { height: 100vh; display: flex; flex-direction: column; }
.hp-main-page { flex: 1 1 0; min-height: 0; }

/* ============ 12. 设置页的卡片与表单 ============ */
/* 一个分区 = 若干张卡；一张卡 = 图标 + 标题（+ 状态） → 说明 → 内容 → 底部动作。
   字段一律"左边名称与说明、右边控件"：扫得快，也不像两列网格那样左右横跳。 */
.hp-card {
  width: 100%;
  background: var(--hp-bg-base);
  border: .5px solid var(--hp-border-l2);
  border-radius: var(--hp-radius-md);
  padding: 14px 16px;
  box-shadow: var(--hp-shadow-lv1);
}
.hp-card-head { display: flex; align-items: center; gap: 8px; }
.hp-card-icon { color: var(--hp-label-tertiary); }
.hp-card-title { font-size: 14px; line-height: 22px; font-weight: 600; color: var(--hp-label-primary); }

.hp-field-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.hp-field-main { flex: 1 1 auto; min-width: 0; }
.hp-field-name { font-size: 13px; line-height: 20px; color: var(--hp-label-primary); }
.hp-field-help { font-size: 12px; line-height: 19px; color: var(--hp-label-tertiary); }
.hp-input { width: 340px; max-width: 52vw; }
.hp-path {
  font-family: var(--hp-font-family-code);
  font-size: 12px;
  color: var(--hp-label-secondary);
  word-break: break-all;
  text-align: right;
  max-width: 52vw;
}
.hp-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

/* 状态点：未配置=灰、已配置/测试通过=绿、测试失败=红（颜色靠这几个类切） */
.hp-status { display: inline-flex; align-items: center; font-size: 12px; color: var(--hp-label-tertiary); }
.hp-status::before {
  content: "";
  width: 6px; height: 6px;
  border-radius: 50%;
  background: currentColor;
  opacity: .55;
  margin-right: 6px;
}
.hp-status-ok { color: #2f9e62; }
.hp-status-bad { color: #c0392b; }

/* ============ 13. 列表（卡片内的表格：表头 + 行） ============ */
.hp-row-head {
  font-size: 12px;
  color: var(--hp-label-tertiary);
  padding-bottom: 6px;
  border-bottom: .5px solid var(--hp-border-l2);
}
.hp-row-item {
  font-size: 13px;
  color: var(--hp-label-primary);
  padding: 7px 0;
  border-bottom: .5px solid var(--hp-border-l1);
}
.hp-row-item:hover { background: var(--hp-bg-hover); }
.hp-col-name { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hp-col { width: 76px; text-align: right; color: var(--hp-label-secondary); }

/* ============ 14. 上传控件（知识库导入） ============ */
/* Quasar 的 QUploader 默认那圈虚线边框太重，压一压，跟卡片里其它元素一个语气 */
.hp-upload { width: 100%; border: .5px dashed var(--hp-border-l3); border-radius: var(--hp-radius-md); }
.hp-upload .q-uploader__header { background: transparent; color: var(--hp-label-secondary); }
.hp-upload .q-uploader__list { background: transparent; }

/* ============ 15. 任务页（进度条 / 行内动作 / 徽标） ============ */
/* 进度条**不做动画**：界面是软件渲染（为了修白板把 GPU 关了），动画会明显掉帧。 */
.hp-progress {
  width: 120px; height: 3px;
  border-radius: 2px;
  background: var(--hp-border-l2);
  overflow: hidden;
}
.hp-progress-fill { height: 100%; background: var(--q-primary, #4a7dff); }
.hp-progress-indeterminate { width: 35%; height: 100%; background: var(--hp-label-tertiary); opacity: .45; }
.hp-progress-cell { width: 120px; }

/* 行内动作：平时不显示，鼠标移到这一行才浮现（减少视觉噪音） */
.hp-row-actions { opacity: 0; }
.hp-row-item:hover .hp-row-actions { opacity: 1; }

/* 左栏的小徽标（有活跃任务时才显示数字） */
.hp-badge {
  min-width: 18px; height: 18px; padding: 0 5px;
  border-radius: 9px;
  background: var(--hp-bg-hover-solid);
  color: var(--hp-label-secondary);
  font-size: 11px; line-height: 18px; text-align: center;
}

/* ============ 16. 分页与筛选（任务页 / 知识库页共用） ============ */
.hp-pager { min-height: 32px; }
/* Quasar 的 QPagination 默认字有点大、上下留白多，压一压跟卡片其它元素一个语气 */
.hp-pagination .q-pagination__content { font-size: 13px; }
.hp-pagination .q-btn { min-height: 24px; min-width: 24px; }
.hp-page-size { width: 116px; }
.hp-search { width: 200px; }

/* ============ 17. 列表卡骨架（卡头 → 工具栏 → 主体 → 页脚） ============ */
/* 两个列表页（任务 / 资料）**共用同一套顺序**：用户换页不用重新找控件（见 §13.8）。 */
.hp-toolbar { min-height: 34px; }
.hp-list-footer { border-top: .5px solid var(--hp-border-l1); padding-top: 8px; }
/* 自动刷新那三件套（开关 + 时间戳 + 刷新）语气要轻：它是"信任装置"，不是主操作 */
.hp-auto-switch { font-size: 12px; }
.hp-auto-switch .q-toggle__label { font-size: 12px; color: var(--hp-label-secondary); }

/* ============ 18. 任务详情（事件时间线） ============ */
.hp-detail-card { width: 720px; max-width: 92vw; }
.hp-event { border-left: 2px solid var(--hp-border-l2); padding-left: 10px; margin-top: 6px; }
/* 报错原文用等宽字体：traceback 与路径靠对齐才看得清 */
.hp-detail-mono {
  font-family: var(--hp-font-family-code);
  font-size: 11px;
  line-height: 17px;
  color: var(--hp-label-secondary);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 220px;
  overflow: auto;
}

/* ============ 19. 批量选择 ============ */
/* 动作条只在选中时出现（见 §13.8）：占着地方却点不动最烦人 */
.hp-pick-bar {
  border-top: 1px solid var(--hp-border-l2);
  border-bottom: 1px solid var(--hp-border-l2);
  padding: 4px 12px;
  background: var(--hp-bg-l2);
}
.hp-pick { color: var(--hp-label-tertiary); }
.hp-pick:hover { color: var(--hp-accent); }
"""


def apply() -> None:
    """把观感注入当前页面。

    为什么每个页面都调一次：NiceGUI 的 ``add_head_html`` 是 per-page 的，
    页面自己保证观感到位，就不依赖"入口有没有先跑过"。
    """
    ui.add_head_html(f"<style>{STYLE}</style>")
