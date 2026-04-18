"""回调函数类型定义"""

from typing import Callable, Any
from dataclasses import dataclass


@dataclass
class ProgressCallback:
    """
    进度回调函数

    Args:
        downloaded: 已下载字节数
        total: 总字节数
        status: 当前状态
        speed: 下载速度 (bytes/s)
    """

    downloaded: int
    total: int
    status: str = "downloading"
    speed: float = 0.0


# 进度回调类型
ProgressCallbackType = Callable[[ProgressCallback], None]

# 通用回调类型
GenericCallbackType = Callable[[Any], None]
