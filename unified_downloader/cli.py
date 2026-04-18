"""命令行工具 - 统一年报及IPO文件下载工具"""

import os
import sys
import json
import click
from pathlib import Path
from datetime import datetime

from unified_downloader import UnifiedDownloader
from unified_downloader.core.config import Config
from unified_downloader.models.enums import Market


# ============================================================================
# CLI Context
# ============================================================================


class CLIContext:
    """CLI上下文"""

    def __init__(self, config_path=None):
        self.config_path = config_path
        self.downloader = None

    def init_downloader(self):
        if self.downloader is None:
            if self.config_path:
                cfg = Config.from_file(self.config_path)
            else:
                cfg = Config()
            self.downloader = UnifiedDownloader(cfg)


ctx = click.make_pass_decorator(CLIContext, ensure=True)


# ============================================================================
# Utility Functions
# ============================================================================


def detect_market(code: str) -> str:
    """自动识别市场"""
    code = code.strip().upper()

    # A股: 6位数字 (沪市) 或 000/001开头 (深市)
    if code.isdigit():
        if len(code) == 6:
            if code.startswith(("6", "5", "9")):
                return "a"  # 沪市
            elif code.startswith(("0", "1", "3")):
                return "a"  # 深市
        return "a"

    # 港股: 4-5位数字
    if code.isdigit() and len(code) in (4, 5):
        return "h"

    # 美股: 字母组成的Ticker
    if code.isalpha():
        return "m"

    return "a"  # 默认A股


def format_size(size: int) -> str:
    """格式化文件大小"""
    if size is None or size <= 0:
        return "N/A"
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def echo_result(result, verbose=False):
    """格式化输出下载结果"""
    if result.success:
        click.echo(click.style("✓ 下载成功", fg="green"))
        click.echo(f"  文件: {result.file_path}")
        click.echo(f"  大小: {format_size(result.file_size)}")
        if result.source:
            click.echo(f"  来源: {result.source}")
        if result.cached:
            click.echo(click.style("  (来自缓存)", fg="cyan"))
        if verbose and result.metadata:
            click.echo("  元数据:")
            for k, v in result.metadata.items():
                click.echo(f"    {k}: {v}")
    else:
        click.echo(click.style("✗ 下载失败", fg="red"))
        click.echo(f"  错误码: {result.error_code}")
        click.echo(f"  原因: {result.error_message}")


# ============================================================================
# CLI Commands
# ============================================================================


@click.group()
@click.option("--config", "-c", type=click.Path(exists=True), help="配置文件路径")
@click.version_option(version="1.0.0")
@ctx
def cli(cli_ctx, config):
    """统一年报及IPO文件下载工具

    支持A股、美股、港股的年报、中期报告、招股说明书等文档下载
    """
    cli_ctx.config_path = config
    cli_ctx.init_downloader()


# --------------------------------------------------------------------------
# Help Commands
# --------------------------------------------------------------------------


@cli.group("help")
def help_group():
    """显示帮助信息"""
    pass


@help_group.command("quickstart")
@ctx
def help_quickstart(cli_ctx):
    """快速开始指南"""
    click.echo(click.style("快速开始指南", bold=True, fg="green"))
    click.echo()
    click.echo("1. 下载A股年报:")
    click.echo("   unified-downloader download single 000001 -y 2024")
    click.echo()
    click.echo("2. 下载港股年报:")
    click.echo("   unified-downloader download single 00700 -y 2025 -m h")
    click.echo()
    click.echo("3. 下载美股10-K:")
    click.echo("   unified-downloader download single AAPL -y 2024 -m m")
    click.echo()
    click.echo("4. 搜索可用文档:")
    click.echo("   unified-downloader search list 000001")
    click.echo()
    click.echo("5. 批量下载:")
    click.echo("   unified-downloader download batch tasks.json")


