# Qdrant 服务端（可选，服务端模式需要）

这里**只提交说明和下载脚本**；`qdrant.exe`、`static/`、运行时文件都不入库。

## 一键下载

在仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File bin\qdrant\download.ps1
```

可选参数：

```powershell
# 强制重下（覆盖已有文件）
powershell -ExecutionPolicy Bypass -File bin\qdrant\download.ps1 -Force

# 只下服务端，不下 Dashboard 静态文件
powershell -ExecutionPolicy Bypass -File bin\qdrant\download.ps1 -SkipWebUi

# 指定版本
powershell -ExecutionPolicy Bypass -File bin\qdrant\download.ps1 -QdrantVersion v1.19.2 -WebUiVersion v0.2.19
```

脚本会下载：

| 产物 | 用途 | 是否提交 |
|---|---|---|
| `bin/qdrant/qdrant.exe` | Qdrant 服务端（约 80 MB） | 否 |
| `bin/qdrant/static/` | Qdrant Dashboard（`/dashboard`） | 否 |

GitHub 直连失败时会自动尝试 `https://ghproxy.net/` 镜像。

## 手动下载

1. 从官方 release 下载 Windows 版：
   <https://github.com/qdrant/qdrant/releases/latest>
   文件名形如 `qdrant-x86_64-pc-windows-msvc.zip`；
2. 解压后把 `qdrant.exe` 放到本目录：`bin/qdrant/qdrant.exe`；
3. Dashboard 静态文件（可选）：
   <https://github.com/qdrant/qdrant-web-ui/releases>
   下载 `dist-qdrant.zip`，解压内容放到 `bin/qdrant/static/`（其中要有 `index.html`）。

## 启用服务端模式

1. 打开 helper → 设置 → 通用 → 本机服务；
2. 填 Qdrant HTTP 端口（默认 6333），保存并应用；
3. 首次从本地嵌入式切换需要重启 helper；
4. 重启后主程序会启动 Qdrant + worker，数据在 `data/qdrant-server/`。

## 数据位置

- 服务端数据：`data/qdrant-server/`
- 本地嵌入式旧数据：`data/qdrant/`
- 两套存储不自动迁移；切换后旧向量需要重新导入或重建。

## 端口被占

界面会提示端口占用；换一个端口保存，写入数据库 `settings.vector.url`，重启后生效。
Qdrant 启动失败看 `data/logs/qdrant.log`。

## 生命周期

- 启动：`vector.url` 非空且 `bin/qdrant/qdrant.exe` 存在 → 拉起 Qdrant sidecar + worker；
- 退出：先停 worker，再停 Qdrant；
- 手动停止/重启：设置页的「保存端口并应用」「刷新状态」。

## 不提交的文件

`.gitignore` 已忽略：

```text
/bin/qdrant/qdrant.exe
/bin/qdrant/.qdrant-initialized
/bin/qdrant/static/
/bin/qdrant/snapshots/
/bin/qdrant/*.zip
```
