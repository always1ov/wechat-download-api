#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
搜索路由 - FastAPI版本
"""

import os
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from typing import Optional, List
import time
import httpx
import json
import logging

from utils import weread_client
from utils.auth_manager import auth_manager
from utils.image_proxy import proxy_image_url
from utils.wechat_status import is_login_expired, LOGIN_EXPIRED_MSG

logger = logging.getLogger(__name__)

router = APIRouter()


def get_base_url(request: Request) -> str:
    """
    获取服务的基础 URL，优先使用环境变量 SITE_URL，
    支持反向代理（检测 X-Forwarded-Proto 和 X-Forwarded-Host）
    """
    site_url = os.getenv("SITE_URL", "").strip()
    if site_url:
        return site_url.rstrip("/")
    
    proto = request.headers.get("X-Forwarded-Proto", "http")
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "localhost:5000")
    
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
    """[2026-05-18] 过滤已知失效号：避免搜到老 fakeid 订阅后拉不到内容。"""
    from utils import rss_store
    blacklisted = set(rss_store.get_active_blacklist_fakeids())
    return [a for a in accounts if a.get("fakeid") not in blacklisted]


async def search_via_weread(query: str, base_url: str = "") -> list:
    """用微信读书搜公众号（i 域 /store/search）。

    公众号后台的 searchbiz 是本项目最后一个硬依赖后台的环节；微信读书自己能搜，
    搜到的 MP_WXS_* 换算回 fakeid 后可直接喂给既有的订阅/文章接口。
    i 域不可用时抛 WereadError，由调用方回退到后台。
    """
    async with weread_client.WereadClient() as client:
        accounts = await client.search_mp_accounts(query)
    for acc in accounts:
        acc["round_head_img"] = proxy_image_url(acc.get("round_head_img", ""), base_url)
    return _filter_blacklisted(accounts)


async def searchbiz_raw(query: str, base_url: str = ""):
    """搜公众号，返回 (过滤黑名单后的公众号列表, 错误信息)。
    成功时 error=None；失败时 list 空、error 为提示。供 /searchbiz 与批量订阅复用。

    公众号后台优先；后台没登录或明确报错时改用微信读书搜索。
    """
    credentials = auth_manager.get_credentials()
    source = weread_client.article_source()
    token = (credentials or {}).get("token")
    cookie = (credentials or {}).get("cookie")
    use_mp = bool(token and cookie) and source != "weread"
    use_weread = weread_client.is_enabled() and source != "mp"

    async def _weread_fallback(mp_error: str):
        if not use_weread:
            return [], mp_error
        try:
            accounts = await search_via_weread(query, base_url)
        except weread_client.WereadError as e:
            logger.info("[WeRead] 搜索不可用: %s", e.message)
            return [], mp_error if mp_error else e.user_message
        except Exception as e:
            logger.warning("[WeRead] 搜索异常: %s", e)
            return [], mp_error if mp_error else f"微信读书搜索失败: {e}"
        if mp_error:
            logger.info("[WeRead] 公众号后台搜索不可用，已改用微信读书搜索")
        return accounts, None

    if not use_mp:
        if not use_weread:
            return [], "服务器未登录，请先访问管理页面扫码登录"
        return await _weread_fallback("")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://mp.weixin.qq.com/cgi-bin/searchbiz",
                params={
                    "action": "search_biz",
                    "token": token,
                    "lang": "zh_CN",
                    "f": "json",
                    "ajax": 1,
                    "random": time.time(),
                    "query": query,
                    "begin": 0,
                    "count": 5
                },
                headers={
                    "Cookie": cookie,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }
            )
            result = response.json()

        if result.get("base_resp", {}).get("ret") == 0:
            out = [
                {
                    "fakeid": acc.get("fakeid", ""),
                    "nickname": acc.get("nickname", ""),
                    "alias": acc.get("alias", ""),
                    "round_head_img": proxy_image_url(acc.get("round_head_img", ""), base_url),
                    "service_type": acc.get("service_type", 0),
                }
                for acc in result.get("list", [])
            ]
            return _filter_blacklisted(out), None
        else:
            base = result.get("base_resp", {})
            ret_code = base.get("ret")
            err_msg = base.get("err_msg", "未知错误")
            if is_login_expired(ret_code, err_msg):
                return await _weread_fallback(LOGIN_EXPIRED_MSG)
            return await _weread_fallback(f"搜索失败: ret={ret_code}, msg={err_msg}")

    except json.JSONDecodeError:
        # 微信返回了非 JSON（通常是登录/验证页）→ 登录态失效
        return await _weread_fallback(LOGIN_EXPIRED_MSG)
    except Exception as e:
        print(f"[ERROR] search failed: {str(e)}")
        return await _weread_fallback(f"搜索请求失败: {str(e)}")


@router.get("/searchbiz", response_model=SearchResponse, summary="搜索公众号")
async def search_accounts(query: str = Query(..., description="公众号名称或关键词", alias="query"), request: Request = None):
    """
    按关键词搜索微信公众号，获取 FakeID。

    **查询参数：**
    - **query** (必填): 搜索关键词（公众号名称）

    **返回字段（均在 `data` 下）：**
    - `list[]`: 匹配的公众号列表，每项含 `fakeid`、`nickname`、`alias`、`round_head_img`、`service_type`
    - `total`: 匹配数量（已过滤黑名单）
    """
    base_url = get_base_url(request) if request else ""
    accounts, err = await searchbiz_raw(query, base_url)
    if err:
        return SearchResponse(success=False, error=err)
    return SearchResponse(success=True, data={"list": accounts, "total": len(accounts)})