@help_group.command("markets")
@ctx
def help_markets(cli_ctx):
    """市场支持说明"""
    click.echo(click.style("市场支持说明", bold=True, fg="green"))
    click.echo()
    click.echo("A股 (市场: a)")
    click.echo("  - 股票代码: 6位数字 (如 000001, 600519)")
    click.echo("  - 支持类型: annual_report, interim_report, quarterly")
    click.echo("  - 数据源: 巨潮资讯 (cninfo)")
    click.echo()
    click.echo("港股 (市场: h)")
    click.echo("  - 股票代码: 4-5位数字 (如 00700, 09988)")
    click.echo("  - 支持类型: annual_report, interim_report, prospectus")
    click.echo("  - 数据源: 港交所披露易 (HKEx)")
    click.echo()
    click.echo("美股 (市场: m)")
    click.echo("  - 股票代码: 字母Ticker (如 AAPL, TSLA)")
    click.echo("  - 支持类型: 10k, 10q, s1, 8k, annual_report")
    click.echo("  - 数据源: SEC EDGAR (edgartools)")


@help_group.command("examples")
@ctx
def help_examples(cli_ctx):
    """使用示例"""
    click.echo(click.style("使用示例", bold=True, fg="green"))
    click.echo()
    examples = [
        ("# 下载示例 (验证配置)", "unified-downloader download demo -m a"),
        (
            "# 指定年份和类型",
            "unified-downloader download single 600519 -y 2024 -t annual_report",
        ),
        ("# 自动识别市场", "unified-downloader download single 000001 -y 2024"),
        ("# 搜索文档列表", "unified-downloader search list 000001 -l 20"),
        ("# 查看可用年份", "unified-downloader search years 00700 -t annual_report"),
        ("# 查看已下载文件", "unified-downloader file list 000001"),
        (
            "# 查看文件信息",
            "unified-downloader file info ./downloads/a/000/000001_2024_ANNUAL_REPORT.PDF",
        ),
        ("# 系统状态", "unified-downloader status"),
        ("# 配置信息", "unified-downloader config show"),
    ]
    for desc, cmd in examples:
        click.echo(f"{desc}")
        click.echo(f"  {cmd}")
        click.echo()


# --------------------------------------------------------------------------
# Download Commands
# --------------------------------------------------------------------------


@cli.group("download")
def download_group():
    """下载命令"""
    pass


@download_group.command("single")
@click.argument("code")
@click.option("--year", "-y", type=int, help="年份")
@click.option(
    "--type",
    "-t",
    type=click.Choice(
        ["annual_report", "interim_report", "prospectus", "10k", "10q", "s1", "8k"],
        case_sensitive=False,
    ),
    default="annual_report",
    help="文档类型",
)
@click.option(
    "--market",
    "-m",
    type=click.Choice(["a", "m", "h", "auto"], case_sensitive=False),
    default="auto",
    help="市场 (auto=自动识别)",
)
@click.option("--output", "-o", type=click.Path(), help="输出目录")
@click.option("--no-cache", is_flag=True, help="禁用缓存")
@click.option("--verbose", "-v", is_flag=True, help="详细输出")
@ctx
def download_single(cli_ctx, code, year, type, market, output, no_cache, verbose):
    """下载单个文档"""
    downloader = cli_ctx.downloader

    # 自动识别市场
    if market == "auto":
        market = detect_market(code)
        click.echo(f"自动识别市场: {market}")

    market_enum = Market(market) if market != "auto" else None

    click.echo(f"正在下载 {code} {year or '(最新)'} {type}...")

    result = downloader.download(
        code=code,
        year=year,
        document_type=type,
        market=market_enum,
        use_cache=not no_cache,
    )

    echo_result(result, verbose=verbose)

    if not result.success:
        sys.exit(1)


