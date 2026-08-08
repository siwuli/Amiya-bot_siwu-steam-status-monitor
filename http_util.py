# -*- coding: utf-8 -*-
"""HTTP 请求工具：使用 aiohttp 替代原插件依赖的 httpx

Amiya-Bot 运行环境自带 aiohttp 而无 httpx，因此移植时将原插件的
``httpx.AsyncClient`` / ``httpx.get`` 全部替换为这里的异步工具函数。
"""

import aiohttp


async def http_get(url: str, params=None, headers=None, proxy=None, timeout: float = 15,
                   follow_redirects: bool = True):
    """GET 请求并读取完整响应体，失败返回 None。

    返回的 ``aiohttp.ClientResponse`` 已缓存 body（已调用 ``await resp.read()``），
    因此在 context 关闭后仍可安全调用 ``resp.status`` / ``resp.json()`` /
    ``await resp.text()`` / ``await resp.read()``。

    - aiohttp 原生不支持 socks 代理，遇到 socks 开头代理时忽略并回退直连。
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
                await resp.read()
                return resp
    except Exception:
        return None
