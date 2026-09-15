#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
健康检查路由
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health", summary="健康检查")
async def health_check():
    """
    检查服务健康状态，并报告微信读书通道是否就绪。
    """
    from utils import weread_client

    return {
        "status": "healthy",
        "version": "1.0.0",
        "framework": "FastAPI",
        "source": "weread",
        "weread": {
            "configured": weread_client.weread_auth.is_configured(),
            "app_api": weread_client.app_domain_usable(),
        },
    }
