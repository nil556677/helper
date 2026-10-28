"""应用包根：界面（`app/ui`）→ 编排（`app/services`）→ 引擎（`app/core`）。

红线：`app/ui` 不 import `app/core` 的内部实现（一律走 services），`app/core` 不 import 界面。
"""
