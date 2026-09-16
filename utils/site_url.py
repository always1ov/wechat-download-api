#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
SITE_URL 的唯一入口（带校验）。

RSS 里的图片地址是拿 SITE_URL 拼出来的，所以 SITE_URL 配错的后果不是
「图片显示不出来」这么轻 —— 它会被写进每一条 RSS，阅读器拿它当 URL 和
Referer 用。实测有阅读器（Read You）在遇到非 ASCII 的 Referer 时直接崩：

    java.lang.IllegalArgumentException:
        Unexpected char 0x4f60 at 0 in Referer value: 你的IP

0x4f60 就是「你」—— 用户把文档里的中文占位符 `http://你的IP:5000` 原样粘了进去。
这种配置错误必须在服务端就挡住，不能顺着写进 RSS 去崩别人的阅读器。

所以这里统一做三件事：
1. 校验：必须是 http/https，主机名必须是 ASCII 且非空；
2. 认出常见的占位符（你的IP、your-domain、example.com 等）；
3. 不合格就当没配，回退到请求自带的 Host —— 宁可回退，也不能输出坏地址。
"""

import logging
import os
from typing import Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 文档里出现过的占位符。原样粘进去比配错更常见，单独认出来给明确提示。
PLACEHOLDER_HINTS = (
    "你的ip", "你的域名", "实际访问地址", "your-domain", "your_domain",
    "yourdomain", "example.com", "your-ip", "your_ip", "changeme",
    "<ip>", "<domain>", "xxx.xxx",
)

_warned = False


def validate(raw: str) -> Tuple[bool, str]:
    """校验一个 SITE_URL。返回 (是否可用, 不可用的原因)。"""
    value = (raw or "").strip()
    if not value:
        return False, "未配置"

    lowered = value.lower()
    for hint in PLACEHOLDER_HINTS:
        if hint in lowered:
            return False, f"看起来还是文档里的占位符（含「{hint}」），需要改成实际访问地址"

    try:
        parsed = urlparse(value)
    except Exception as exc:
        return False, f"不是合法 URL: {exc}"

    if parsed.scheme not in ("http", "https"):
        return False, f"协议必须是 http 或 https（当前是 {parsed.scheme or '空'}）"
    if not parsed.netloc:
        return False, "缺少主机名"

    # 非 ASCII 主机名会让阅读器在设置 Referer 时抛异常（见模块开头）
    try:
        parsed.netloc.encode("ascii")
    except UnicodeEncodeError:
        bad = next((c for c in parsed.netloc if ord(c) > 127), "")
        return False, (f"主机名含非 ASCII 字符「{bad}」—— 这会让部分 RSS 阅读器"
                       f"直接崩溃，必须改成实际的 IP 或域名")

    return True, ""


def configured() -> str:
    """返回校验通过的 SITE_URL（已去掉尾部斜杠）；不合格返回空串。

    不合格时会打一条警告（只打一次，不刷屏），说明为什么以及回退成什么。
    """
    global _warned
    raw = os.getenv("SITE_URL", "")
    if not (raw or "").strip():
        return ""

    ok, reason = validate(raw)
    if ok:
        return raw.strip().rstrip("/")

    if not _warned:
        _warned = True
        logger.error(
            "SITE_URL 配置有问题，已忽略并改用请求里的 Host：%s（当前值：%r）。"
            "RSS 里的图片地址是用它拼的，配错会导致图片打不开、"
            "个别阅读器甚至会崩溃。",
            reason, raw,
        )
    return ""


def reset_warning() -> None:
    """测试用：清掉「已经警告过」的标记。"""
    global _warned
    _warned = False


def from_request(request) -> str:
    """从请求头推导服务地址（SITE_URL 不可用时的回退）。"""
    try:
        proto = request.headers.get("X-Forwarded-Proto", "") or request.url.scheme or "http"
        host = (request.headers.get("X-Forwarded-Host")
                or request.headers.get("Host")
                or "localhost:5000")
        return f"{proto}://{host}".rstrip("/")
    except Exception:
        return "http://localhost:5000"


def base_url(request=None) -> str:
    """服务的基础 URL：优先合格的 SITE_URL，否则回退请求里的 Host。"""
    site = configured()
    if site:
        return site
    if request is not None:
        return from_request(request)
    return "http://localhost:5000"


def describe() -> dict:
    """给 /api/health 用的配置自检。"""
    raw = os.getenv("SITE_URL", "")
    ok, reason = validate(raw)
    return {
        "value": raw,
        "valid": ok,
        "reason": reason,
        "effective": configured() or "(回退到请求 Host)",
    }
