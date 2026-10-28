"""RAG 功能包（净室 v2）：检索 / 证据 / 生成 / 入库编排。

模块：
- ``kb_store``：SQLite documents/chunks 唯一入口；
- ``retrieval``：dense+sparse → RRF → 回填 → 精排 → 证据 的纯函数；
- ``evidence`` / ``answer``：证据组装与生成/引用校验。
"""
