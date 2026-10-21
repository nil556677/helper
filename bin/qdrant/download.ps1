# 下载 Qdrant Windows 服务端 + Web UI 静态文件（下载物不入库）
#
# 用法（在仓库根目录或任意位置）：
#   powershell -ExecutionPolicy Bypass -File bin\qdrant\download.ps1
#   powershell -ExecutionPolicy Bypass -File bin\qdrant\download.ps1 -Force
#   powershell -ExecutionPolicy Bypass -File bin\qdrant\download.ps1 -SkipWebUi
#
# 产物：
#   bin/qdrant/qdrant.exe   （约 80 MB，服务端模式必需）
#   bin/qdrant/static/      （Qdrant Dashboard，可选；没有它 /dashboard 会 404）
#
# 这两个路径已在 .gitignore 中，不会被提交。

param(
    [string]$QdrantVersion = "v1.19.2",
    [string]$WebUiVersion = "v0.2.19",
    [switch]$Force,
    [switch]$SkipWebUi
)

$ErrorActionPreference = "Stop"
$target = $PSScriptRoot
$project = Split-Path -Parent (Split-Path -Parent $target)
$tmp = Join-Path $project ".tmp\qdrant-download"
$exe = Join-Path $target "qdrant.exe"
$static = Join-Path $target "static"

function Get-RemoteFile {
    param([string]$Url, [string]$OutFile)
    Write-Host "下载：$Url"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
        return
    } catch {
        $mirror = "https://ghproxy.net/$Url"
        Write-Warning "直连失败，尝试镜像：$mirror"
        Invoke-WebRequest -Uri $mirror -OutFile $OutFile -UseBasicParsing
    }
}

function Expand-Zip {
    param([string]$Zip, [string]$Destination)
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Expand-Archive -LiteralPath $Zip -DestinationPath $Destination -Force
}

if ((Test-Path -LiteralPath $exe) -and -not $Force) {
    Write-Host "已存在 qdrant.exe（加 -Force 可重下）：$exe"
} else {
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $qdrantZip = Join-Path $tmp "qdrant.zip"
    Get-RemoteFile `
        -Url "https://github.com/qdrant/qdrant/releases/download/$QdrantVersion/qdrant-x86_64-pc-windows-msvc.zip" `
        -OutFile $qdrantZip
    $extract = Join-Path $tmp "qdrant"
    Expand-Zip -Zip $qdrantZip -Destination $extract
    $found = Get-ChildItem -LiteralPath $extract -Recurse -Filter "qdrant.exe" | Select-Object -First 1
    if ($null -eq $found) {
        throw "压缩包里没找到 qdrant.exe"
    }
    Copy-Item -LiteralPath $found.FullName -Destination $exe -Force
    Write-Host "已写入：$exe"
}

if (-not $SkipWebUi) {
    if ((Test-Path -LiteralPath (Join-Path $static "index.html")) -and -not $Force) {
        Write-Host "已存在 Web UI（加 -Force 可重下）：$static"
    } else {
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        $webZip = Join-Path $tmp "qdrant-web-ui.zip"
        Get-RemoteFile `
            -Url "https://github.com/qdrant/qdrant-web-ui/releases/download/$WebUiVersion/dist-qdrant.zip" `
            -OutFile $webZip
        $webExtract = Join-Path $tmp "web-ui"
        Expand-Zip -Zip $webZip -Destination $webExtract
        $index = Get-ChildItem -LiteralPath $webExtract -Recurse -Filter "index.html" | Select-Object -First 1
        if ($null -eq $index) {
            throw "Web UI 压缩包里没找到 index.html"
        }
        if (Test-Path -LiteralPath $static) {
            Remove-Item -LiteralPath $static -Recurse -Force
        }
        Copy-Item -LiteralPath $index.Directory.FullName -Destination $static -Recurse -Force
        Write-Host "已写入：$static"
    }
}

Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "完成。启动 helper 后会在设置 → 通用 → 本机服务里检测到 qdrant.exe。"
