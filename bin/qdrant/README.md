# Qdrant 服务端（可选，但服务端模式需要）

主程序不再要求 `config.json`；Qdrant 地址/端口存数据库，界面「设置 · 通用 · 本机服务」里改。
本地嵌入式模式不需要这个目录；切到**服务端模式**时，主程序会从这个目录启动 Qdrant，
并随主程序启动/退出（不会留下单独常驻的服务进程）。

## 放置

1. 从官方 release 下载 Windows 版：
   <https://github.com/qdrant/qdrant/releases/latest>
   文件名形如 `qdrant-x86_64-pc-windows-msvc.zip`；
2. 解压后把 `qdrant.exe` 放到本目录：

   ```
   bin/qdrant/qdrant.exe
   ```

3. 打开 helper → 设置 → 通用 → 本机服务：
   - 填 Qdrant HTTP 端口（默认 6333），点「保存端口并应用」；
   - 首次从本地嵌入式切换会提示「重启 helper 后生效」；
   - 重启后主程序会启动 Qdrant + worker，UI 不再打开本地嵌入式目录。

## 数据位置

服务端数据在 `data/qdrant-server/`（与本地嵌入式的 `data/qdrant/` 是两套存储）。
**切换后旧向量不会自动迁移**：资料需要重新导入，或在知识库列表里「重建」。
切换前建议先备份 `data/`。

## 端口被占

界面会提示端口占用；换一个端口保存即可。端口写入数据库（`settings.vector.url`），
重启服务后生效；Qdrant 启动失败时看 `data/logs/qdrant.log`。

## 生命周期

- 主程序启动时：若 `vector.url` 非空且本文件存在 → 启动 Qdrant sidecar + worker；
- 主程序退出时：先停 worker，再停 Qdrant；
- 手动停止/重启：设置页的「保存端口并应用」「刷新状态」。