@download_group.command("batch")
@click.argument("file", type=click.File())
@click.option("--output", "-o", type=click.Path(), help="输出目录")
@click.option("--workers", "-w", type=int, default=5, help="最大并发数")
@click.option(
    "--errors",
    "-e",
    type=click.IntRange(0, 10),
    default=3,
    help="最大错误数 (超过此数将停止)",
)
@click.option("--verbose", "-v", is_flag=True, help="详细输出")
@ctx
def download_batch(cli_ctx, file, output, workers, errors, verbose):
    """批量下载（从JSON文件读取任务列表）"""
    downloader = cli_ctx.downloader

    try:
        tasks = json.load(file)
    except json.JSONDecodeError as e:
        click.echo(click.style(f"✗ JSON解析失败: {e}", fg="red"), err=True)
        sys.exit(1)

    if not isinstance(tasks, list):
        click.echo(click.style("✗ JSON文件根元素必须是数组", fg="red"), err=True)
        sys.exit(1)

    click.echo(f"开始批量下载 {len(tasks)} 个任务...")

    # 显示任务列表
    if verbose:
        for i, task in enumerate(tasks[:10]):
            click.echo(
                f"  {i + 1}. {task.get('code', 'N/A')} - {task.get('year', 'N/A')} - {task.get('type', 'annual_report')}"
            )
        if len(tasks) > 10:
            click.echo(f"  ... 还有 {len(tasks) - 10} 个任务")

    # 执行批量下载
    result = downloader.batch_download(tasks, max_workers=workers, max_errors=errors)

    click.echo()
    click.echo(click.style("批量下载完成:", bold=True))
    click.echo(f"  总数:   {result.total}")
    click.echo(click.style(f"  成功:   {result.succeeded}", fg="green"))
    click.echo(
        click.style(f"  失败:   {result.failed}", fg="red" if result.failed else None)
    )
    click.echo(f"  成功率: {result.success_rate * 100:.1f}%")

    if result.failed > 0:
        sys.exit(1)


@download_group.command("demo")
@click.option(
    "--market",
    "-m",
    type=click.Choice(["a", "h", "m"], case_sensitive=False),
    default="a",
    help="市场",
)
@ctx
def download_demo(cli_ctx, market):
    """下载示例文件（用于验证配置）"""
    demo_configs = {
        "a": {"code": "000001", "year": 2024, "type": "annual_report"},
        "h": {"code": "00700", "year": 2025, "type": "annual_report"},
        "m": {"code": "AAPL", "year": 2024, "type": "10k"},
    }

    config = demo_configs[market]
    downloader = cli_ctx.downloader

    click.echo(f"下载示例文件 ({market}股)...")
    click.echo(f"  代码: {config['code']}")
    click.echo(f"  年份: {config['year']}")
    click.echo(f"  类型: {config['type']}")
    click.echo()

    result = downloader.download(
        code=config["code"],
        year=config["year"],
        document_type=config["type"],
    )

    echo_result(result, verbose=True)

    if not result.success:
        sys.exit(1)


# --------------------------------------------------------------------------
# Search Commands
# --------------------------------------------------------------------------


@cli.group("search")
def search_group():
    """搜索命令"""
    pass


@search_group.command("list")
@click.argument("code")
@click.option("--year", "-y", type=int, help="年份")
@click.option(
    "--type",
    "-t",
    type=click.Choice(
        ["annual_report", "interim_report", "prospectus", "10k", "10q", "s1", "8k"],
        case_sensitive=False,
    ),
    default="annual_report",
    help="文档类型",
)
@click.option(
    "--market",
    "-m",
    type=click.Choice(["a", "m", "h", "auto"], case_sensitive=False),
    default="auto",
    help="市场",
)
@click.option("--limit", "-l", type=int, default=10, help="返回结果数量")
@ctx
def search_list(cli_ctx, code, year, type, market, limit):
    """搜索可用文档列表"""
    downloader = cli_ctx.downloader

    if market == "auto":
        market = detect_market(code)

    market_enum = Market(market)

    click.echo(f"搜索 {code} {year or '所有年份'} {type}...")

    try:
        if market_enum == Market.A:
            from unified_downloader.adapters.a_stock import AStockAdapter

            adapter = AStockAdapter(downloader._http_client, [])
        elif market_enum == Market.H:
            from unified_downloader.adapters.h_stock import HStockAdapter

            adapter = HStockAdapter(downloader._http_client, [])
        elif market_enum == Market.M:
            from unified_downloader.adapters.m_stock import MStockAdapter

            adapter = MStockAdapter(downloader._http_client, [])
        else:
            click.echo(click.style(f"✗ 不支持的市场: {market}", fg="red"))
            sys.exit(1)

        results = adapter.search(code, year, type)

        if not results:
            click.echo("未找到文档")
            return

        click.echo(click.style(f"\n找到 {len(results)} 个文档:", bold=True))
        click.echo()

        for i, doc in enumerate(results[:limit]):
            if market_enum == Market.A:
                title = doc.get("title", "N/A")
                time = doc.get("time", "N/A")
                click.echo(f"{i + 1}. {title}")
                click.echo(f"   时间: {time}")
            elif market_enum == Market.H:
                title = doc.get("title", "N/A")
                date_time = doc.get("date_time", "N/A")
                click.echo(f"{i + 1}. {title}")
                click.echo(f"   时间: {date_time}")
            elif market_enum == Market.M:
                filed_at = doc.get("filedAt", "N/A")
                form_type = doc.get("formType", "N/A")
                accession = doc.get("accessionNo", "N/A")
                click.echo(f"{i + 1}. {form_type} - {filed_at}")
                click.echo(f"   Accession: {accession}")

        if len(results) > limit:
            click.echo()
            click.echo(f"... 还有 {len(results) - limit} 个文档")

    except Exception as e:
        click.echo(click.style(f"✗ 搜索失败: {e}", fg="red"), err=True)
        sys.exit(1)


