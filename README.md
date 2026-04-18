# Unified Downloader

统一年报及IPO文件下载工具，支持A股、美股、港股的年报、中期报告、招股说明书等文档下载。

## 功能特性

- **A股 (China A-Share)**: 使用巨潮资讯数据源
- **港股 (HK Stock)**: 使用港交所披露易数据源
- **美股 (US Stock)**: 使用 SEC EDGAR (edgartools + sec-api 兜底)

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
| `file list CODE` | 查看已下载文件 |
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

# 查看已下载文件
unified-downloader file list 000001

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

- 股票代码: 4-5位数字 (如 `00700`, `09988`)
- 支持类型: `annual_report`, `interim_report`, `prospectus`
- 数据源: 港交所披露易 (HKEx)

### 美股 (市场: m)

- 股票代码: 字母Ticker (如 `AAPL`, `TSLA`)
- 支持类型: `10k`, `10q`, `s1`, `8k`, `annual_report`
- 数据源: SEC EDGAR

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

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SEC_API_KEY` | sec-api API密钥 | - |
| `DOWNLOAD_DIR` | 下载目录 | `./downloads` |
| `HTTP_TIMEOUT` | HTTP超时时间(秒) | 30 |
| `HTTP_MAX_RETRIES` | 最大重试次数 | 3 |

## 项目结构

```
unified_downloader/
├── adapters/          # 适配器
│   ├── a_stock.py    # A股适配器
│   ├── h_stock.py    # 港股适配器
│   └── m_stock.py    # 美股适配器
├── cli.py             # 命令行工具
├── core/              # 核心模块
├── infra/             # 基础设施
│   └── http_client.py # HTTP客户端
└── models/            # 数据模型
```
