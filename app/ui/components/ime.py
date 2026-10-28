"""输入法（IME）安全：给"回车/ Esc 触发动作"的输入框加一层浏览器侧保护。

**为什么需要**：中文/日文输入法组词时，回车是"确认候选词"、Esc 是"取消候选词"，
这两个键在组词状态下**不应**触发业务动作（提交改名、保存配置）。浏览器在组词期间
会给出 ``isComposing``（部分旧版/输入法给 ``keyCode === 229``），据此过滤即可。

**用法**：当作 NiceGUI ``Element.on(..., js_handler=...)`` 的 js_handler——
组词中不 emit（服务端收不到事件，输入框保持原样），非组词时 emit（走正常处理）。

```python
field.on('keydown.enter', submit, js_handler=IME_ENTER_GUARD)
```
"""

from __future__ import annotations

#: 组词中不 emit；否则 emit 给服务端（默认行为）。
IME_ENTER_GUARD = "(e) => { if (e && (e.isComposing || e.keyCode === 229)) return; emit(); }"
