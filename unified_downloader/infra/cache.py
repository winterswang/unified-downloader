"""缓存管理"""

import sqlite3
import hashlib
import shutil
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from dataclasses import dataclass

from unified_downloader.exceptions import CacheError


@dataclass
class CacheEntry:
    """缓存条目"""

    key: str
    file_path: str
    size: int
    created_at: str
    expires_at: str
    md5: Optional[str] = None


class CacheManager:
    """
    本地缓存管理器

    使用SQLite存储缓存元数据，支持TTL过期和大小限制
    """

    def __init__(
        self,
        cache_dir: Path | str,
        ttl_days: int = 30,
        max_size_gb: float = 10.0,
    ):
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._cache_dir / ".cache.db"
        self._ttl_days = ttl_days
        self._max_size_bytes = int(max_size_gb * 1024 * 1024 * 1024)

        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库"""
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache_entries (
                    key TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    md5 TEXT,
                    access_count INTEGER DEFAULT 0,
                    last_access TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_expires_at ON cache_entries(expires_at)
            """)

    def _make_key(
        self, market: str, code: str, year: Optional[int], doc_type: str
    ) -> str:
        """生成缓存键"""
        raw = f"{market}:{code}:{year}:{doc_type}"
        return hashlib.md5(raw.encode()).hexdigest()

    def get(
        self,
        market: str,
        code: str,
        year: Optional[int],
        doc_type: str,
    ) -> Optional[str]:
        """
        获取缓存路径

        Args:
            market: 市场代码 (a/m/h)
            code: 股票代码
            year: 年份
            doc_type: 文档类型

        Returns:
            缓存文件路径，如果不存在或已过期返回None
        """
        key = self._make_key(market, code, year, doc_type)

        with sqlite3.connect(str(self._db_path)) as conn:
            row = conn.execute(
                """
                SELECT file_path, expires_at FROM cache_entries
                WHERE key = ?
            """,
                (key,),
            ).fetchone()

            if not row:
                return None

            file_path = row[0]
            expires_at = datetime.fromisoformat(row[1])

            # 检查是否过期
            if datetime.now() > expires_at:
                self._delete_entry(key, file_path)
                return None

            # 更新访问记录
            conn.execute(
                """
                UPDATE cache_entries
                SET access_count = access_count + 1, last_access = ?
                WHERE key = ?
            """,
                (datetime.now().isoformat(), key),
            )

            # 返回文件路径
            if Path(file_path).exists():
                return file_path

            # 文件不存在，删除记录
            self._delete_entry(key, file_path)
            return None

    def put(
        self,
        market: str,
        code: str,
        year: Optional[int],
        doc_type: str,
        file_path: Path | str,
        ttl_days: Optional[int] = None,
        md5: Optional[str] = None,
    ) -> str:
        """
        添加缓存

        Args:
            market: 市场代码
            code: 股票代码
            year: 年份
            doc_type: 文档类型
            file_path: 文件路径
            ttl_days: 过期天数，None使用默认值
            md5: MD5校验值

        Returns:
            缓存键
        """
        key = self._make_key(market, code, year, doc_type)
        file_path = Path(file_path)

        if not file_path.exists():
            raise CacheError(f"文件不存在: {file_path}")

        size = file_path.stat().st_size
        ttl = ttl_days if ttl_days is not None else self._ttl_days
        now = datetime.now()
        expires_at = now + timedelta(days=ttl)

        # 移动文件到缓存目录
        cached_path = self._cache_dir / market / code[:3] / f"{key}{file_path.suffix}"
        cached_path.parent.mkdir(parents=True, exist_ok=True)

        if str(file_path) != str(cached_path):
            shutil.copy2(file_path, cached_path)

        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO cache_entries
                (key, file_path, size, created_at, expires_at, md5, last_access)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    key,
                    str(cached_path),
                    size,
                    now.isoformat(),
                    expires_at.isoformat(),
                    md5,
                    now.isoformat(),
                ),
            )

        # 检查是否需要清理
        self._maybe_cleanup()

        return key

    def _delete_entry(self, key: str, file_path: str) -> None:
        """删除缓存条目"""
        try:
            Path(file_path).unlink(missing_ok=True)
        except Exception:
            pass

        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))

    def _maybe_cleanup(self) -> None:
        """检查并清理缓存"""
        total_size = self.get_size()

        if total_size > self._max_size_bytes:
            self.clear(older_than_days=7)

    def get_size(self) -> int:
        """
        获取缓存总大小

        注意：此方法遍历目录，O(n)复杂度
        """
        total_size = 0
        for cache_file in self._cache_dir.rglob("*"):
            if cache_file.is_file() and cache_file.name != ".cache.db":
                try:
                    total_size += cache_file.stat().st_size
                except Exception:
                    pass
        return total_size

    def clear(self, older_than_days: Optional[int] = None) -> int:
        """
        清理缓存

        Args:
            older_than_days: 只清理指定天数前的缓存，None表示全部清理

        Returns:
            清理的文件数量
        """
        count = 0

        with sqlite3.connect(str(self._db_path)) as conn:
            if older_than_days is None:
                # 清理所有缓存
                rows = conn.execute(
                    "SELECT key, file_path FROM cache_entries"
                ).fetchall()
            else:
                cutoff = datetime.now() - timedelta(days=older_than_days)
                rows = conn.execute(
                    """
                    SELECT key, file_path FROM cache_entries WHERE created_at < ?
                """,
                    (cutoff.isoformat(),),
                ).fetchall()

            for key, file_path in rows:
                try:
                    Path(file_path).unlink(missing_ok=True)
                except Exception:
                    pass
                count += 1

            if older_than_days is None:
                conn.execute("DELETE FROM cache_entries")
            else:
                cutoff = datetime.now() - timedelta(days=older_than_days)
                conn.execute(
                    "DELETE FROM cache_entries WHERE created_at < ?",
                    (cutoff.isoformat(),),
                )

        return count

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        with sqlite3.connect(str(self._db_path)) as conn:
            total_entries = conn.execute(
                "SELECT COUNT(*) FROM cache_entries"
            ).fetchone()[0]
            total_size = self.get_size()
            expired_entries = conn.execute(
                """
                SELECT COUNT(*) FROM cache_entries WHERE expires_at < ?
            """,
                (datetime.now().isoformat(),),
            ).fetchone()[0]

        return {
            "total_entries": total_entries,
            "total_size_bytes": total_size,
            "total_size_gb": round(total_size / (1024**3), 2),
            "expired_entries": expired_entries,
            "max_size_gb": self._max_size_bytes / (1024**3),
            "cache_dir": str(self._cache_dir),
        }

    def close(self) -> None:
        """关闭数据库连接"""
        pass  # SQLite不需要显式关闭
