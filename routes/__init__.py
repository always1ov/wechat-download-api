#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
路由模块初始化
"""

from . import (
    admin, article, articles, export, feed, health, image, rss, search, stats, weread,
)

__all__ = [
    'admin', 'article', 'articles', 'export', 'feed', 'health',
    'image', 'rss', 'search', 'stats', 'weread',
]