@search_group.command("years")
@click.argument("code")
@click.option(
    "--type",
    "-t",
    type=click.Choice(
        ["annual_report", "interim_report", "prospectus", "10k", "10q"],
        case_sensitive=False,
    ),
    default="annual_report",
    help="文档类型",
)
@click.option(
    "--market",
    "-m",
    type=click.Choice(["a", "m", "h", "auto"], case_sensitive=False),
    default="auto",
    help="市场",
)
@ctx
def search_years(cli_ctx, code, type, market):
    """获取可用年份列表"""
    downloader = cli_ctx.downloader

    if market == "auto":
        market = detect_market(code)

    market_enum = Market(market)

    try:
        if market_enum == Market.A:
            from unified_downloader.adapters.a_stock import AStockAdapter

            adapter = AStockAdapter(downloader._http_client, [])
        elif market_enum == Market.H:
            from unified_downloader.adapters.h_stock import HStockAdapter

            adapter = HStockAdapter(downloader._http_client, [])
        elif market_enum == Market.M:
            from unified_downloader.adapters.m_stock import MStockAdapter

            adapter = MStockAdapter(downloader._http_client, [])
        else:
            click.echo(click.style(f"✗ 不支持的市场: {market}", fg="red"))
            sys.exit(1)

        years = adapter.get_available_years(code, type)

        if not years:
            click.echo("未找到可用年份")
            return

        click.echo(f"{code} 可用年份 ({type}):")
        for y in years:
            click.echo(f"  - {y}")

    except Exception as e:
        click.echo(click.style(f"✗ 获取年份失败: {e}", fg="red"), err=True)
        sys.exit(1)


# --------------------------------------------------------------------------
# File Management Commands
# --------------------------------------------------------------------------


@cli.group("file")
def file_group():
    """文件管理命令"""
    pass


@file_group.command("list")
@click.option("--market", "-m", type=click.Choice(["a", "m", "h"]), help="按市场筛选")
@click.option("--type", "-t", help="按文档类型筛选")
@click.option("--limit", "-l", type=int, default=20, help="显示数量")
@click.option("--format", "-f", type=click.Choice(["table", "json"]), default="table")
@ctx
def file_list(cli_ctx, market, type, limit, format):
    """列出已下载的文件"""
    download_dir = Path("downloads")

    if not download_dir.exists():
        click.echo("暂无下载文件")
        return

    files = []
    for m in ["a", "h", "m"]:
        m_dir = download_dir / m
        if not m_dir.exists():
            continue
        if market and m != market:
            continue

        for f in m_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in (".pdf", ".html", ".txt"):
                stat = f.stat()
                files.append(
                    {
                        "market": m.upper(),
                        "path": str(f.relative_to(download_dir)),
                        "name": f.name,
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime),
                    }
                )

    if not files:
        click.echo("暂无下载文件")
        return

    # 按修改时间排序
    files.sort(key=lambda x: x["modified"], reverse=True)

    if format == "json":
        click.echo(json.dumps(files[:limit], indent=2, default=str))
    else:
        click.echo(click.style(f"已下载文件 ({len(files)} 个):", bold=True))
        click.echo()
        for i, f in enumerate(files[:limit]):
            size_str = format_size(f["size"])
            mtime = f["modified"].strftime("%Y-%m-%d %H:%M")
            click.echo(f"{i + 1}. [{f['market']}] {f['name']}")
            click.echo(f"   大小: {size_str:>10}  修改: {mtime}")
            click.echo(f"   路径: {f['path']}")

        if len(files) > limit:
            click.echo()
            click.echo(f"... 还有 {len(files) - limit} 个文件")


