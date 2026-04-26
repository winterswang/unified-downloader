#!/usr/bin/env python3
"""
Unified Downloader MCP Server

Exposes the unified-downloader functionality as MCP tools for OpenClaw integration.

Usage:
    pip install mcp
    openclaw mcp set unified-downloader '{"command": "python3", "args": ["/path/to/unified_downloader_mcp_server.py"]}'

Or add to ~/.openclaw/openclaw.json:
    "mcp": {
        "servers": {
            "unified-downloader": {
                "command": "python3",
                "args": ["-u", "/path/to/unified_downloader_mcp_server.py"]
            }
        }
    }
"""

import json
import asyncio
import sys
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, CallToolResult, TextContent

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from unified_downloader import UnifiedDownloader, Market


server = Server("unified-downloader")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="download_document",
            description="下载A股、港股、美股的年报、中期报告、招股说明书等文档",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "股票代码\n- A股: 6位数字如 000001, 600519\n- 港股: 4-5位数字如 00700, 09988\n- 美股: 字母Ticker如 AAPL, TSLA"},
                    "year": {"type": "integer", "description": "年份，如 2024"},
                    "market": {"type": "string", "enum": ["a", "h", "m"], "description": "市场: a=A股, h=港股, m=美股"},
                    "document_type": {"type": "string", "default": "annual_report", "description": "文档类型:\n- A股: annual_report, interim_report, quarterly\n- 港股: annual_report, interim_report, prospectus\n- 美股: 10k, 10q, s1, 8k"},
                    "output_dir": {"type": "string", "description": "输出目录路径"},
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="search_documents",
            description="搜索可用文档列表",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "股票代码"},
                    "year": {"type": "integer", "description": "年份"},
                    "market": {"type": "string", "enum": ["a", "h", "m"], "description": "市场"},
                    "document_type": {"type": "string", "default": "annual_report", "description": "文档类型"},
                    "limit": {"type": "integer", "default": 10, "description": "返回结果数量"},
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="get_available_years",
            description="获取某股票某类型文档的可用年份列表",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "股票代码"},
                    "market": {"type": "string", "enum": ["a", "h", "m"], "description": "市场"},
                    "document_type": {"type": "string", "default": "annual_report", "description": "文档类型"},
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="get_download_status",
            description="获取系统状态、缓存统计等信息",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="download_demo",
            description="下载示例文件用于验证配置",
            inputSchema={
                "type": "object",
                "properties": {
                    "market": {"type": "string", "enum": ["a", "h", "m"], "default": "a", "description": "市场"},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Any) -> CallToolResult:
    try:
        downloader = UnifiedDownloader()

        if name == "download_document":
            code = arguments.get("code")
            if not code:
                return CallToolResult(content=[TextContent(type="text", text="Error: code is required")], isError=True)

            year = arguments.get("year")
            market = arguments.get("market", "a")
            doc_type = arguments.get("document_type", "annual_report")

            result = downloader.download(
                code=code,
                year=year,
                document_type=doc_type,
                market=Market(market),
            )

            if result.success:
                return CallToolResult(content=[TextContent(type="text", text=json.dumps({
                    "success": True,
                    "file_path": result.file_path,
                    "file_size": result.file_size,
                    "source": result.source,
                    "cached": result.cached,
                }, ensure_ascii=False, indent=2))])
            else:
                return CallToolResult(content=[TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error_code": result.error_code,
                    "error_message": result.error_message,
                }, ensure_ascii=False))], isError=True)

        elif name == "search_documents":
            code = arguments.get("code")
            if not code:
                return CallToolResult(content=[TextContent(type="text", text="Error: code is required")], isError=True)

            market = arguments.get("market", "a")
            year = arguments.get("year")
            doc_type = arguments.get("document_type", "annual_report")
            limit = arguments.get("limit", 10)

            from unified_downloader.adapters.a_stock import AStockAdapter
            from unified_downloader.adapters.h_stock import HStockAdapter
            from unified_downloader.adapters.m_stock import MStockAdapter

            market_enum = Market(market)
            if market_enum == Market.A:
                adapter = AStockAdapter(downloader._http_client, [])
            elif market_enum == Market.H:
                adapter = HStockAdapter(downloader._http_client, [])
            else:
                adapter = MStockAdapter(downloader._http_client, [])

            results = adapter.search(code, year, doc_type)

            return CallToolResult(content=[TextContent(type="text", text=json.dumps({
                "code": code,
                "market": market,
                "year": year,
                "document_type": doc_type,
                "count": len(results),
                "results": results[:limit],
            }, ensure_ascii=False, indent=2))])

        elif name == "get_available_years":
            code = arguments.get("code")
            if not code:
                return CallToolResult(content=[TextContent(type="text", text="Error: code is required")], isError=True)

            market = arguments.get("market", "a")
            doc_type = arguments.get("document_type", "annual_report")

            from unified_downloader.adapters.a_stock import AStockAdapter
            from unified_downloader.adapters.h_stock import HStockAdapter
            from unified_downloader.adapters.m_stock import MStockAdapter

            market_enum = Market(market)
            if market_enum == Market.A:
                adapter = AStockAdapter(downloader._http_client, [])
            elif market_enum == Market.H:
                adapter = HStockAdapter(downloader._http_client, [])
            else:
                adapter = MStockAdapter(downloader._http_client, [])

            years = adapter.get_available_years(code, doc_type)

            return CallToolResult(content=[TextContent(type="text", text=json.dumps({
                "code": code,
                "market": market,
                "document_type": doc_type,
                "years": years,
            }, ensure_ascii=False, indent=2))])

        elif name == "get_download_status":
            cache_dir = Path("data/cache")
            cache_size = sum(f.stat().st_size for f in cache_dir.rglob("*") if f.is_file()) if cache_dir.exists() else 0

            downloads_dir = Path("downloads")
            download_count = len(list(downloads_dir.rglob("*"))) if downloads_dir.exists() else 0
            download_size = sum(f.stat().st_size for f in downloads_dir.rglob("*") if f.is_file()) if downloads_dir.exists() else 0

            return CallToolResult(content=[TextContent(type="text", text=json.dumps({
                "cache": {
                    "path": str(cache_dir),
                    "size_bytes": cache_size,
                },
                "downloads": {
                    "path": str(downloads_dir),
                    "file_count": download_count,
                    "size_bytes": download_size,
                },
            }, ensure_ascii=False, indent=2))])

        elif name == "download_demo":
            market = arguments.get("market", "a")
            demo_configs = {
                "a": {"code": "000001", "year": 2024, "type": "annual_report"},
                "h": {"code": "00700", "year": 2025, "type": "annual_report"},
                "m": {"code": "AAPL", "year": 2024, "type": "10k"},
            }
            config = demo_configs.get(market, demo_configs["a"])

            result = downloader.download(
                code=config["code"],
                year=config["year"],
                document_type=config["type"],
                market=market,
            )

            if result.success:
                return CallToolResult(content=[TextContent(type="text", text=json.dumps({
                    "success": True,
                    "market": market,
                    "file_path": result.file_path,
                    "file_size": result.file_size,
                }, ensure_ascii=False, indent=2))])
            else:
                return CallToolResult(content=[TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error_code": result.error_code,
                    "error_message": result.error_message,
                }, ensure_ascii=False))], isError=True)

        else:
            return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")], isError=True)

    except Exception as e:
        return CallToolResult(content=[TextContent(type="text", text=f"Error: {str(e)}")], isError=True)


if __name__ == "__main__":
    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(run())
