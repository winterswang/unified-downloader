# Unified Downloader

统一年报及IPO文件下载工具，支持A股、美股、港股的年报、中期报告、招股说明书等文档下载。

## 功能特性

- **A股 (China A-Share)**: 使用巨潮资讯数据源（AKShare）
- **港股 (HK Stock)**: 使用港交所披露易数据源
- **美股 (US Stock)**: 使用 SEC EDGAR (edgartools + sec-api 兜底)
- **缓存管理**: SQLite 元数据 + 本地文件缓存，支持 TTL 过期和容量限制
- **断点续传**: 下载中断后可恢复，基于文件锁保证安全
- **熔断器**: 每市场独立熔断，防止级联故障（CLOSED → OPEN → HALF_OPEN）
- **审计日志**: SQLite 记录所有下载事件，支持查询和 CSV 导出
- **速率限制**: 各数据源独立限速，防止请求被封
- **MCP Server**: 提供 MCP 工具接口，支持 AI 助手集成

## 安装

```bash
pip install -e .
```

安装后即可直接使用 `unified-downloader` 命令。

## 快速开始

### 下载A股年报

```bash
# 下载单个年报
unified-downloader download single 000001 -y 2024

# 下载中期报告
unified-downloader download single 000001 -y 2024 -t interim_report
```

### 下载港股年报

```bash
# 下载腾讯年报
unified-downloader download single 00700 -y 2025 -m h

# 下载招股说明书
unified-downloader download single 00700 -t prospectus -m h
```

### 下载美股10-K

```bash
# 下载苹果10-K
unified-downloader download single AAPL -y 2024 -m m

# 下载季度报告10-Q
unified-downloader download single AAPL -y 2024 -t 10q -m m
```

## CLI 命令

### 下载命令

| 命令 | 说明 |
|------|------|
| `download single CODE` | 下载单个文档 |
| `download batch FILE` | 批量下载（从JSON文件读取） |
| `download demo -m a\|h\|m` | 下载示例文件验证配置 |

### 搜索命令

| 命令 | 说明 |
|------|------|
| `search list CODE [-y YEAR] [-l N]` | 搜索可用文档列表 |
| `search years CODE [-t TYPE]` | 获取可用年份 |

### 文件管理

| 命令 | 说明 |
|------|------|
| `file list [-m a\|h\|m]` | 列出已下载文件 |
| `file info PATH` | 查看文件信息 |
| `file open PATH` | 打开文件 |

### 缓存和配置

| 命令 | 说明 |
|------|------|
| `cache stats` | 查看缓存统计 |
| `cache clean` | 清理缓存 |
| `config show` | 显示配置信息 |
| `config env` | 显示环境变量 |
| `status` | 系统状态 |
| `reset` | 重置熔断器 |

### 帮助命令

| 命令 | 说明 |
|------|------|
| `help quickstart` | 快速开始指南 |
| `help markets` | 市场支持说明 |
| `help examples` | 使用示例 |

## 使用示例

```bash
# 验证配置（下载示例文件）
unified-downloader download demo -m a

# 搜索文档列表
unified-downloader search list 000001 -l 20

# 查看可用年份
unified-downloader search years 00700 -t annual_report

# 列出已下载文件
unified-downloader file list

# 查看文件信息
unified-downloader file info ./downloads/a/000/000001_2024_ANNUAL_REPORT.PDF

# 系统状态
unified-downloader status

# 配置信息
unified-downloader config show
```

## 市场说明

### A股 (市场: a)

- 股票代码: 6位数字 (如 `000001`, `600519`)
- 支持类型: `annual_report`, `interim_report`, `quarterly`
- 数据源: 巨潮资讯 (cninfo)

### 港股 (市场: h)

- 股票代码: 5位数字且0开头 (如 `00700`, `09988`)
- 支持类型: `annual_report`, `interim_report`, `prospectus`, `quarterly`
- 数据源: 港交所披露易 (HKEx)

### 美股 (市场: m)

- 股票代码: 字母Ticker (如 `AAPL`, `TSLA`) 或 10位CIK
- 支持类型: `10k`, `10q`, `s1`, `s1a`, `6k`, `8k`
- 数据源: SEC EDGAR (edgartools 优先, sec-api 兜底)

## Python API

