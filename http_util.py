# -*- coding: utf-8 -*-
"""HTTP 请求工具：使用 aiohttp 替代原插件依赖的 httpx

Amiya-Bot 运行环境自带 aiohttp 而无 httpx，因此移植时将原插件的
``httpx.AsyncClient`` / ``httpx.get`` 全部替换为这里的异步工具函数。
"""

from amiyalog import logger as log

import aiohttp
import asyncio
import json


class HttpResponse:
    """轻量响应对象：在 session 关闭前读取完整 body，避免 aiohttp 连接释放后无法再读。

    兼容调用方使用到的 aiohttp.ClientResponse 接口：``status`` /
    ``await resp.json()`` / ``await resp.read()`` / ``await resp.text()``。
    """

    __slots__ = ('status', '_body')

    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def text(self, encoding: str = 'utf-8', errors: str = 'replace') -> str:
        return self._body.decode(encoding, errors=errors)

    async def json(self, encoding: str = 'utf-8', loads=json.loads):
        return loads(self._body.decode(encoding))


async def http_get(url: str, params=None, headers=None, proxy=None, timeout: float = 15,
                   follow_redirects: bool = True):
    """GET 请求并读取完整响应体，失败返回 None。

    返回的 :class:`HttpResponse` 已缓存完整 body，调用方可在连接释放后
    安全读取 ``resp.status`` / ``await resp.json()`` / ``await resp.read()`` /
    ``await resp.text()``。

    - aiohttp 原生不支持 socks 代理，遇到 socks 开头代理时忽略并回退直连。
    - 失败时记录真实异常类型与原因（超时/连接失败/DNS 等），便于定位问题，
      但保持返回 None 的约定，由调用方统一按“无响应”处理。
    """
    try:
        proxy_param = proxy if (proxy and not proxy.startswith('socks')) else None
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.get(
                url,
                params=params,
                headers=headers,
                proxy=proxy_param,
                allow_redirects=follow_redirects,
            ) as resp:
                body = await resp.read()
                return HttpResponse(resp.status, body)
    except asyncio.TimeoutError:
        log.warning(f"[Steam状态监控] HTTP 请求超时: {url} (timeout={timeout}s)")
        return None
    except Exception as e:
        log.warning(f"[Steam状态监控] HTTP 请求异常: {type(e).__name__}: {e} url={url}")
        return None
