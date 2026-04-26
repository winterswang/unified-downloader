"""审计日志"""

import sqlite3
import csv
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

from unified_downloader.models.enums import EventType


class AuditLogger:
    """
    审计日志管理器

    记录所有下载操作，支持查询和导出
    """

    def __init__(self, audit_dir: Path | str):
        self._audit_dir = Path(audit_dir)
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._audit_dir / ".audit.db"

        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库"""
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    market TEXT,
                    code TEXT,
                    year INTEGER,
                    document_type TEXT,
                    success INTEGER NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    duration_ms INTEGER,
                    file_size INTEGER,
                    source TEXT,
                    details TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp ON audit_logs(timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_event_type ON audit_logs(event_type)
            """)

    def log(
        self,
        event_type: EventType,
        success: bool = True,
        market: Optional[str] = None,
        code: Optional[str] = None,
        year: Optional[int] = None,
        document_type: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        duration_ms: Optional[int] = None,
        file_size: Optional[int] = None,
        source: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        记录审计日志

        Args:
            event_type: 事件类型
            success: 是否成功
            market: 市场代码
            code: 股票代码
            year: 年份
            document_type: 文档类型
            error_code: 错误码
            error_message: 错误消息
            duration_ms: 耗时（毫秒）
            file_size: 文件大小
            source: 数据源
            details: 额外详情

        Returns:
            记录ID
        """
        timestamp = datetime.now().isoformat()
        details_json = str(details) if details else None

        with sqlite3.connect(str(self._db_path)) as conn:
            cursor = conn.execute(
                """
                INSERT INTO audit_logs
                (timestamp, event_type, market, code, year, document_type,
                 success, error_code, error_message, duration_ms, file_size, source, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    timestamp,
                    event_type.value,
                    market,
                    code,
                    year,
                    document_type,
                    1 if success else 0,
                    error_code,
                    error_message,
                    duration_ms,
                    file_size,
                    source,
                    details_json,
                ),
            )
            return cursor.lastrowid

    def query(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        event_type: Optional[str] = None,
        market: Optional[str] = None,
        code: Optional[str] = None,
        success: Optional[bool] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        查询审计日志

        Args:
            start_date: 开始日期 (ISO格式)
            end_date: 结束日期 (ISO格式)
            event_type: 事件类型
            market: 市场代码
            code: 股票代码
            success: 是否成功
            limit: 返回记录数限制

        Returns:
            审计日志列表
        """
        conditions = []
        params = []

        if start_date:
            conditions.append("timestamp >= ?")
            params.append(start_date)

        if end_date:
            conditions.append("timestamp <= ?")
            params.append(end_date)

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)

        if market:
            conditions.append("market = ?")
            params.append(market)

        if code:
            conditions.append("code = ?")
            params.append(code)

        if success is not None:
            conditions.append("success = ?")
            params.append(1 if success else 0)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        sql = "SELECT * FROM audit_logs"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY timestamp DESC LIMIT ?"

        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, (*params, limit)).fetchall()

            return [dict(row) for row in rows]

    def export_csv(
        self,
        file_path: Path | str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> str:
        """
        导出为CSV

        Args:
            file_path: 输出文件路径
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            导出的文件路径
        """
        logs = self.query(start_date=start_date, end_date=end_date, limit=100000)
        file_path = Path(file_path)

        with open(file_path, "w", newline="", encoding="utf-8") as f:
            if logs:
                writer = csv.DictWriter(f, fieldnames=logs[0].keys())
                writer.writeheader()
                writer.writerows(logs)

        return str(file_path)

    def get_stats(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取统计信息

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            统计信息字典
        """
        conditions = []
        params = []

        if start_date:
            conditions.append("timestamp >= ?")
            params.append(start_date)

        if end_date:
            conditions.append("timestamp <= ?")
            params.append(end_date)

        where_clause = ""
        if conditions:
            where_clause = " WHERE " + " AND ".join(conditions)

        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row

            # 总记录数
            total = conn.execute(
                "SELECT COUNT(*) as count FROM audit_logs" + where_clause,
                params,
            ).fetchone()["count"]

            # 成功/失败数
            success_count = conn.execute(
                "SELECT COUNT(*) as count FROM audit_logs"
                + where_clause
                + (" AND " if conditions else " WHERE ") + "success = 1",
                params,
            ).fetchone()["count"]

            failed_count = total - success_count

            # 按事件类型统计
            event_stats = conn.execute(
                "SELECT event_type, COUNT(*) as count, "
                "SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count "
                "FROM audit_logs" + where_clause + " GROUP BY event_type",
                params,
            ).fetchall()

            # 按市场统计
            market_where = where_clause + (" AND " if conditions else " WHERE ") + "market IS NOT NULL"
            market_stats = conn.execute(
                "SELECT market, COUNT(*) as count, "
                "SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count "
                "FROM audit_logs" + market_where + " GROUP BY market",
                params,
            ).fetchall()

            # 平均耗时
            duration_where = where_clause + (" AND " if conditions else " WHERE ") + "duration_ms IS NOT NULL"
            avg_duration = conn.execute(
                "SELECT AVG(duration_ms) as avg_duration "
                "FROM audit_logs" + duration_where,
                params,
            ).fetchone()["avg_duration"]

            return {
                "total": total,
                "success": success_count,
                "failed": failed_count,
                "success_rate": round(success_count / total, 4) if total > 0 else 0,
                "avg_duration_ms": round(avg_duration, 2) if avg_duration else 0,
                "by_event_type": [dict(row) for row in event_stats],
                "by_market": [dict(row) for row in market_stats],
            }

    def clear(self, older_than_days: int = 90) -> int:
        """
        清理旧日志

        Args:
            older_than_days: 保留天数

        Returns:
            删除的记录数
        """
        cutoff = datetime.now() - timedelta(days=older_than_days)

        with sqlite3.connect(str(self._db_path)) as conn:
            cursor = conn.execute(
                """
                DELETE FROM audit_logs WHERE timestamp < ?
            """,
                (cutoff.isoformat(),),
            )

            return cursor.rowcount

    def close(self) -> None:
        """关闭数据库连接"""
        pass
