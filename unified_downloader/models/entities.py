"""数据实体"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime

from unified_downloader.models.enums import Market, DocumentType, TaskStatus


@dataclass
class DownloadResult:
    """下载结果"""

    success: bool
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    source: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None
    cached: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def error(self) -> Optional[str]:
        """返回错误信息（兼容旧接口）"""
        return self.error_message


@dataclass
class BatchResult:
    """批量下载结果"""

    total: int
    succeeded: int
    failed: int
    results: List[DownloadResult] = field(default_factory=list)
    duration_ms: Optional[int] = None

    @property
    def success_rate(self) -> float:
        """计算成功率"""
        if self.total == 0:
            return 0.0
        return self.succeeded / self.total


@dataclass
class TaskInfo:
    """任务信息"""

    task_id: str
    market: Market
    code: str
    year: Optional[int]
    document_type: DocumentType
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    result: Optional[DownloadResult] = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    error: Optional[str] = None


@dataclass
class DataSource:
    """数据源配置"""

    name: str
    base_url: str
    priority: int = 1
    timeout: int = 30
    retry_times: int = 3
    enabled: bool = True


@dataclass
class CircuitBreakerState:
    """熔断器状态"""

    name: str
    state: str  # CLOSED, OPEN, HALF_OPEN
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[datetime] = None
    last_state_change: Optional[datetime] = None


@dataclass
class AuditLog:
    """审计日志"""

    id: Optional[int] = None
    timestamp: datetime = field(default_factory=datetime.now)
    event_type: str = ""
    market: Optional[str] = None
    code: Optional[str] = None
    year: Optional[int] = None
    document_type: Optional[str] = None
    success: bool = True
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None
    file_size: Optional[int] = None
    source: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckpointData:
    """断点数据"""

    task_id: str
    url: str
    file_path: str
    downloaded_bytes: int
    total_bytes: Optional[int] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
