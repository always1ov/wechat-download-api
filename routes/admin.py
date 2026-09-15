#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
管理路由 - FastAPI版本
"""

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from utils import rss_store, weread_client
from utils.weread_client import WereadClient, WereadError, weread_auth

router = APIRouter()


# ── 状态管理 ─────────────────────────────────────────────

class StatusResponse(BaseModel):
    """登录状态（微信读书）"""
    authenticated: bool
    loggedIn: bool
    account: str
    nickname: Optional[str] = ""
    vid: Optional[str] = ""
    envManaged: Optional[bool] = False
    status: str


@router.get("/status", response_model=StatusResponse, summary="获取登录状态")
async def get_status():
    """当前微信读书登录状态。

    本项目只用微信读书，这里报告的就是微信读书的登录态。
    """
    info = weread_auth.get_info()
    ok = bool(info.get("configured"))
    vid = info.get("vid", "")
    return StatusResponse(
        authenticated=ok,
        loggedIn=ok,
        account=f"微信读书用户 {vid}" if vid else ("已登录" if ok else ""),
        nickname=f"微信读书用户 {vid}" if vid else "",
        vid=vid,
        envManaged=bool(info.get("env_managed")),
        status="登录正常" if ok else "未登录",
    )


@router.post("/logout", summary="退出登录")
async def logout():
    """清除微信读书登录态。

    环境变量 WEREAD_COOKIE 托管时清不掉（不会去改部署配置），会如实告知。
    """
    if weread_auth.is_env_managed():
        weread_auth.clear()
        return {"success": False,
                "message": "已清除本地保存的登录态，但 WEREAD_COOKIE 环境变量仍然生效，"
                           "需要改部署配置才能真正退出"}
    ok = weread_auth.clear()
    return {"success": ok, "message": "已退出登录" if ok else "退出登录失败"}


# ── 黑名单管理 ─────────────────────────────────────────────

class BlacklistItem(BaseModel):
    id: int
    fakeid: str
    nickname: str
    reason: str
    verification_count: int
    is_active: bool
    blacklisted_at: int
    unblacklisted_at: Optional[int]
    note: str


class AddBlacklistRequest(BaseModel):
    fakeid: str = Field(..., description="公众号ID")
    nickname: str = Field("", description="公众号名称")
    reason: str = Field("manual", description="加入原因")
    note: str = Field("", description="备注")


@router.get("/blacklist", summary="获取黑名单列表")
async def get_blacklist():
    """获取公众号黑名单列表"""
    blacklist = rss_store.get_blacklist()
    return {
        "blacklist": [
            BlacklistItem(
                id=bl["id"],
                fakeid=bl["fakeid"],
                nickname=bl["nickname"],
                reason=bl["reason"],
                verification_count=bl["verification_count"],
                is_active=bool(bl["is_active"]),
                blacklisted_at=bl["blacklisted_at"],
                unblacklisted_at=bl["unblacklisted_at"],
                note=bl["note"],
            )
            for bl in blacklist
        ]
    }


@router.post("/blacklist", summary="添加到黑名单")
async def add_to_blacklist(req: AddBlacklistRequest):
    """手动添加公众号到黑名单"""
    success = rss_store.add_to_blacklist(
        fakeid=req.fakeid,
        nickname=req.nickname,
        reason=req.reason,
        note=req.note or "手动添加"
    )
    if success:
        return {"success": True, "message": f"已将 {req.nickname or req.fakeid} 加入黑名单"}
    return {"success": False, "message": "添加失败"}


@router.delete("/blacklist/{fakeid}", summary="从黑名单移除")
async def remove_from_blacklist(fakeid: str):
    """从黑名单移除公众号（标记为非活跃）"""
    success = rss_store.remove_from_blacklist(fakeid)
    if success:
        return {"success": True, "message": "已从黑名单移除"}
    return {"success": False, "message": "移除失败，记录不存在"}


@router.delete("/blacklist/record/{blacklist_id}", summary="永久删除黑名单记录")
async def delete_blacklist_record(blacklist_id: int):
    """永久删除黑名单记录（仅可删除非活跃记录）"""
    success = rss_store.delete_blacklist_record(blacklist_id)
    if success:
        return {"success": True, "message": "记录已删除"}
    return {"success": False, "message": "删除失败，记录不存在或仍在生效中"}


# ── 分类管理 ─────────────────────────────────────────────

class CategoryItem(BaseModel):
    id: int
    name: str
    description: str
    color: str
    sort_order: int
    subscription_count: int
    created_at: int


class CreateCategoryRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="分类名称")
    description: str = Field("", max_length=200, description="分类描述")
    color: str = Field("blue", description="颜色: blue, green, red, purple, orange, gray")


class UpdateCategoryRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    description: Optional[str] = None
    color: Optional[str] = None


class SetCategoryRequest(BaseModel):
    category_id: Optional[int] = Field(None, description="分类ID，null表示取消分类")


@router.get("/categories", summary="获取分类列表")
async def get_categories():
    """获取所有分类"""
    categories = rss_store.list_categories()
    return {
        "categories": [
            CategoryItem(
                id=c["id"],
                name=c["name"],
                description=c["description"],
                color=c["color"],
                sort_order=c["sort_order"],
                subscription_count=c["subscription_count"],
                created_at=c["created_at"],
            )
            for c in categories
        ]
    }


@router.post("/categories", summary="创建分类")
async def create_category(req: CreateCategoryRequest):
    """创建新分类"""
    category_id = rss_store.create_category(
        name=req.name,
        description=req.description,
        color=req.color
    )
    if category_id:
        return {"success": True, "id": category_id, "message": f"分类 '{req.name}' 创建成功"}
    raise HTTPException(status_code=400, detail="分类名称已存在")


@router.patch("/categories/{category_id}", summary="更新分类")
async def update_category(category_id: int, req: UpdateCategoryRequest):
    """更新分类信息"""
    success = rss_store.update_category(
        category_id=category_id,
        name=req.name,
        description=req.description,
        color=req.color
    )
    if success:
        return {"success": True, "message": "分类已更新"}
    raise HTTPException(status_code=404, detail="分类不存在")


@router.delete("/categories/{category_id}", summary="删除分类")
async def delete_category(category_id: int):
    """删除分类（订阅会自动解除关联）"""
    success = rss_store.delete_category(category_id)
    if success:
        return {"success": True, "message": "分类已删除"}
    raise HTTPException(status_code=404, detail="分类不存在")


@router.get("/categories/{category_id}/subscriptions", summary="获取分类下的订阅")
async def get_category_subscriptions(category_id: int):
    """获取分类下的所有订阅"""
    category = rss_store.get_category(category_id)
    if not category:
        raise HTTPException(status_code=404, detail="分类不存在")
    
    subscriptions = rss_store.get_subscriptions_by_category(category_id)
    return {
        "category": CategoryItem(
            id=category["id"],
            name=category["name"],
            description=category["description"],
            color=category["color"],
            sort_order=category["sort_order"],
            subscription_count=len(subscriptions),
            created_at=category["created_at"],
        ),
        "subscriptions": subscriptions
    }


@router.put("/subscriptions/{fakeid}/category", summary="设置订阅分类")
async def set_subscription_category(fakeid: str, req: SetCategoryRequest):
    """设置订阅的分类"""
    # 如果指定了分类，验证分类存在
    if req.category_id is not None:
        category = rss_store.get_category(req.category_id)
        if not category:
            raise HTTPException(status_code=404, detail="分类不存在")
    
    success = rss_store.set_subscription_category(fakeid, req.category_id)
    if success:
        return {"success": True, "message": "分类已设置"}
    raise HTTPException(status_code=404, detail="订阅不存在")


# ── 历史文章获取 ─────────────────────────────────────────────

class FetchHistoryRequest(BaseModel):
    fakeid: str = Field(..., description="公众号ID")
    count: int = Field(1, ge=1, le=100, description="获取数量，1-100篇")


class FetchHistoryResponse(BaseModel):
    success: bool
    message: str
    fetched_count: int = 0
    new_count: int = 0


@router.post("/history/fetch", response_model=FetchHistoryResponse, summary="获取历史文章")
async def fetch_history_articles(req: FetchHistoryRequest):
    """
    获取公众号的历史文章并存入数据库。
    简化版：直接调用微信 API 获取历史文章列表，不涉及用户权限和付费逻辑。
    """
    if not weread_client.is_enabled():
        return FetchHistoryResponse(
            success=False,
            message=weread_client.COOKIE_MISSING_MSG,
            fetched_count=0,
            new_count=0
        )
    
    # 检查订阅是否存在
    subscriptions = rss_store.list_subscriptions()
    sub = next((s for s in subscriptions if s["fakeid"] == req.fakeid), None)
    if not sub:
        return FetchHistoryResponse(
            success=False,
            message="订阅不存在，请先添加订阅",
            fetched_count=0,
            new_count=0
        )
    
    try:
        # 调用 poller 的内部方法获取文章列表
        fetched_count, new_count = await _fetch_history_internal(
            fakeid=req.fakeid,
            target_count=req.count
        )
        
        return FetchHistoryResponse(
            success=True,
            message=f"获取完成，共获取 {fetched_count} 篇，新增 {new_count} 篇",
            fetched_count=fetched_count,
            new_count=new_count
        )
    except Exception as e:
        return FetchHistoryResponse(
            success=False,
            message=f"获取失败: {str(e)}",
            fetched_count=0,
            new_count=0
        )


async def _fetch_history_internal(fakeid: str, target_count: int) -> tuple:
    """通过微信读书深翻文章列表，标记为 source='deep_fetch' 入库。

    翻页深度受 WEREAD_MAX_PAGES 限制（默认 5 页、每页约 50 条）；想挖更深就调大它。
    数据库的 UNIQUE(fakeid, link) 负责去重：轮询器已拉到的保持 source='poll'，
    只有这里新拿到的才记为 deep_fetch。

    返回 (本次取回篇数, 新增篇数)。
    """
    sub = rss_store.get_subscription(fakeid)
    if not sub:
        raise ValueError("订阅不存在")

    try:
        async with WereadClient() as client:
            articles = await client.list_articles(
                fakeid, limit=target_count, nickname=sub.get("nickname", "")
            )
    except WereadError as e:
        raise ValueError(e.user_message) from e

    articles = articles[:target_count]
    new_count = rss_store.save_articles(fakeid, articles, source="deep_fetch")
    return len(articles), new_count
