"""回调函数类型定义"""

from typing import Callable, Any, Dict

# 进度回调类型 - 接收下载进度字典
ProgressCallbackType = Callable[[Dict[str, Any]], None]

# 通用回调类型
GenericCallbackType = Callable[[Any], None]
