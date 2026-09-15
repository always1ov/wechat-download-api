#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
公众号搜索 —— 走微信读书

微信读书有两条找号的路：
1. App 域 /store/search，能搜全网，但对登录态要求严；
2. 书架，只能搜到「你在微信读书里已关注的号」，但用的是登录态校验同一个接口，
   扫码有效就一定能出结果。

第 2 条是保底：想订阅哪个号，先在微信读书 App 里关注，这里就搜得到。
"""

import logging
import os

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from typing import Optional, List

from utils import rss_store, weread_client
from utils.image_proxy import proxy_image_url

logger = logging.getLogger(__name__)

router = APIRouter()


def get_base_url(request: Request) -> str:
    """服务的基础 URL，优先 SITE_URL，其次反代头。"""
    site_url = os.getenv("SITE_URL", "").strip()
    if site_url:
        return site_url.rstrip("/")
    proto = request.headers.get("X-Forwarded-Proto", "http")
    host = (request.headers.get("X-Forwarded-Host")
            or request.headers.get("Host", "localhost:5000"))
    return f"{proto}://{host}"


class Account(BaseModel):
    """公众号模型"""
    id: str
    name: str
    round_head_img: str


class SearchResponse(BaseModel):
    """搜索响应模型"""
    success: bool
    data: Optional[dict] = None
    error: Optional[str] = None


def _filter_blacklisted(accounts: list) -> list:
    """过滤已知失效号，避免搜到后订阅了却拉不到内容。"""
    blacklisted = set(rss_store.get_active_blacklist_fakeids())
    return [a for a in accounts if a.get("fakeid") not in blacklisted]


def _empty_hint(query: str, shelf_size: int, store_error) -> str:
    """没搜到时，告诉用户下一步该干嘛，而不是干巴巴一句「未找到」。

    绝大多数情况是「这个号不在你的微信读书书架上」—— 全网搜索那条路对登录态
    要求很严，经常不可用，书架才是稳的。所以提示统一往「去 App 关注」上引。
    """
    name = (query or "").strip()
    quoted = f"「{name}」" if name else "这个号"
    if shelf_size == 0:
        tip = (f"没找到{quoted}，而且你的微信读书书架是空的。"
               f"请先在微信读书 App 里关注{quoted}（公众号会自动进书架），再回来搜。")
    else:
        tip = (f"书架上这 {shelf_size} 个公众号里没有{quoted}。"
               f"请先在微信读书 App 里关注{quoted}，再回来搜；"
               f"也可以直接点「从微信读书书架导入」看看书架上都有什么。")
    if store_error is not None:
        # 全网搜索这条路本身还报了错，一并说清楚，免得用户以为是自己关注错了
        tip += f"（全网搜索当前不可用：{store_error.message}）"
    return tip


async def searchbiz_raw(query: str, base_url: str = ""):
    """搜公众号，返回 (过滤黑名单后的列表, 错误信息, 空结果提示)。

    - error 只在真失败时非空（没登录、Cookie 失效、接口异常）；
    - hint 只在 error 为空、结果又是空列表时非空，说明为什么没搜到、该怎么办。

    供 /searchbiz 与批量订阅复用。
    """
    if not weread_client.is_enabled():
        return [], weread_client.COOKIE_MISSING_MSG, ""

    needle = (query or "").strip().lower()
    accounts, store_error, shelf_size = [], None, 0

    try:
        async with weread_client.WereadClient() as client:
            try:
                accounts = await client.search_mp_accounts(query)
            except weread_client.WereadError as e:
                store_error = e
                logger.info("[WeRead] /store/search 不可用（%s），改从书架里找", e.message)

            if not accounts:
                shelf = await client.get_shelf_accounts()
                shelf_size = len(shelf)
                accounts = [
                    a for a in shelf
                    if not needle
                    or needle in (a.get("nickname", "") or "").lower()
                    or needle in (a.get("alias", "") or "").lower()
                ]
                if accounts:
                    logger.info("[WeRead] 从书架匹配到 %d 个公众号", len(accounts))
                elif store_error is not None and not shelf:
                    # 两条路都没结果、且搜索本身报过错：如果是登录态的问题，
                    # 那就是真失败，别拿「去关注一下」糊弄过去
                    if store_error.is_auth_error:
                        return [], store_error.user_message, ""
    except weread_client.WereadError as e:
        return [], e.user_message, ""
    except Exception as e:
        logger.error("[WeRead] 搜索异常: %s", e)
        return [], f"搜索失败: {e}", ""

    for acc in accounts:
        acc["round_head_img"] = proxy_image_url(acc.get("round_head_img", ""), base_url)
    accounts = _filter_blacklisted(accounts)
    hint = "" if accounts else _empty_hint(query, shelf_size, store_error)
    return accounts, None, hint


@router.get("/searchbiz", response_model=SearchResponse, summary="搜索公众号")
async def search_accounts(
    query: str = Query(..., description="公众号名称或关键词", alias="query"),
    request: Request = None,
):
    """
    按关键词搜索微信公众号，获取 FakeID。

    数据来自微信读书：先试 App 域全网搜索，不通则在你的微信读书书架里按名字匹配。
    **搜不到想要的号时**，先去微信读书 App 关注它，再回来搜。
    书架上的号也可以用 `POST /api/weread/shelf/import` 一键全部导入。

    **查询参数：**
    - **query** (必填): 搜索关键词（公众号名称）

    **返回字段（均在 `data` 下）：**
    - `list[]`: 匹配的公众号列表，每项含 `fakeid`、`nickname`、`alias`、`round_head_img`
    - `total`: 匹配数量（已过滤黑名单）
    - `hint`: 仅在 `total` 为 0 时出现，说明为什么没搜到、下一步该怎么办
    """
    base_url = get_base_url(request) if request else ""
    accounts, err, hint = await searchbiz_raw(query, base_url)
    if err:
        return SearchResponse(success=False, error=err)
    data = {"list": accounts, "total": len(accounts)}
    if hint:
        data["hint"] = hint
    return SearchResponse(success=True, data=data)
