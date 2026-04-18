"""HTTP客户端封装"""

import hashlib
import logging
import time
from pathlib import Path
from typing import Optional, Dict, Any, Callable

import requests
import aiohttp

from unified_downloader.exceptions import (
    NetworkError,
    RateLimitError,
    TimeoutError,
    FileIntegrityError,
)

logger = logging.getLogger(__name__)


class HTTPClient:
    """
    HTTP客户端，同步版本

    提供重试、熔断、超时等能力的HTTP请求封装
    """

    def __init__(
        self,
        timeout: int = 30,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        max_backoff: float = 30.0,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.max_backoff = max_backoff
        self._session: Optional[requests.Session] = None

    @property
    def session(self) -> requests.Session:
        """获取或创建会话"""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Cache-Control": "max-age=0",
                }
            )
        return self._session

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> requests.Response:
        """发送GET请求"""
        return self._request("GET", url, params=params, headers=headers, **kwargs)

    def post(
        self,
        url: str,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> requests.Response:
        """发送POST请求"""
        return self._request(
            "POST", url, data=data, json=json, headers=headers, **kwargs
        )

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """发送HTTP请求"""
        kwargs.setdefault("timeout", self.timeout)

        last_exception = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.request(method, url, **kwargs)

                if response.status_code == 429:
                    # Rate limit - use longer backoff
                    retry_after = int(response.headers.get("Retry-After", 60))
                    wait_time = min(retry_after, self.max_backoff)
                    logger.warning(
                        f"Rate limited (429), waiting {wait_time}s before retry..."
                    )
                    time.sleep(wait_time)
                    raise RateLimitError(f"请求过于频繁 (429): {url}")

                if response.status_code == 404:
                    raise FileIntegrityError(f"资源不存在 (404): {url}")

                response.raise_for_status()
                return response

            except requests.exceptions.Timeout:
                last_exception = TimeoutError(f"请求超时: {url}")
                if attempt < self.max_retries - 1:
                    time.sleep(min(self.retry_backoff * (2**attempt), self.max_backoff))
                continue

            except requests.exceptions.ConnectionError:
                last_exception = NetworkError(f"连接失败: {url}")
                if attempt < self.max_retries - 1:
                    time.sleep(min(self.retry_backoff * (2**attempt), self.max_backoff))
                continue

            except requests.exceptions.HTTPError as e:
                if e.response.status_code in (429, 500, 502, 503, 504):
                    last_exception = NetworkError(
                        f"HTTP错误 {e.response.status_code}: {url}"
                    )
                    if attempt < self.max_retries - 1:
                        # Use longer backoff for 5xx errors
                        backoff = self.retry_backoff * (2**attempt) * 2
                        time.sleep(min(backoff, self.max_backoff))
                    continue
                raise

            except (RateLimitError, FileIntegrityError, TimeoutError):
                raise

            except Exception as e:
                last_exception = NetworkError(f"请求失败: {url} - {str(e)}")
                if attempt < self.max_retries - 1:
                    time.sleep(min(self.retry_backoff * (2**attempt), self.max_backoff))
                continue

        raise last_exception

    def download_file(
        self,
        url: str,
        file_path: Path | str,
        on_progress: Optional[Callable] = None,
        checkpoint: Optional[Dict[str, Any]] = None,
        expected_md5: Optional[str] = None,
        chunk_size: int = 8192,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        下载文件到本地

        Args:
            url: 文件URL
            file_path: 保存路径
            on_progress: 进度回调
            checkpoint: 断点信息 {'downloaded_bytes': int, 'etag': str}
            expected_md5: 期望的MD5校验值
            chunk_size: 分块大小
            headers: 自定义请求头

        Returns:
            {'file_path': str, 'file_size': int, 'md5': str}
        """
        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        request_headers = dict(headers) if headers else {}
        mode = "wb"
        downloaded = 0

        # 如果有断点，从断点继续下载
        if checkpoint:
            downloaded = checkpoint.get("downloaded_bytes", 0)
            if downloaded > 0:
                request_headers["Range"] = f"bytes={downloaded}-"
                mode = "ab"

        start_time = time.time()
        response = self._request("GET", url, headers=request_headers, stream=True)
        total_size = int(response.headers.get("content-length", 0))

        md5_hash = hashlib.md5()
        with open(file_path, mode) as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    md5_hash.update(chunk)
                    downloaded += len(chunk)

                    if on_progress:
                        elapsed = time.time() - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        on_progress(
                            {
                                "downloaded": downloaded,
                                "total": total_size,
                                "status": "downloading",
                                "speed": speed,
                            }
                        )

        md5_result = md5_hash.hexdigest()

        # 校验MD5
        if expected_md5 and md5_result != expected_md5:
            file_path.unlink(missing_ok=True)
            raise FileIntegrityError(
                f"MD5校验失败，期望: {expected_md5}, 实际: {md5_result}"
            )

        return {
            "file_path": str(file_path),
            "file_size": downloaded,
            "md5": md5_result,
        }

    def close(self):
        """关闭会话"""
        if self._session:
            self._session.close()
            self._session = None


class AsyncHTTPClient:
    """
    异步HTTP客户端

    使用aiohttp实现异步HTTP请求
    """

    def __init__(
        self,
        timeout: int = 30,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        max_backoff: float = 30.0,
        connector_limit: int = 100,
    ):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.max_backoff = max_backoff
        self.connector_limit = connector_limit
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    async def session(self) -> aiohttp.ClientSession:
        """获取或创建会话"""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=self.connector_limit)
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                connector=connector,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Cache-Control": "max-age=0",
                },
            )
        return self._session

    async def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> aiohttp.ClientResponse:
        """发送GET请求"""
        return await self._request("GET", url, params=params, headers=headers, **kwargs)

    async def post(
        self,
        url: str,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> aiohttp.ClientResponse:
        """发送POST请求"""
        return await self._request(
            "POST", url, data=data, json=json, headers=headers, **kwargs
        )

    async def _request(self, method: str, url: str, **kwargs) -> aiohttp.ClientResponse:
        """发送HTTP请求"""
        session = await self.session

        last_exception = None
        for attempt in range(self.max_retries):
            try:
                async with session.request(method, url, **kwargs) as response:
                    if response.status == 429:
                        raise RateLimitError(f"请求过于频繁 (429): {url}")

                    if response.status == 404:
                        raise FileIntegrityError(f"资源不存在 (404): {url}")

                    response.raise_for_status()
                    return response

            except aiohttp.ClientTimeout:
                last_exception = TimeoutError(f"请求超时: {url}")
                if attempt < self.max_retries - 1:
                    await self._sleep(
                        min(self.retry_backoff * (2**attempt), self.max_backoff)
                    )
                continue

            except aiohttp.ClientConnectorError:
                last_exception = NetworkError(f"连接失败: {url}")
                if attempt < self.max_retries - 1:
                    await self._sleep(
                        min(self.retry_backoff * (2**attempt), self.max_backoff)
                    )
                continue

            except aiohttp.ClientResponseError as e:
                if e.status in (429, 500, 502, 503, 504):
                    last_exception = NetworkError(f"HTTP错误 {e.status}: {url}")
                    if attempt < self.max_retries - 1:
                        await self._sleep(
                            min(self.retry_backoff * (2**attempt), self.max_backoff)
                        )
                    continue
                raise

            except (RateLimitError, FileIntegrityError, TimeoutError):
                raise

            except Exception as e:
                last_exception = NetworkError(f"请求失败: {url} - {str(e)}")
                if attempt < self.max_retries - 1:
                    await self._sleep(
                        min(self.retry_backoff * (2**attempt), self.max_backoff)
                    )
                continue

        raise last_exception

    async def _sleep(self, seconds: float):
        """异步睡眠"""
        import asyncio

        await asyncio.sleep(seconds)

    async def download_file(
        self,
        url: str,
        file_path: Path | str,
        on_progress: Optional[Callable] = None,
        checkpoint: Optional[Dict[str, Any]] = None,
        expected_md5: Optional[str] = None,
        chunk_size: int = 8192,
    ) -> Dict[str, Any]:
        """
        异步下载文件

        Args:
            url: 文件URL
            file_path: 保存路径
            on_progress: 进度回调
            checkpoint: 断点信息
            expected_md5: 期望的MD5
            chunk_size: 分块大小

        Returns:
            {'file_path': str, 'file_size': int, 'md5': str}
        """
        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        headers = {}
        mode = "wb"
        downloaded = 0

        if checkpoint:
            downloaded = checkpoint.get("downloaded_bytes", 0)
            if downloaded > 0:
                headers["Range"] = f"bytes={downloaded}-"
                mode = "ab"

        session = await self.session
        start_time = time.time()

        async with session.get(url, headers=headers) as response:
            total_size = int(response.headers.get("content-length", 0))

            md5_hash = hashlib.md5()
            with open(file_path, mode) as f:
                async for chunk in response.content.iter_chunked(chunk_size):
                    if chunk:
                        f.write(chunk)
                        md5_hash.update(chunk)
                        downloaded += len(chunk)

                        if on_progress:
                            elapsed = time.time() - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            on_progress(
                                {
                                    "downloaded": downloaded,
                                    "total": total_size,
                                    "status": "downloading",
                                    "speed": speed,
                                }
                            )

            md5_result = md5_hash.hexdigest()

            if expected_md5 and md5_result != expected_md5:
                file_path.unlink(missing_ok=True)
                raise FileIntegrityError("MD5校验失败")

            return {
                "file_path": str(file_path),
                "file_size": downloaded,
                "md5": md5_result,
            }

    async def close(self):
        """关闭会话"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
