#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
健康检查路由
"""

import os

from fastapi import APIRouter

router = APIRouter()


def build_info() -> dict:
    """镜像的构建标记（CI 通过 Dockerfile 的 ARG 烤进来）。

    用来回答「我 pull 完到底更新没有」——不然只能靠肉眼比对页面。
    """
    return {
        "sha": os.getenv("BUILD_SHA", "unknown"),
        "short_sha": os.getenv("BUILD_SHA", "unknown")[:7],
        "ref": os.getenv("BUILD_REF", "dev"),
        "time": os.getenv("BUILD_TIME", ""),
    }


@router.get("/health", summary="健康检查")
async def health_check():
    """
    检查服务健康状态，并报告微信读书通道是否就绪。

    `build` 是镜像的构建标记（commit sha / 分支 / 时间），
    用来确认当前跑的是哪一版代码。
    """
    from utils import image_cache, site_url, weread_client

    return {
        "status": "healthy",
        "version": "1.0.0",
        "framework": "FastAPI",
        "source": "weread",
        "build": build_info(),
        "weread": {
            "configured": weread_client.weread_auth.is_configured(),
            "app_api": weread_client.app_domain_usable(),
        },
        "image_cache": image_cache.stats(),
        "site_url": site_url.describe(),
    }
