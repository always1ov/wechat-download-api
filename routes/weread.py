#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
微信读书通道管理路由

公众号后台凭证约 4 天过期、还有频率风控；配好微信读书 Cookie 后，后台失效时
轮询器和文章接口会自动切到微信读书继续采集。这里提供 Cookie 的配置/校验，
以及直连微信读书的文章列表/正文接口，便于排查「到底是哪条通道挂了」。
"""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from utils import rss_store, weread_client
from utils.weread_client import WereadClient, WereadError, weread_auth

logger = logging.getLogger(__name__)

router = APIRouter()


class WereadResponse(BaseModel):
    """通用响应"""
    success: bool = Field(..., description="是否成功")
    data: Optional[Dict] = Field(None, description="返回数据，失败时为 null")
    error: Optional[str] = Field(None, description="错误信息，成功时为 null")


class CookieRequest(BaseModel):
    """Cookie 配置请求"""
    cookie: str = Field(
        ...,
        description="微信读书 Cookie，需含 wr_skey / wr_vid / wr_rt。"
                    "取法：浏览器登录 weread.qq.com → F12 Network → 任意请求 → "
                    "复制 Request Headers 里的完整 Cookie",
    )


class ShelfRequest(BaseModel):
    """加入书架请求"""
    fakeid: str = Field(..., description="公众号 FakeID，或已换算好的 MP_WXS_* bookId")
    nickname: str = Field("", description="公众号名称（仅用于日志/提示）")


@router.get("/weread/status", response_model=WereadResponse, summary="微信读书通道状态")
async def weread_status():
    """
    查看微信读书通道的配置状态（不回传完整 Cookie）。

    **返回字段（均在 `data` 下）：**
    - `configured`: 是否已配置 Cookie
    - `enabled`: 通道是否启用（默认「配了 Cookie 就启用」，可用 WEREAD_ENABLED 覆盖）
    - `env_managed`: Cookie 是否由 WEREAD_COOKIE 环境变量托管（是则管理页写入不生效）
    - `vid`: Cookie 里的 wr_vid
    - `article_source`: 取数策略 auto / mp / weread
    - `auto_add_to_shelf`: 采集前是否自动把公众号加入书架
    """
    info = weread_auth.get_info()
    info.update({
        "article_source": weread_client.article_source(),
        "auto_add_to_shelf": weread_client.auto_add_to_shelf(),
        "content_interval": weread_client.content_interval(),
        "page_interval": weread_client.page_interval(),
        "max_pages": weread_client.max_pages(),
    })
    return WereadResponse(success=True, data=info)


@router.post("/weread/cookie", response_model=WereadResponse, summary="配置微信读书 Cookie")
async def save_weread_cookie(req: CookieRequest):
    """
    保存微信读书 Cookie 到 `data/.weread.json`，并立即校验一次有效性。

    **请求体参数：**
    - **cookie** (必填): 完整的 weread.qq.com Cookie

    保存成功即返回 success=true；校验结果在 `data.verified` / `data.verify_message` 里，
    校验失败不代表没保存（可能只是网络不通）。
    """
    ok, message = weread_auth.save_cookie(req.cookie)
    if not ok:
        return WereadResponse(success=False, error=message)

    verified, verify_message = False, ""
    try:
        async with WereadClient() as client:
            verified, verify_message = await client.verify()
    except Exception as e:  # 网络异常不该让保存动作失败
        verify_message = f"校验请求失败: {e}"
        logger.warning("[WeRead] Cookie 校验异常: %s", e)

    info = weread_auth.get_info()
    info.update({"message": message, "verified": verified,
                 "verify_message": verify_message})
    return WereadResponse(success=True, data=info)


@router.delete("/weread/cookie", response_model=WereadResponse, summary="清除微信读书 Cookie")
async def clear_weread_cookie():
    """删除已保存的微信读书 Cookie（不影响 WEREAD_COOKIE 环境变量）。"""
    ok = weread_auth.clear()
    if not ok:
        return WereadResponse(success=False, error="清除失败，请检查 data 目录权限")
    return WereadResponse(success=True, data=weread_auth.get_info())


@router.post("/weread/verify", response_model=WereadResponse, summary="校验微信读书 Cookie")
async def verify_weread_cookie():
    """打一次最轻的微信读书接口，确认当前 Cookie 是否还有效。"""
    if not weread_auth.is_configured():
        return WereadResponse(success=False, error=weread_client.COOKIE_MISSING_MSG)
    try:
        async with WereadClient() as client:
            valid, message = await client.verify()
    except Exception as e:
        return WereadResponse(success=False, error=f"校验请求失败: {e}")
    return WereadResponse(success=valid, data={"valid": valid, "message": message},
                          error=None if valid else message)


@router.post("/weread/shelf", response_model=WereadResponse, summary="把公众号加入微信读书书架")
async def add_to_shelf(req: ShelfRequest):
    """
    把公众号加入微信读书书架（= 在微信读书里关注它）。

    微信读书只对**书架上**的公众号返回文章列表，所以采集前必须先加入书架。
    正常采集流程会自动做这一步，这里用于手动补做或排查。

    **请求体参数：**
    - **fakeid** (必填): 公众号 FakeID，或 `MP_WXS_*` 形式的 bookId
    - **nickname** (可选): 公众号名称，仅用于提示文案
    """
    try:
        book_id = weread_client.fakeid_to_book_id(req.fakeid)
    except WereadError as e:
        return WereadResponse(success=False, error=e.user_message)

    try:
        async with WereadClient() as client:
            ok, detail = await client.ensure_on_shelf(book_id, req.nickname)
    except WereadError as e:
        return WereadResponse(success=False, error=e.user_message)
    except Exception as e:
        return WereadResponse(success=False, error=f"加入书架失败: {e}")

    data = {"book_id": book_id, "fakeid": req.fakeid, "detail": detail}
    return WereadResponse(success=ok, data=data, error=None if ok else detail)


@router.get("/weread/articles", response_model=WereadResponse, summary="通过微信读书获取文章列表")
async def weread_articles(
    fakeid: str = Query(..., description="公众号 FakeID，或 MP_WXS_* 形式的 bookId"),
    count: int = Query(10, description="获取数量，最大 100", ge=1, le=100),
):
    """
    直连微信读书取文章列表，完全不经过公众号后台。

    翻页上限由 `WEREAD_MAX_PAGES` 控制；列表接口被风控时自动退回
    `/api/mp/cover`（只能取到最新一篇）。
    """
    if not weread_auth.is_configured():
        return WereadResponse(success=False, error=weread_client.COOKIE_MISSING_MSG)

    sub = rss_store.get_subscription(fakeid)
    nickname = (sub or {}).get("nickname", "")

    try:
        async with WereadClient() as client:
            articles: List[Dict] = await client.list_articles(
                fakeid, limit=count, nickname=nickname
            )
    except WereadError as e:
        return WereadResponse(success=False, error=e.user_message)
    except Exception as e:
        logger.error("[WeRead] 文章列表请求异常: %s", e)
        return WereadResponse(success=False, error=f"请求失败: {e}")

    return WereadResponse(success=True, data={
        "articles": articles,
        "total": len(articles),
        "count": len(articles),
        "book_id": weread_client.fakeid_to_book_id(fakeid),
        "source": "weread",
    })


@router.get("/weread/content", response_model=WereadResponse, summary="通过微信读书获取文章正文")
async def weread_content(
    review_id: str = Query("", description="微信读书 reviewId，形如 MP_WXS_123_<token>",
                           alias="review_id"),
    url: str = Query("", description="文章短链 https://mp.weixin.qq.com/s/<token>"),
    fakeid: str = Query("", description="公众号 FakeID，配合 url 使用"),
):
    """
    直连微信读书取文章正文，绕开 mp.weixin.qq.com 的验证码风控。

    二选一：直接给 `review_id`，或给 `url` + `fakeid`（由二者推导 reviewId）。
    """
    if not weread_auth.is_configured():
        return WereadResponse(success=False, error=weread_client.COOKIE_MISSING_MSG)

    target = review_id.strip()
    if not target:
        if not url or not fakeid:
            return WereadResponse(
                success=False,
                error="请提供 review_id，或同时提供 url 与 fakeid"
            )
        target = weread_client.review_id_from_article_url(url, fakeid)
        if not target:
            return WereadResponse(
                success=False,
                error="无法从该链接推导 reviewId（只支持 https://mp.weixin.qq.com/s/<token> 短链）"
            )

    try:
        async with WereadClient() as client:
            result = await client.fetch_article_content(target)
    except WereadError as e:
        return WereadResponse(success=False, error=e.user_message)
    except Exception as e:
        logger.error("[WeRead] 正文请求异常: %s", e)
        return WereadResponse(success=False, error=f"请求失败: {e}")

    return WereadResponse(success=True, data={
        "review_id": target,
        "link": weread_client.review_id_to_mp_url(target),
        "content": result.get("content", ""),
        "plain_content": result.get("plain_content", ""),
        "images": result.get("images", []),
    })
