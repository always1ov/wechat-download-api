#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
文章路由 - FastAPI版本
"""

import logging
import re
import time
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from utils import rss_store, weread_client
from utils.weread_client import WereadError
from utils.auth_manager import auth_manager
from utils.helpers import extract_article_info, parse_article_url, is_image_text_message, has_article_content, get_client_ip
from utils.rate_limiter import rate_limiter
from utils.webhook import webhook
from utils.http_client import fetch_page

logger = logging.getLogger(__name__)

router = APIRouter()

class ArticleRequest(BaseModel):
    """文章请求"""
    url: str = Field(..., description="微信文章链接，如 https://mp.weixin.qq.com/s/xxxxx")
    fakeid: Optional[str] = Field(
        None,
        description="文章所属公众号的 FakeID（可选）。走微信读书通道时用它推导 reviewId；"
                    "不填则从已订阅文章库里反查",
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
    - **url** (必填): 微信文章链接，支持 `https://mp.weixin.qq.com/s/xxxxx` 格式

    **返回字段：**
    - `title`: 文章标题
    - `content`: HTML 正文（保留原始排版）
    - `plain_content`: 纯文本正文（去除所有 HTML 标签，适合直接阅读或 AI 处理）
    - `author`: 作者
    - `publish_time`: 发布时间戳
    - `images`: 文章内的图片列表
    """
    client_ip = get_client_ip(request)
    allowed, error_msg = rate_limiter.check_rate_limit(client_ip, "/api/article")
    if not allowed:
        return {"success": False, "error": f"Rate limited: {error_msg}"}

    credentials = auth_manager.get_credentials()
    source = weread_client.article_source()
    use_direct = bool(credentials) and source != "weread"
    use_weread = weread_client.is_enabled() and source != "mp"

    if not use_direct and not use_weread:
        return {"success": False,
                "error": "服务器未登录，请先访问管理页面扫码登录，或配置微信读书 Cookie"}

    logger.info("[Article] request from %s: %s", client_ip, article_request.url[:80])

    html = None
    direct_error = None
    if use_direct:
        try:
            html = await fetch_page(
                article_request.url,
                extra_headers={"Referer": "https://mp.weixin.qq.com/"},
                timeout=120
            )
        except Exception as e:
            error_str = str(e)
            direct_error = ("请求超时，请稍后重试" if "timeout" in error_str.lower()
                            else f"处理请求时发生错误: {error_str}")
            logger.warning("[Article] 直抓失败: %s", error_str[:200])

    if html is not None and has_article_content(html):
        try:
            return {"success": True, "data": _extract_from_page(html, article_request.url)}
        except Exception as e:
            logger.error("[Article] 解析正文失败: %s", e)
            direct_error = f"处理请求时发生错误: {e}"

    # 直抓拿到了页面但不是正文 —— 判定原因并告警（验证码/登录失效是运维关心的信号）
    if html is not None and direct_error is None:
        if "verify" in html or "验证" in html or "环境异常" in html:
            await webhook.notify('verification_required', {
                'url': article_request.url,
                'ip': client_ip
            })
            direct_error = ("触发微信安全验证。解决方法：1) 在浏览器中打开文章URL完成验证 "
                            "2) 等待30分钟后重试 3) 降低请求频率")
        elif "请登录" in html:
            await webhook.notify('login_expired', {
                'account': (credentials or {}).get('nickname', ''),
                'url': article_request.url
            })
            direct_error = "登录已失效，请重新扫码登录"
        else:
            direct_error = "无法获取文章内容。可能原因：文章被删除、访问受限或需要验证。"

    # 微信读书兜底：它有独立登录态，不受公众号后台的验证码/风控影响
    if use_weread:
        data, weread_error = await _fetch_via_weread(
            article_request.url, article_request.fakeid
        )
        if data:
            logger.info("[Article] 直抓未果，已通过微信读书取到正文: %s",
                        article_request.url[:80])
            return {"success": True, "data": data}
        if direct_error:
            return {"success": False,
                    "error": f"{direct_error}（微信读书兜底也失败：{weread_error}）"}
        return {"success": False, "error": weread_error}

    return {"success": False,
            "error": direct_error or "无法获取文章内容。可能原因：文章被删除、访问受限或需要验证。"}


def _extract_from_page(html: str, url: str) -> dict:
    """从完整文章页解析结构化数据（短链页拿不到 __biz 时，再从页内脚本里找）。"""
    params = parse_article_url(url)

    if not params or not params.get('__biz'):
        location_match = re.search(r'var\s+msg_link\s*=\s*"([^"]+)"', html)
        if location_match:
            real_url = location_match.group(1).replace('&amp;', '&')
            params = parse_article_url(real_url)

    if not params or not params.get('__biz'):
        href_match = re.search(r'window\.location\.href\s*=\s*"([^"]+)"', html)
        if href_match:
            real_url = href_match.group(1).replace('&amp;', '&')
            params = parse_article_url(real_url)

    if not params or not params.get('__biz'):
        biz_match = re.search(r'var\s+__biz\s*=\s*"([^"]+)"', html)
        mid_match = re.search(r'var\s+mid\s*=\s*"([^"]+)"', html)
        idx_match = re.search(r'var\s+idx\s*=\s*"([^"]+)"', html)
        sn_match = re.search(r'var\s+sn\s*=\s*"([^"]+)"', html)

        if all([biz_match, mid_match, idx_match, sn_match]):
            params = {
                '__biz': biz_match.group(1),
                'mid': mid_match.group(1),
                'idx': idx_match.group(1),
                'sn': sn_match.group(1)
            }

    if not params or not params.get('__biz'):
        params = None

    return extract_article_info(html, params)


async def _fetch_via_weread(url: str, fakeid: Optional[str] = None):
    """通过微信读书正文接口取文章，返回 (文章数据, 错误提示)。

    reviewId = <bookId>_<短链 token>，bookId 由 fakeid base64 解码得到。
    调用方没给 fakeid 时，按链接从已订阅文章库里反查。
    """
    token = weread_client.extract_article_token(url)
    if not token:
        return None, "微信读书通道只支持 https://mp.weixin.qq.com/s/<token> 形式的短链"

    row = None
    try:
        row = rss_store.get_article_by_link(url)
    except Exception as e:
        logger.warning("[Article] 按链接反查订阅失败: %s", e)

    fakeid = fakeid or (row or {}).get("fakeid", "")
    if not fakeid:
        return None, ("微信读书通道需要知道文章所属公众号："
                      "请在请求体里带上 fakeid，或先通过 RSS 订阅该公众号")

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