@file_group.command("info")
@click.argument("path", type=click.Path(exists=True))
@ctx
def file_info(cli_ctx, path):
    """显示文件详细信息"""
    p = Path(path)
    stat = p.stat()

    click.echo(click.style("文件信息:", bold=True))
    click.echo(f"  路径: {p.absolute()}")
    click.echo(f"  大小: {format_size(stat.st_size)}")
    click.echo(f"  创建: {datetime.fromtimestamp(stat.st_ctime)}")
    click.echo(f"  修改: {datetime.fromtimestamp(stat.st_mtime)}")
    click.echo(f"  类型: {p.suffix}")

    # 尝试读取PDF元数据
    if p.suffix.lower() == ".pdf":
        try:
            with open(p, "rb") as f:
                header = f.read(20)
                if header.startswith(b"%PDF"):
                    click.echo(click.style("  格式: PDF 文件", fg="green"))
        except Exception:
            pass


@file_group.command("open")
@click.argument("path", type=click.Path(exists=True))
@ctx
def file_open(cli_ctx, path):
    """打开文件（使用系统默认程序）"""
    import subprocess

    platform = sys.platform

    try:
        if platform == "darwin":
            subprocess.run(["open", str(path)])
        elif platform == "linux":
            subprocess.run(["xdg-open", str(path)])
        elif platform == "win32":
            subprocess.run(["start", "", str(path)], shell=True)
        else:
            click.echo(f"不支持的平台: {platform}")
    except Exception as e:
        click.echo(click.style(f"打开失败: {e}", fg="red"))


# --------------------------------------------------------------------------
# Cache & Status Commands
# --------------------------------------------------------------------------


@cli.group("cache")
def cache_group():
    """缓存管理命令"""
    pass


@cache_group.command("stats")
@ctx
def cache_stats(cli_ctx):
    """显示缓存统计"""
    downloader = cli_ctx.downloader
    stats = downloader.get_cache_stats()

    click.echo(click.style("缓存统计:", bold=True))
    click.echo(f"  总条目: {stats['total_entries']}")
    click.echo(f"  总大小: {stats['total_size_gb']:.2f} GB")
    click.echo(f"  过期条目: {stats['expired_entries']}")


@cache_group.command("clean")
@click.option("--older-than", type=int, default=30, help="清理指定天数前的缓存")
@click.confirmation_option(prompt="确认清理缓存?")
@ctx
def cache_clean(cli_ctx, older_than):
    """清理缓存"""
    downloader = cli_ctx.downloader
    count = downloader.clear_cache(older_than_days=older_than)
    click.echo(click.style(f"✓ 已清理 {count} 个缓存文件", fg="green"))


@cli.command("status")
@click.option("--verbose", "-v", is_flag=True, help="详细输出")
@ctx
def status(cli_ctx, verbose):
    """显示系统状态"""
    downloader = cli_ctx.downloader

    click.echo(click.style("系统状态:", bold=True))

    # 缓存状态
    cache_stats = downloader.get_cache_stats()
    click.echo()
    click.echo(click.style("缓存:", bold=True))
    click.echo(f"  条目: {cache_stats['total_entries']}")
    click.echo(f"  大小: {cache_stats['total_size_gb']:.2f} GB")

    # 熔断器状态
    circuit_status = downloader.get_circuit_status()
    click.echo()
    click.echo(click.style("熔断器:", bold=True))
    for name, status in circuit_status.items():
        state_color = {"closed": "green", "open": "red", "half_open": "yellow"}[
            status["state"]
        ]
        click.echo(
            f"  {name.upper()}: {click.style(status['state'], fg=state_color)} "
            f"(失败: {status['failure_count']})"
        )

    # 下载目录
    download_dir = Path("downloads")
    if download_dir.exists():
        total_files = sum(1 for _ in download_dir.rglob("*") if _.is_file())
        total_size = sum(
            f.stat().st_size for f in download_dir.rglob("*") if f.is_file()
        )
        click.echo()
        click.echo(click.style("下载文件:", bold=True))
        click.echo(f"  目录: {download_dir.absolute()}")
        click.echo(f"  文件: {total_files} 个")
        click.echo(f"  大小: {format_size(total_size)}")


