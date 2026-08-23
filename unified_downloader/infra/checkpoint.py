"""断点续传管理"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime

from unified_downloader.exceptions import CheckpointError

logger = logging.getLogger(__name__)

# Platform-compatible file locking
if sys.platform == "win32":
    import msvcrt
else:
    try:
        import fcntl
    except ImportError:
        fcntl = None


class CheckpointManager:
    """
    断点续传管理器

    管理下载任务的断点信息，支持从断点恢复下载
    """

    def __init__(self, checkpoint_dir: Path | str):
        self._checkpoint_dir = Path(checkpoint_dir)
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file_path = self._checkpoint_dir / ".lock"

    def _get_checkpoint_path(self, task_id: str) -> Path:
        """获取断点文件路径"""
        return self._checkpoint_dir / f"{task_id}.json"

    @staticmethod
    def _lock_file(f, exclusive: bool = True) -> None:
        """Platform-compatible file lock"""
        if sys.platform == "win32":
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK if exclusive else msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)

    @staticmethod
    def _unlock_file(f) -> None:
        """Platform-compatible file unlock"""
        if sys.platform == "win32":
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def save(
        self,
        task_id: str,
        url: str,
        file_path: str,
        downloaded_bytes: int,
        total_bytes: Optional[int] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> None:
        """
        保存断点信息

        Args:
            task_id: 任务ID
            url: 下载URL
            file_path: 文件保存路径
            downloaded_bytes: 已下载字节数
            total_bytes: 总字节数
            etag: ETag响应头
            last_modified: Last-Modified响应头
        """
        checkpoint_path = self._get_checkpoint_path(task_id)

        checkpoint_data = {
            "task_id": task_id,
            "url": url,
            "file_path": file_path,
            "downloaded_bytes": downloaded_bytes,
            "total_bytes": total_bytes,
            "etag": etag,
            "last_modified": last_modified,
            "updated_at": datetime.now().isoformat(),
        }

        try:
            # W40-#50 P1: tmp+rename 原子写 — 之前 open("w") 在拿 flock 前
            # 就截断目标文件且 json.dump 直写, 写一半崩溃留下半截 JSON,
            # 之后 get() 每次都抛 CheckpointError 毒化该 task_id 的全部
            # 后续下载, 直到人工删文件
            tmp_path = checkpoint_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                self._lock_file(f)
                json.dump(checkpoint_data, f, ensure_ascii=False, indent=2)
                self._unlock_file(f)
            os.replace(tmp_path, checkpoint_path)
        except Exception as e:
            raise CheckpointError(f"保存断点失败: {e}")

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        获取断点信息

        Args:
            task_id: 任务ID

        Returns:
            断点信息字典，如果不存在返回None
        """
        checkpoint_path = self._get_checkpoint_path(task_id)

        if not checkpoint_path.exists():
            return None

        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                self._lock_file(f, exclusive=False)
                data = json.load(f)
                self._unlock_file(f)
                return data
        except json.JSONDecodeError as e:
            # W40-#50 P1: 损坏 (截断) 的断点文件不再抛 CheckpointError 毒化
            # 后续下载 — 移除坏文件并当作无断点, 从头下载
            logger.warning(
                f"Corrupted checkpoint {checkpoint_path} removed, restarting fresh: {e}"
            )
            try:
                checkpoint_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        except Exception as e:
            raise CheckpointError(f"读取断点失败: {e}")

    def delete(self, task_id: str) -> None:
        """
        删除断点信息

        Args:
            task_id: 任务ID
        """
        checkpoint_path = self._get_checkpoint_path(task_id)

        if checkpoint_path.exists():
            try:
                checkpoint_path.unlink()
            except Exception as e:
                raise CheckpointError(f"删除断点失败: {e}")

    def exists(self, task_id: str) -> bool:
        """检查断点是否存在"""
        return self._get_checkpoint_path(task_id).exists()

    def clear(self, older_than_hours: Optional[int] = None) -> int:
        """
        清理断点文件

        Args:
            older_than_hours: 只清理指定小时数前的断点

        Returns:
            清理的文件数量
        """
        count = 0
        now = datetime.now()

        for checkpoint_file in self._checkpoint_dir.glob("*.json"):
            if checkpoint_file.name == ".lock":
                continue

            try:
                if older_than_hours:
                    mtime = datetime.fromtimestamp(checkpoint_file.stat().st_mtime)
                    if (now - mtime).total_seconds() > older_than_hours * 3600:
                        checkpoint_file.unlink()
                        count += 1
                else:
                    checkpoint_file.unlink()
                    count += 1
            except Exception:
                pass

        return count

    def resume(self, task_id: str, url: str) -> Optional[Dict[str, Any]]:
        """
        获取断点并验证

        Args:
            task_id: 任务ID
            url: 当前请求URL（用于验证断点是否匹配）

        Returns:
            匹配的断点信息
        """
        checkpoint = self.get(task_id)

        if not checkpoint:
            return None

        # 验证URL是否匹配
        if checkpoint.get("url") != url:
            return None

        # 检查文件是否存在
        file_path = Path(checkpoint.get("file_path", ""))
        if not file_path.exists():
            return None

        return checkpoint

    def save_resume(
        self,
        task_id: str,
        url: str,
        file_path: str,
        downloaded_bytes: int,
        total_bytes: Optional[int] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        保存断点信息（带文件锁）

        这是一个便捷方法，结合了保存和过期检查
        """
        self.save(
            task_id=task_id,
            url=url,
            file_path=file_path,
            downloaded_bytes=downloaded_bytes,
            total_bytes=total_bytes,
            etag=etag,
            last_modified=last_modified,
        )

        return {
            "task_id": task_id,
            "url": url,
            "file_path": file_path,
            "downloaded_bytes": downloaded_bytes,
            "total_bytes": total_bytes,
        }
