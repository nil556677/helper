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

#: 全部样式。分区：token → 基础 → 左栏 → 顶栏 → 消息流 → 引用 → 输入区 →
#: 等待反馈/底栏统计 → 右栏。
STYLE = """
/* ============ 1. 设计 token（浅色，数值取自 DSH 主题包） ============ */
:root {
  /* 底色与层次：DSH 浅色下 bg-layer-* 与 bg-base 同为纯白，层次靠描边与淡底表达 */
  /* 页面底色（浅灰）与卡片表面（白）：层次靠"底灰 + 白卡 + 描边"表达 */
  --hp-bg-page: #f6f7f9;
  --hp-bg-base: #ffffff;
  --hp-bg-layer-1: #ffffff;
  --hp-surface-2: #f7f8fa;
  /* 交互淡底 */
  --hp-bg-hover: #1118270a;
  --hp-bg-hover-solid: #f2f4f7;

  --hp-label-primary: #111418;
  --hp-label-secondary: #5b6570;
  --hp-label-tertiary: #8a929c;

  --hp-border-l1: #f0f2f5;
  --hp-border-l2: #e6e8ec;
  --hp-border-l3: #d9dde3;
  --hp-border-l4: #c4cad2;

  --hp-brand: #1b1f24;
  --hp-on-brand: #ffffff;
  /* 主色（检索/知识/主操作）全套派生：实心、hover、文字、淡底、描边 —— 界面不再一片灰 */
  --hp-accent: #2f6fed;
  --hp-accent-hover: #245bd0;
  --hp-accent-fg: #1d5bd6;
  --hp-accent-soft: #edf3fe;
  --hp-accent-soft-strong: #dde9fd;
  --hp-accent-border: #b9cffb;
  /* 语义色（成功/警告/危险）公用的"文字 + 淡底 + 描边"派生，一处改全花开 */
  --hp-success: #2e9e63;
  --hp-success-soft: #e7f4ec;
  --hp-warn: #c98a1e;
  --hp-warn-soft: #fbf3e2;
  --hp-danger: #d14343;
  --hp-danger-soft: #fbeaea;
  --hp-danger-border: #efb6b6;
  --hp-negative: #d14343;
  --hp-warning: #c98a1e;

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
  --hp-measure: 880px;   /* 聊天正文的阅读宽度 */
}

/* ============ 2. 基础 ============ */
html, body { height: 100%; }
body {
  background: var(--hp-bg-page);
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
/* 设置页内容区：**按百分比**取宽（窄屏自然收、宽屏不空），上限只防超宽屏 */
.hp-page { width: 94%; max-width: 1440px; margin-left: auto; margin-right: auto; }
.hp-page-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }

/* ============ 3. 左栏（导航） ============ */
.hp-sidebar { background: var(--hp-bg-base); border-right: .5px solid var(--hp-border-l2) !important; }
.hp-brand { color: var(--hp-label-primary); font-weight: 600; letter-spacing: .08em; }
/* 品牌徽标图标用主色：给左栏一个"这是谁"的身份色，而不是和普通文字一样灰 */
.hp-brand-icon { color: var(--hp-accent); }

/* 左栏里"整行可点、左对齐"的按钮（「新建对话」「返回聊天」等）。
   `.hp-btn-ghost` = 次要按钮档：与 `.hp-row-button` 同款灰字、hover 才显色 */
.hp-row-button, .hp-btn-ghost {
  border-radius: var(--hp-radius-md);
  color: var(--hp-label-secondary);
  justify-content: flex-start;
  min-height: 38px;
  transition: background-color .12s ease, color .12s ease;
}
.hp-row-button:hover, .hp-btn-ghost:hover { background: var(--hp-bg-hover); color: var(--hp-label-primary); }

/* 会话列表（左栏）：一行一个会话，标题超长省略号。
   注意这些"行"是 **button**（不是 ui.item）：实测 `ui.item` 的点击在真实浏览器里不触发。 */
.hp-conv-row { justify-content: flex-start; flex-wrap: nowrap; }
.hp-conv-title { flex: 1 1 auto; min-width: 0; justify-content: flex-start; }
/* 右侧动作组（重命名 + 删除）：固定成一组贴行尾，不参与标题的伸缩 */
.hp-conv-actions {
  display: flex; align-items: center; justify-content: flex-end;
  gap: 0; flex: none; height: 100%;
}
.hp-conv-actions > .q-btn { flex: none; align-self: center; }
.hp-conv-title .q-btn__content {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}
.hp-conv-delete {
  opacity: 0;  /* 和重命名一样：鼠标移到该条会话才浮现 */
  color: var(--hp-label-tertiary);
  flex: none;
  transition: opacity .12s ease, color .12s ease;
}
.hp-conv-row:hover .hp-conv-delete { opacity: 1; color: var(--hp-negative); }
/* 重命名图标：只在 hover 浮现（比删除更轻；删除是常驻渐显） */
.hp-conv-edit {
  opacity: 0;
  color: var(--hp-label-tertiary);
  flex: none;
  transition: opacity .12s ease, color .12s ease;
}
.hp-conv-row:hover .hp-conv-edit { opacity: 1; color: var(--hp-label-secondary); }
/* 改名态的输入框：占满、去掉自带边框，跟普通标题一个语气 */
.hp-conv-title-edit { flex: 1; min-width: 0; }
.hp-conv-title-edit .q-field__control { min-height: 24px; padding: 0; }
.hp-row-active .hp-conv-title { color: inherit !important; }
/* 当前选中那一行（会话列表 / 功能导航 / 设置分区共用）。
   主色淡底 + 左侧 3px 色条 + 主色文字 → "我现在在哪"一眼可辨，不再是灰蒙蒙一片 */
.hp-row-active {
  background: var(--hp-accent-soft) !important;
  color: var(--hp-accent-fg) !important;
  box-shadow: inset 3px 0 0 0 var(--hp-accent);
}
.hp-row-active:hover { background: var(--hp-accent-soft-strong) !important; }

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
/* 输入框上方的“待发送队列”条目：浅底 + 细边，和输入框区分开 */
.hp-queue-item {
  background: var(--hp-bg-layer-1);
  border: .5px solid var(--hp-border-l2);
  border-radius: 8px;
  padding: 4px 8px;
}
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
.hp-attachment-chip {
  background: var(--hp-bg-layer-2);
  border: .5px solid var(--hp-border-l2);
  border-radius: 8px;
  padding: 2px 6px;
}
.hp-attach-thumb {
  width: 42px;
  height: 42px;
  object-fit: cover;
  border-radius: 6px;
}
.hp-attach-thumb-sm {
  width: 24px;
  height: 24px;
  object-fit: cover;
  border-radius: 4px;
}
.hp-attach-name {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hp-attach-wrap {
  position: relative;
  width: 36px;
  height: 36px;
  flex: none;
  margin-bottom: 4px;
  overflow: hidden;
}
.hp-attach-wrap .hp-attach-btn {
  position: absolute;
  inset: 0;
  z-index: 1;
  color: var(--hp-label-tertiary);
}
.hp-attach-wrap .hp-attach-btn:hover { color: var(--hp-label-primary); }
.hp-attach-wrap .hp-attach-btn .q-icon { font-size: 20px; }
.hp-attach-wrap .hp-attach-overlay {
  position: absolute;
  inset: 0;
  z-index: 2;
  width: 36px;
  height: 36px;
  opacity: 0;
  overflow: hidden;
}
.hp-attach-wrap .q-uploader {
  width: 36px;
  max-width: 36px;
  min-width: 0 !important;
  height: 36px;
  max-height: 36px;
  min-height: 0;
  border: 0;
  background: transparent;
}
.hp-attach-wrap .q-uploader__header {
  width: 36px;
  max-width: 36px;
  height: 36px;
  min-height: 36px;
  padding: 0;
  background: transparent;
}
.hp-attach-wrap .q-uploader__list { display: none; }

/* 批量导入/导出文本框：右下角可鼠标纵向拖拽改变高度；超过视口 60% 后内部滚动 */
.hp-resize-textarea textarea {
  resize: vertical !important;
  min-height: 160px;
  max-height: 60vh;
  overflow: auto;
}
.hp-send, .hp-btn-primary {
  background: var(--hp-accent) !important;
  color: var(--hp-on-brand) !important;
  border-radius: var(--hp-radius-sm) !important;
  box-shadow: 0 1px 2px 0 color-mix(in srgb, var(--hp-accent) 30%, transparent);
}
.hp-send:hover, .hp-btn-primary:hover { background: var(--hp-accent-hover) !important; opacity: 1; }

/* ============ 7.5 等待反馈与底栏统计（DSH 简化版） ============ */
/* 等待中的"正在检索…"行：DSH 的 running status 是蓝色 + 微光扫过 + 鲸鱼图标。
   没搬它的 PNG 资源，用主题图标 + 蓝色微光 + 跳动点 + 秒表，观感同一档。 */
.hp-running-row { color: var(--hp-accent); }
.hp-running-icon { color: var(--hp-accent); }
.hp-running-text { font-size: 13px; line-height: 22px; }
.hp-shimmer-accent {
  background-image: linear-gradient(90deg, var(--hp-accent) 0%, var(--hp-accent) 35%,
                    #7ba7f6 50%, var(--hp-accent) 65%, var(--hp-accent) 100%);
  background-size: 300% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: hp-shimmer 1.6s linear infinite;
}
/* 跳动点：纯 CSS，不占 JS 循环；reduced-motion 下退化成静态点 */
.hp-dots { display: inline-flex; gap: 3px; align-items: center; flex: none; }
.hp-dots > span {
  width: 4px; height: 4px; border-radius: 50%;
  background: currentColor; opacity: .35;
  animation: hp-dot 1.2s ease-in-out infinite;
}
.hp-dots > span:nth-child(2) { animation-delay: .2s; }
.hp-dots > span:nth-child(3) { animation-delay: .4s; }
@keyframes hp-dot {
  0%, 60%, 100% { opacity: .3; transform: none; }
  30% { opacity: 1; transform: translateY(-1px); }
}
@media (prefers-reduced-motion: reduce) {
  .hp-shimmer-accent { animation: none; background-image: none; color: var(--hp-accent); }
  .hp-dots > span { animation: none; opacity: .6; }
}
.hp-running-clock {
  font-size: 12px; font-variant-numeric: tabular-nums;
  color: var(--hp-accent); opacity: .75;
}

/* 底栏统计（DSH composer dock 简化版）：一行小字 + 上下文占用条。
   进度条**不做动画**（软件渲染下动画掉帧，见任务页的说明），宽度直接按值定。 */
.hp-composer-stats {
  min-height: 22px;
  padding: 3px 2px 0;
  flex-wrap: wrap;
  row-gap: 2px;
}
.hp-stat-pill {
  font-size: 12px; line-height: 20px;
  color: var(--hp-label-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
/* 上下文占用条：三段分别是 用户 / 回答 / 引用，宽度按"占参考窗口的比例"算 */
.hp-context-bar {
  width: 120px; height: 4px; border-radius: 2px;
  background: var(--hp-border-l2);
  overflow: hidden;
  display: inline-flex;
  flex: none;
}
.hp-context-bar > span { height: 100%; flex: none; }
.hp-context-user { background: var(--hp-accent); }
.hp-context-answer { background: var(--hp-accent-soft-strong); }
.hp-context-ref { background: var(--hp-border-l4); }
/* 上下文明细：点统计行数字弹出的小弹窗（贴着点击位置，不再撑开整行） */
.hp-context-pop { max-width: 420px; }
.hp-context-pop .q-menu__content { max-height: 60vh; overflow: auto; padding: 8px 10px; }

/* 工作步骤展示（固定「标准」档）：过程组折起，点开看推理预览与步骤明细 */
/* 过程组里的每一步：一行（图标 + 标题 + 箭头），点开看明细。
   嵌套 Quasar expansion 默认带很大的 margin/padding，这里统一压平。 */
.hp-step,
.hp-reasoning {
  width: 100%;
  background: transparent;
  margin: 0 !important;
}
.hp-step .q-expansion-item__container,
.hp-reasoning .q-expansion-item__container { border: 0 !important; }
.hp-step .q-item,
.hp-reasoning .q-item { min-height: 22px; padding: 0; color: var(--hp-label-tertiary); }
.hp-step .q-item__section--main,
.hp-reasoning .q-item__section--main { padding: 0; }
.hp-step .q-expansion-item__content,
.hp-reasoning .q-expansion-item__content { padding: 0 0 2px 20px; }
.hp-step .q-item__label,
.hp-reasoning .q-item__label { font-size: 12.5px; line-height: 20px; }

.hp-step-row,
.hp-reasoning-row { color: var(--hp-label-tertiary); }
.hp-step-row:hover,
.hp-reasoning-row:hover { color: var(--hp-label-secondary); }
.hp-reasoning > summary {
  list-style: none;
  display: flex;
  align-items: center;
  cursor: pointer;
}
.hp-reasoning > summary::-webkit-details-marker { display: none; }
.hp-reasoning > summary::after {
  content: "▸";
  margin-left: auto;
  color: var(--hp-label-tertiary);
  font-size: 12px;
  transition: transform .12s ease;
}
.hp-reasoning[open] > summary::after {
  content: "▾";
}
.hp-reasoning > .hp-md-compact {
  padding: 4px 0 4px 20px;
}
.hp-step-icon, .hp-reasoning-icon { color: inherit; }
.hp-step-title, .hp-reasoning-title { font-size: 12.5px; line-height: 20px; }
.hp-step-error { color: var(--hp-negative); }
.hp-reasoning-preview {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0;
}
.hp-step-detail-text {
  color: var(--hp-label-tertiary);
  font-size: 12px; line-height: 20px;
  white-space: pre-wrap;      /* 明细里的换行（查询词/原因/每条命中）要保住 */
  word-break: break-word;
}
/* 推理块正文：比答案小一号、语气更轻（它是"过程"，不是结论） */
.hp-md-compact { font-size: 12.5px; line-height: 20px; color: var(--hp-label-secondary); }
.hp-md-compact ul { margin: 2px 0; padding-left: 18px; }

/* ============ 7.7 资料区（引用折叠条） ============ */
/* 默认收起：只露一行"资料 · N 段"，点开才列出每条出处。
   不把来源整行铺在答案下面——长答案会拖出一条很长的清单。 */
.hp-sources-row {
  width: 100%;
  background: transparent;
  margin: 0 !important;
}
.hp-sources-row .q-expansion-item__container { border: 0 !important; }
.hp-sources-row .q-item { min-height: 24px; padding: 0; color: var(--hp-label-tertiary); }
.hp-sources-row .q-item__section--main { padding: 0; }
.hp-sources-row .q-expansion-item__content { padding: 0; }
.hp-sources-row .q-item__label { font-size: 12px; line-height: 20px; }
.hp-sources-head:hover { color: var(--hp-label-secondary); }
.hp-sources-title { font-size: 12px; line-height: 20px; }
.hp-source-line { color: var(--hp-label-secondary); }
.hp-source-name {
  font-size: 12px; line-height: 20px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  cursor: pointer;
}
.hp-source-name:hover { color: var(--hp-accent); }
/* 右栏里的"命中片段"：左侧主色竖条 + 淡底，指明到底哪一段命中了 */
.hp-snippet {
  border-left: 2px solid var(--hp-accent);
  background: var(--hp-accent-soft);
  border-radius: 0 var(--hp-radius-sm) var(--hp-radius-sm) 0;
  padding: 6px 10px;
  font-size: 12.5px; line-height: 20px;
  color: var(--hp-label-primary);
  white-space: pre-wrap;
  word-break: break-word;
}

/* ============ 8. 右栏（贴边面板） ============ */
/* 详情页「分块明细」的行内展开：默认很占高，压扁成一行摘要；展开内容保持紧凑。 */
.hp-chunk-expansion { border-top: .5px solid var(--hp-border-l1); }
.hp-chunk-expansion .q-item { min-height: 34px; padding: 0 8px; }
.hp-chunk-expansion .q-item__label { font-size: 12.5px; color: var(--hp-label-primary); }
.hp-chunk-expansion .q-expansion-item__content { padding: 4px 8px 10px; }
.hp-panel { background: var(--hp-bg-base); border-left: .5px solid var(--hp-border-l2) !important; }
/* 左缘拖拽条：贴在手柄位置的可视化提示 + 拖动时的光标。真正的拖动逻辑在 shell 注入的 JS 里，
   这里只负责"看得见、抓得住"——命中区 8px，悬停时亮一条竖线。 */
.hp-panel::before {
  content: "";
  position: absolute; left: 0; top: 0; bottom: 0; width: 8px;
  cursor: col-resize;
  z-index: 3000;
  touch-action: none;
  background: transparent;
  transition: background .12s ease;
}
.hp-panel::before:hover { background: linear-gradient(to right, var(--hp-border-l3), transparent); }
body.hp-panel-dragging, body.hp-panel-dragging * { cursor: col-resize !important; user-select: none !important; }
/* 右栏（原件预览）：头行固定、内容自己滚，关闭 × 永远看得见 */
.hp-panel-body-wrap { display: flex; flex-direction: column; }
.hp-panel-head {
  flex: none;
  padding: 10px 14px;
  border-bottom: .5px solid var(--hp-border-l2);
}
.hp-panel-body { flex: 1; min-height: 0; overflow-y: auto; padding: 12px 14px 20px; }
.hp-icon-button, .hp-btn-icon {
  border-radius: var(--hp-radius-sm);
  color: var(--hp-label-secondary);
  min-height: 30px; min-width: 30px;
}
.hp-icon-button:hover, .hp-btn-icon:hover { background: var(--hp-bg-hover); color: var(--hp-label-primary); }
/* 图标按钮的语义色：列表行里的纯图标动作靠这个一眼分主次（查看=蓝 / 危险=红 / 重建=橙），
   不再是一排分不清的灰色圆形 */
.hp-icon-view { color: var(--hp-accent); }
.hp-icon-view:hover { background: var(--hp-accent-soft); color: var(--hp-accent-fg); }
.hp-icon-danger { color: var(--hp-danger); }
.hp-icon-danger:hover { background: color-mix(in srgb, var(--hp-danger) 13%, transparent); color: var(--hp-danger); }
.hp-icon-warn { color: var(--hp-warn); }
.hp-icon-warn:hover { background: color-mix(in srgb, var(--hp-warn) 14%, transparent); color: var(--hp-warn); }

/* ============ 9. 设置页 ============ */
/* 设置是主区里的一个完整页面：内容居中限宽，分区导航在左栏（由外壳负责，见 app/shell.py） */
.hp-page-title { font-size: 18px; line-height: 26px; font-weight: 600; color: var(--hp-label-primary); letter-spacing: -.01em; }
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
  border: 1px solid var(--hp-border-l2);
  border-radius: var(--hp-radius-md);
  padding: 14px 16px;
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

/* 轮次轨道（学 DSH）：右侧一列刻度，一条 = 一轮；当前轮加长加深 */
.hp-turn-rail {
  width: 34px; flex: none; padding: 8px 0;
  display: flex; flex-direction: column; align-items: center; justify-content: flex-start;
  gap: 6px; overflow: hidden; user-select: none;
}
.hp-turn-rail-label { font-size: 11px; line-height: 1; color: var(--hp-label-tertiary); }
.hp-turn-ticks {
  display: flex; flex-direction: column; align-items: center;
  gap: 4px; overflow-x: hidden; overflow-y: auto;
}
.hp-turn-tick {
  width: 16px; height: 4px; border-radius: 2px; flex: none;
  background: var(--hp-border-l4); cursor: pointer; transition: width .12s, background .12s;
}
.hp-turn-tick:hover { background: var(--hp-label-secondary); }
.hp-turn-tick--active { width: 26px; background: var(--hp-accent); }
.hp-turn-tick--active:hover { background: var(--hp-accent); }
/* 窄窗口（约 900px 以下）隐藏轨道：正文更重要 */
@media (max-width: 720px) { .hp-turn-rail { display: none; } }

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
.hp-note-error { color: var(--hp-negative); font-size: 13px; line-height: 22px; }
.hp-note-warning { color: var(--hp-warning); font-size: 13px; line-height: 22px; }

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
  position: relative;
  background: var(--hp-bg-base);
  border: 1px solid var(--hp-border-l2);
  border-radius: var(--hp-radius-md);
  /* 内边距收紧：卡片标题/边框占据的空白是设置页"太空"的主因，从 16/20 收到 11/15 */
  padding: 11px 15px;
  box-shadow: 0 1px 2px 0 #0f111508;
  transition: box-shadow .15s ease, border-color .15s ease, transform .15s ease;
}
/* 卡片左侧的主色细条：hover 时出现，给"这是一张可交互的设置卡"一点色彩身份 */
.hp-card:not(.hp-dialog):not(.hp-slot)::before {
  content: "";
  position: absolute; left: 0; top: 12px; bottom: 12px; width: 3px;
  border-radius: 2px;
  background: transparent;
  transition: background .15s ease;
}
.hp-card:not(.hp-dialog):not(.hp-slot):hover::before { background: var(--hp-accent); }
.hp-card:hover {
  box-shadow: 0 4px 12px 0 #0f11151c, 0 1px 3px 0 #0f111514;
  border-color: var(--hp-border-l3);
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

/* 状态点：未配置=灰、已配置/测试通过=绿、测试失败=红（颜色靠这几个类切）。
   定义在主题变量处统一为一份（这里只有工具类，避免两处 .hp-status 互相覆盖）。 */

/* ============ 13. 列表（卡片内的表格：表头 + 行） ============ */
.hp-row-head {
  font-size: 12px;
  color: var(--hp-label-tertiary);
  padding: 8px 0;
  border-bottom: 1px solid var(--hp-border-l2);
}
.hp-row-item {
  font-size: 13px;
  color: var(--hp-label-primary);
  padding: 10px 8px;
  border-bottom: 1px solid var(--hp-border-l1);
  border-radius: var(--hp-radius-sm);
  transition: background-color .12s ease;
}
.hp-row-item:hover { background: var(--hp-bg-hover); }
.hp-col-name { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hp-col { width: 76px; text-align: right; color: var(--hp-label-secondary); }
/* 固定列宽：flex 0 0 = 不伸不缩。表头与行用同一套，列才永远对齐。 */
.hp-col-check { flex: 0 0 30px; width: 30px; display: flex; justify-content: center; }
.hp-col-size { flex: 0 0 84px; width: 84px; text-align: right; color: var(--hp-label-secondary); font-variant-numeric: tabular-nums; }
.hp-col-chunks { flex: 0 0 68px; width: 68px; text-align: right; color: var(--hp-label-secondary); font-variant-numeric: tabular-nums; }
.hp-col-status { flex: 0 0 118px; width: 118px; display: flex; justify-content: flex-end; align-items: center; }
.hp-col-actions { flex: 0 0 152px; width: 152px; display: flex; justify-content: flex-end; align-items: center; gap: 2px; }
.hp-col-time { flex: 0 0 92px; width: 92px; text-align: right; color: var(--hp-label-tertiary); font-size: 12px; }
.hp-col-left { flex: 0 0 76px; width: 76px; text-align: left; color: var(--hp-label-secondary); }

/* ============ 13.5 知识库的视图页签与可点元素 ============ */
/* 三个页签（总览 / 资料列表 / 导入）：胶囊形，选中用主色实心——和左栏的"分区"区分开 */
.hp-tab {
  border: .5px solid var(--hp-border-l2);
  border-radius: 999px;
  color: var(--hp-label-secondary);
  min-height: 30px;
  padding: 0 12px;
}
.hp-tab:hover { background: var(--hp-bg-hover); color: var(--hp-label-primary); }
/* 选中态：Quasar 的 flat 按钮有自己的前景色，必须 !important 才压得住 */
.hp-tab-active,
.hp-tab-active .q-btn__content { color: var(--hp-on-brand) !important; }
.hp-tab-active { background: var(--hp-accent) !important; border-color: var(--hp-accent) !important; }

/* 可点的文字（文件名 / 详情链接）：鼠标与悬停色给出"能点"的提示 */
.hp-clickable { cursor: pointer; }
.hp-clickable:hover { color: var(--hp-accent); }
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
.hp-progress-fill { height: 100%; background: var(--hp-accent); }
/* 不知道总数时：整条铺浅色条纹（静态，不做动画——软件渲染下动画掉帧），
   避免旧样式"35% 灰条"看起来像卡在 35%。 */
.hp-progress-indeterminate {
  width: 100%; height: 100%;
  background: repeating-linear-gradient(
    45deg,
    var(--hp-border-l2) 0 4px,
    var(--hp-label-tertiary) 4px 8px
  );
  opacity: .55;
}
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
  border: 1px solid var(--hp-border-l2);
  border-radius: var(--hp-radius-sm);
  padding: 6px 12px;
  background: var(--hp-surface-2);
}
.hp-pick { color: var(--hp-label-tertiary); }
.hp-pick:hover { color: var(--hp-accent); }

/* ============ 20. 统一弹窗（components/dialog.py） ============ */
/* 弹窗卡片：比普通设置卡更稳一点，固定宽度、白底、明显的层级阴影与描边。 */
.hp-dialog {
  max-width: 460px;
  width: auto;
  background: var(--hp-bg-base);
  border: 1px solid var(--hp-border-l3);
  border-radius: var(--hp-radius-lg);
  box-shadow: var(--hp-shadow-lv2), 0 12px 32px 0 #0000001f;
}
/* 软件渲染下，Quasar 对话框的淡入/缩放过渡要逐帧重绘，观感就是"卡顿 + 页面闪一下"。
   项目原则本就是不放大面积动画（见 app/screens/settings 里对弹框的说明）——直接关掉
   弹窗的开合过渡与遮罩淡显，快而稳。对所有 ui.dialog（confirm/提示/任务详情）统一生效。 */
.q-dialog__backdrop,
.q-dialog__inner,
.q-dialog__inner--minimized,
.q-dialog--animating {
  transition: none !important;
  animation: none !important;
}
/* 弹窗里的"知道了/确定"主按钮：保持主色，但略强调 */
.hp-dialog-ok { background: var(--hp-accent) !important; color: var(--hp-on-brand) !important; }
.hp-dialog-ok:hover { background: var(--hp-accent-hover) !important; }
/* 危险确认：删数据/清空这类，按钮用红（与状态点错误红一致） */
.hp-confirm-danger {
  background: var(--hp-negative) !important;
  color: #ffffff !important;
}
.hp-confirm-danger:hover { opacity: .88; }
/* 统一 toast：圆角、克制阴影、描边，不与默认 Quasar 角标打架 */
.hp-toast { border-radius: var(--hp-radius-sm); box-shadow: var(--hp-shadow-lv2); }
/* ============ 21. 表单控件状态（Quasar 输入/下拉） ============ */
.hp-search, .hp-filter { border-radius: var(--hp-radius-sm); }
.hp-search .q-field__control, .hp-filter .q-field__control { border-radius: var(--hp-radius-sm); }
/* focus 时整块给一圈强调色光环（只在 outline 风格上生效） */
.q-field--outlined .q-field__control:focus-within {
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--hp-accent) 18%, transparent);
  border-radius: var(--hp-radius-sm);
}

/* ============ 22. 空状态与统计网格 ============ */
.hp-empty {
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  padding: 28px 12px; color: var(--hp-label-tertiary); text-align: center;
}
.hp-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
  gap: 10px;
}
.hp-stat-tile {
  padding: 10px 12px; border-radius: var(--hp-radius-sm);
  background: var(--hp-surface-2); border: 1px solid var(--hp-border-l1);
  border-top: 2px solid color-mix(in srgb, var(--hp-accent) 45%, transparent);
}
.hp-stat-tile .v { font-size: 18px; line-height: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }
.hp-stat-tile .n { font-size: 12px; line-height: 19px; color: var(--hp-label-tertiary); }

/* ============ 23. 页签条（知识库三视图） ============ */
.hp-tabs { display: flex; align-items: center; gap: 6px; }

/* ============ 24. 任务分组胶囊 + 状态胶囊 ============ */
.hp-group-btn {
  border: 1px solid var(--hp-border-l2);
  border-radius: 999px;
  color: var(--hp-label-secondary);
  min-height: 30px;
  padding: 0 12px;
  background: var(--hp-bg-base);
}
.hp-group-btn:hover { background: var(--hp-bg-hover); color: var(--hp-label-primary); }
.hp-group-btn-active,
.hp-group-btn-active .q-btn__content { color: var(--hp-on-brand) !important; }
.hp-group-btn-active {
  background: var(--hp-accent) !important;
  border-color: var(--hp-accent) !important;
}
/* 状态文字：浅底胶囊（任务/模型等页共用）；前面的状态点让"灰=未配置"一眼可辨 */
.hp-status {
  display: inline-flex; align-items: center;
  font-size: 12px; padding: 2px 9px; border-radius: 999px;
  background: var(--hp-surface-2); color: var(--hp-label-secondary);
}
.hp-status::before {
  content: "";
  width: 6px; height: 6px;
  border-radius: 50%;
  background: currentColor;
  opacity: .55;
  margin-right: 6px;
}
.hp-status-ok {
  color: var(--hp-positive);
  background: color-mix(in srgb, var(--hp-positive) 12%, transparent);
}
.hp-status-bad {
  color: var(--hp-negative);
  background: color-mix(in srgb, var(--hp-negative) 12%, transparent);
}
.hp-status-chip { font-size: 12px; }

/* ============ 25. 危险动作与选中行（审查修复新增） ============ */
/* 危险按钮（清空/删除这类，不进确认框也要有红色观感）：红字 + hover 淡红底 */
.hp-danger, .hp-btn-danger {
  color: var(--hp-danger) !important;
  background: var(--hp-danger-soft);
  border: .5px solid color-mix(in srgb, var(--hp-danger) 22%, transparent);
  border-radius: var(--hp-radius-sm);
}
.hp-danger:hover, .hp-btn-danger:hover {
  background: color-mix(in srgb, var(--hp-danger) 13%, transparent) !important;
  color: var(--hp-danger) !important;
  border-color: color-mix(in srgb, var(--hp-danger) 40%, transparent);
}

/* 批量选中的行：整行浅主色底（只换图标扫一眼分不清选了哪几行） */
.hp-row-selected { background: color-mix(in srgb, var(--hp-accent) 8%, transparent); }
.hp-row-selected:hover { background: color-mix(in srgb, var(--hp-accent) 12%, transparent); }

/* 列表行的响应式：窄窗口时次要列整列隐藏，不挤压名称列 */
@media (max-width: 900px) {
  .hp-col-hide-sm { display: none !important; }
}
@media (max-width: 1180px) {
  .hp-col-hide-md { display: none !important; }
}

/* 路径行（path_row）：单行省略替代 break-all 硬折行——悬停 tooltip 看全文，旁边有复制按钮 */
.hp-path-inline {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 暂停队列时整页横幅（黄色，与状态点的语义一致） */
.hp-paused-banner {
  border: 1px solid color-mix(in srgb, var(--hp-warning) 35%, transparent);
  background: color-mix(in srgb, var(--hp-warning) 10%, transparent);
  border-radius: var(--hp-radius-sm);
  padding: 8px 12px;
  color: var(--hp-warning);
  font-size: 13px;
}
"""


def apply() -> None:
    """把观感注入当前页面。

    为什么每个页面都调一次：NiceGUI 的 ``add_head_html`` 是 per-page 的，
    页面自己保证观感到位，就不依赖"入口有没有先跑过"。
    """
    from loguru import logger

    # 一行日志便于排障：样式是**静默生效**的，注没注入只能看这里
    logger.debug("主题样式已注入：{} 字节（含统计网格 {}）", len(STYLE), ".hp-stats" in STYLE)
    ui.add_head_html(f"<style>{STYLE}</style>")