@cli.command("reset")
@click.option(
    "--market",
    "-m",
    type=click.Choice(["a", "m", "h", "all"]),
    default="all",
    help="重置指定市场或全部",
)
@ctx
def reset(cli_ctx, market):
    """重置熔断器"""
    downloader = cli_ctx.downloader

    if market == "all":
        downloader.reset_circuit_breakers()
        click.echo(click.style("✓ 已重置所有熔断器", fg="green"))
    else:
        breaker = downloader._circuit_breaker_manager.get_breaker(market)
        breaker.reset()
        click.echo(click.style(f"✓ 已重置 {market.upper()} 市场的熔断器", fg="green"))


# --------------------------------------------------------------------------
# Config Commands
# --------------------------------------------------------------------------


@cli.group("config")
def config_group():
    """配置管理命令"""
    pass


@config_group.command("show")
@click.option(
    "--format",
    "-f",
    type=click.Choice(["table", "json"]),
    default="table",
    help="输出格式",
)
@ctx
def config_show(cli_ctx, format):
    """显示当前配置"""
    downloader = cli_ctx.downloader
    cfg = downloader.config

    config_data = {
        "download_dir": str(cfg.download.download_dir),
        "cache_dir": str(cfg.cache_dir),
        "checkpoint_dir": str(cfg.checkpoint_dir),
        "max_workers": cfg.download.max_workers,
        "chunk_size": cfg.download.chunk_size,
        "cache_enabled": cfg.download.cache_enabled,
        "cache_ttl_days": cfg.download.cache_ttl_days,
        "rate_limit": {
            "failure_threshold": cfg.circuit_breaker.failure_threshold,
            "timeout_seconds": cfg.circuit_breaker.timeout_seconds,
        },
    }

    if format == "json":
        click.echo(json.dumps(config_data, indent=2))
    else:
        click.echo(click.style("当前配置:", bold=True))
        click.echo(f"  下载目录:   {config_data['download_dir']}")
        click.echo(f"  缓存目录:   {config_data['cache_dir']}")
        click.echo(f"  断点目录:   {config_data['checkpoint_dir']}")
        click.echo(f"  最大并发:   {config_data['max_workers']}")
        click.echo(f"  缓存启用:   {config_data['cache_enabled']}")
        click.echo(f"  缓存TTL:    {config_data['cache_ttl_days']} 天")


@config_group.command("env")
@ctx
def config_env(cli_ctx):
    """显示相关环境变量"""
    env_vars = [
        ("EDGAR_IDENTITY", "edgartools身份标识 (email)"),
        ("SEC_API_KEY", "sec-api API密钥"),
        ("SEC_USER_AGENT", "SEC下载User-Agent"),
        ("DOWNLOAD_DIR", "下载目录"),
        ("CACHE_DIR", "缓存目录"),
    ]

    click.echo(click.style("环境变量:", bold=True))
    for var, desc in env_vars:
        value = os.environ.get(var, "<未设置>")
        if value != "<未设置>":
            # 隐藏部分值
            if "KEY" in var and len(value) > 8:
                value = value[:4] + "..." + value[-4:]
        click.echo(f"  {var}")
        click.echo(f"    说明: {desc}")
        click.echo(f"    当前: {value}")


# ============================================================================
# Main Entry Point
# ============================================================================


def main():
    """入口函数"""
    cli(obj=CLIContext())


if __name__ == "__main__":
    main()