```python
from unified_downloader import UnifiedDownloader, AsyncUnifiedDownloader

# 同步下载
downloader = UnifiedDownloader()
result = downloader.download("600519", year=2023)
if result.success:
    print(f"文件保存至: {result.file_path}")
else:
    print(f"下载失败: {result.error_message}")

# 批量下载
tasks = [
    {"code": "600519", "year": 2023},
    {"code": "AAPL", "year": 2023, "market": "m"},
]
batch_result = downloader.batch_download(tasks)
print(f"成功率: {batch_result.success_rate:.1%}")

# 异步下载
import asyncio

async def main():
    async_dl = AsyncUnifiedDownloader()
    result = await async_dl.download("AAPL", year=2024, market="m")
    print(result.file_path)
    await async_dl.close()

asyncio.run(main())

# 缓存与熔断器管理
stats = downloader.get_cache_stats()
print(f"缓存: {stats['total_entries']} 条, {stats['total_size_gb']:.2f} GB")

downloader.reset_circuit_breakers()
```

## 批量下载

创建任务JSON文件：

```json
[
  {"code": "000001", "year": 2024, "type": "annual_report", "market": "a"},
  {"code": "00700", "year": 2025, "type": "annual_report", "market": "h"},
  {"code": "AAPL", "year": 2024, "type": "10k", "market": "m"}
]
```

执行批量下载：

```bash
unified-downloader download batch tasks.json -w 5
```

## MCP Server

支持通过 MCP 协议与 AI 助手（如 Claude Code）集成。

### 启动 MCP Server

```bash
python src/unified_downloader_mcp_server.py
```

### 配置 Claude Code

在 `~/.claude/settings.json` 中添加：

```json
{
  "mcpServers": {
    "unified-downloader": {
      "command": "python3",
      "args": ["-u", "/path/to/unified-downloader/src/unified_downloader_mcp_server.py"]
    }
  }
}
```

### 可用 MCP 工具

| 工具 | 说明 |
|------|------|
| `download_document` | 下载文档（A股/港股/美股） |
| `search_documents` | 搜索可用文档列表 |
| `get_available_years` | 获取可用年份列表 |
| `get_download_status` | 获取系统状态信息 |
| `download_demo` | 下载示例文件验证配置 |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SEC_API_KEY` | sec-api API密钥（美股兜底数据源） | - |
| `EDGAR_IDENTITY` | edgartools 身份标识（邮箱） | `UnifiedDownloader unified-downloader@example.com` |
| `SEC_USER_AGENT` | SEC 下载请求 User-Agent | `UnifiedDownloader/1.0` |
| `DOWNLOAD_DIR` | 下载目录 | `./downloads` |
| `HTTP_TIMEOUT` | HTTP超时时间(秒) | 30 |
| `HTTP_MAX_RETRIES` | 最大重试次数 | 3 |

## 项目结构

```
unified-downloader/
├── src/
│   └── unified_downloader_mcp_server.py  # MCP Server
├── unified_downloader/
│   ├── __init__.py
│   ├── cli.py                  # CLI 命令行工具
│   ├── adapters/               # 适配器（策略模式）
│   │   ├── base.py            # BaseStockAdapter 抽象基类
│   │   ├── a_stock.py         # A股适配器 (AKShare + 巨潮资讯)
│   │   ├── h_stock.py         # 港股适配器 (港交所披露易)
│   │   └── m_stock.py         # 美股适配器 (edgartools + sec-api)
│   ├── core/                   # 核心模块
│   │   ├── config.py          # 配置管理（YAML/环境变量）
│   │   ├── downloader.py      # UnifiedDownloader 同步下载器
│   │   └── async_downloader.py # AsyncUnifiedDownloader 异步下载器
│   ├── exceptions/             # 自定义异常
│   │   └── errors.py          # 12+ 异常类型
│   ├── infra/                  # 基础设施
│   │   ├── http_client.py     # 同步/异步 HTTP 客户端
│   │   ├── cache.py           # SQLite 缓存管理
│   │   ├── checkpoint.py      # 断点续传管理
│   │   ├── circuit_breaker.py # 熔断器
│   │   ├── rate_limiter.py    # 速率限制器
│   │   └── audit.py           # 审计日志
│   └── models/                 # 数据模型
│       ├── enums.py           # 枚举 (Market, DocumentType, EventType...)
│       ├── entities.py        # 数据类 (DownloadResult, BatchResult...)
│       └── callbacks.py       # 回调类型定义
├── data/                       # 运行时数据
│   ├── audit/                 # 审计日志 (SQLite)
│   ├── cache/                 # 文件缓存
│   └── checkpoint/            # 下载断点
├── downloads/                  # 下载文件存放目录
├── pyproject.toml             # Poetry 项目配置
└── README.md
```
