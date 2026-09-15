#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
文章正文 —— 走微信读书

直抓 mp.weixin.qq.com 会触发验证码风控，本项目不再走那条路。
微信读书的正文接口 /web/mp/content 用自己的登录态，没有验证码问题。

reviewId = <bookId>_<文章短链 token>，所以只支持
https://mp.weixin.qq.com/s/<token> 形式的短链；长链（/s?__biz=...）
没有 token，推不出 reviewId。
"""

import logging
import time
from typing import List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from utils import rss_store, weread_client
from utils.helpers import extract_article_info, get_client_ip
from utils.rate_limiter import rate_limiter
from utils.weread_client import WereadError

logger = logging.getLogger(__name__)

router = APIRouter()


class ArticleRequest(BaseModel):
    """文章请求"""
    url: str = Field(..., description="微信文章短链，如 https://mp.weixin.qq.com/s/xxxxx")
    fakeid: Optional[str] = Field(
        None,
        description="文章所属公众号的 FakeID（可选）。用它推导 reviewId；"
                    "不填则从已订阅文章库里按链接反查",
    )


class ArticleData(BaseModel):
    """文章数据"""
    title: str = Field(..., description="文章标题")
    content: str = Field(..., description="文章 HTML 正文（保留原始排版）")
    plain_content: str = Field("", description="纯文本正文（去除所有 HTML 标签，适合直接阅读或 AI 处理）")
    images: List[str] = Field(default_factory=list, description="文章内图片 URL 列表")
    author: str = Field("", description="作者")
    publish_time: int = Field(0, description="发布时间戳（秒）")
    publish_time_str: Optional[str] = Field(None, description="可读发布时间，如 2026-02-24 09:00:00")


class ArticleResponse(BaseModel):
    """文章响应"""
    success: bool = Field(..., description="是否成功")
    data: Optional[ArticleData] = Field(None, description="文章数据，失败时为 null")
    error: Optional[str] = Field(None, description="错误信息，成功时为 null")


@router.post("/article", response_model=ArticleResponse, summary="获取文章内容")
async def get_article(article_request: ArticleRequest, request: Request):
    """
    解析微信公众号文章，返回标题、正文、图片等结构化数据。

    **请求体参数：**
    - **url** (必填): 微信文章短链 `https://mp.weixin.qq.com/s/xxxxx`
    - **fakeid** (可选): 所属公众号 FakeID；不填则按链接从订阅库里反查

    **返回字段：**
    - `title` / `author` / `publish_time`: 元数据（微信读书多返回正文片段，
      拿不到时用订阅库里已有的补齐）
    - `content`: HTML 正文
    - `plain_content`: 纯文本正文
    - `images`: 文章内的图片列表
    """
    client_ip = get_client_ip(request)
    allowed, error_msg = rate_limiter.check_rate_limit(client_ip, "/api/article")
    if not allowed:
        return {"success": False, "error": f"Rate limited: {error_msg}"}

    if not weread_client.is_enabled():
        return {"success": False, "error": weread_client.COOKIE_MISSING_MSG}

    logger.info("[Article] request from %s: %s", client_ip, article_request.url[:80])

    data, error = await _fetch_via_weread(article_request.url, article_request.fakeid)
    if data:
        return {"success": True, "data": data}
    return {"success": False, "error": error}


async def _fetch_via_weread(url: str, fakeid: Optional[str] = None):
    """通过微信读书正文接口取文章，返回 (文章数据, 错误提示)。"""
    token = weread_client.extract_article_token(url)
    if not token:
        return None, "只支持 https://mp.weixin.qq.com/s/<token> 形式的短链"

    row = None
    try:
        row = rss_store.get_article_by_link(url)
    except Exception as e:
        logger.warning("[Article] 按链接反查订阅失败: %s", e)

    fakeid = fakeid or (row or {}).get("fakeid", "")
    if not fakeid:
        return None, ("需要知道文章所属公众号：请在请求体里带上 fakeid，"
                      "或先订阅该公众号")

    try:
        review_id = weread_client.build_review_id(fakeid, token)
    except WereadError as e:
        return None, e.user_message

    try:
        async with weread_client.WereadClient() as client:
            html = await client.get_content_html(review_id)
    except WereadError as e:
        return None, e.user_message

    try:
        processed = weread_client.process_content_html(html, review_id=review_id)
    except WereadError:
        return None, "微信读书未返回正文（文章可能已删除，或未在微信读书发布）"

    # 微信读书返回的多是正文片段，标题/作者/时间大概率抓不到，尽力而为
    meta = extract_article_info(html, None)
    data = {
        "title": meta.get("title", ""),
        "content": processed.get("content", ""),
        "plain_content": processed.get("plain_content", ""),
        "images": processed.get("images", []),
        "author": meta.get("author", ""),
        "publish_time": meta.get("publish_time", 0),
        "publish_time_str": meta.get("publish_time_str") or None,
    }

    # 片段里没有的元数据，用订阅库里已有的补齐
    if row:
        data["title"] = data.get("title") or row.get("title", "")
        data["author"] = data.get("author") or row.get("author", "")
        data["publish_time"] = data.get("publish_time") or row.get("publish_time", 0)
    if data.get("publish_time") and not data.get("publish_time_str"):
        data["publish_time_str"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(data["publish_time"])
        )
    return data, None
