#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
文章列表 API —— 走微信读书

翻页深度受 WEREAD_MAX_PAGES 限制；微信读书没有「在某个号内搜文章」的接口，
带 keyword 时只能对已拉回的列表做标题/摘要过滤。
"""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from utils import rss_store, weread_client

logger = logging.getLogger(__name__)

router = APIRouter()


class ArticleItem(BaseModel):
    """文章列表项"""
    aid: str
    title: str
    link: str
    update_time: int
    create_time: int
    digest: Optional[str] = None
    cover: Optional[str] = None
    author: Optional[str] = None


class ArticlesResponse(BaseModel):
    """文章列表响应"""
    success: bool
    data: Optional[Dict] = None
    error: Optional[str] = None


@router.get("/articles", response_model=ArticlesResponse, summary="获取文章列表")
async def get_articles(
    fakeid: str = Query(..., description="目标公众号的 FakeID（通过搜索或书架接口获取）"),
    begin: int = Query(0, description="偏移量，从第几条开始", ge=0, alias="begin"),
    count: int = Query(10, description="获取数量，最大 100", ge=1, le=100),
    keyword: Optional[str] = Query(None, description="在该公众号内按标题/摘要过滤（可选）"),
):
    """
    获取指定公众号的文章列表，支持分页。

    **使用流程：**
    1. `GET /api/weread/shelf/accounts` 看书架上有哪些公众号（或 `GET /api/public/searchbiz` 搜索）
    2. 取其中的 `fakeid`
    3. 调用本接口获取文章列表

    **查询参数：**
    - **fakeid** (必填): 目标公众号的 FakeID
    - **begin** (可选): 偏移量，默认 0
    - **count** (可选): 获取数量，默认 10，最大 100
    - **keyword** (可选): 按标题/摘要过滤已拉回的文章

    **注意**：`total` 是本次拉回的条数，不是公众号历史总数 —— 微信读书列表接口
    不给总数，翻页深度受 `WEREAD_MAX_PAGES` 限制。
    """
    if not weread_client.is_enabled():
        return ArticlesResponse(success=False, error=weread_client.COOKIE_MISSING_MSG)

    sub = rss_store.get_subscription(fakeid)
    nickname = (sub or {}).get("nickname", "")

    try:
        async with weread_client.WereadClient() as client:
            articles: List[Dict] = await client.list_articles(
                fakeid, limit=begin + count, nickname=nickname
            )
    except weread_client.WereadError as e:
        logger.warning("[WeRead] 文章列表获取失败 %s: %s", fakeid[:8], e.message)
        return ArticlesResponse(success=False, error=e.user_message)
    except Exception as e:
        logger.error("[WeRead] 文章列表请求异常: %s", e)
        return ArticlesResponse(success=False, error=f"获取文章列表失败: {e}")

    if keyword:
        needle = keyword.lower()
        articles = [
            a for a in articles
            if needle in (a.get("title", "") or "").lower()
            or needle in (a.get("digest", "") or "").lower()
        ]

    page = articles[begin:begin + count]
    return ArticlesResponse(success=True, data={
        "articles": [
            {
                "aid": a.get("aid", ""),
                "title": a.get("title", ""),
                "link": a.get("link", ""),
                "update_time": a.get("update_time", 0),
                "create_time": a.get("create_time", 0),
                "digest": a.get("digest", ""),
                "cover": a.get("cover", ""),
                "author": a.get("author", ""),
            }
            for a in page
        ],
        "total": len(articles),
        "begin": begin,
        "count": len(page),
        "keyword": keyword,
        "source": "weread",
    })


@router.get("/articles/search", response_model=ArticlesResponse, summary="搜索公众号文章")
async def search_articles(
    fakeid: str = Query(..., description="目标公众号的 FakeID"),
    query: str = Query(..., description="搜索关键词", alias="query"),
    begin: int = Query(0, description="偏移量，默认 0", ge=0, alias="begin"),
    count: int = Query(10, description="获取数量，默认 10，最大 100", ge=1, le=100),
):
    """
    在指定公众号内按关键词过滤文章。

    微信读书没有「号内搜索」接口，这里是对已拉回的列表做标题/摘要过滤，
    召回范围受 `WEREAD_MAX_PAGES` 限制，不等同于全量搜索。
    """
    return await get_articles(fakeid=fakeid, keyword=query, begin=begin, count=count)
