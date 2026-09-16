#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
RSS 后台轮询器

定时通过微信读书拉取订阅号的最新文章（含正文）并缓存到 SQLite。

本项目只走微信读书一条路：公众号后台凭证约 4 天过期、appmsgpublish 有频率风控、
正文页直抓会触发验证码，实测不可用；微信读书用自己的登录态，且能通过书架
直接拿到用户关注的公众号，不需要公众号后台。
"""

import asyncio
import logging
import os
from typing import Dict, List

from utils import rss_store
from utils import weread_client
from utils.weread_client import WereadClient, WereadError

logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("RSS_POLL_INTERVAL", "3600"))
ARTICLES_PER_POLL = int(os.getenv("ARTICLES_PER_POLL", "10"))
FETCH_FULL_CONTENT = os.getenv("RSS_FETCH_FULL_CONTENT", "true").lower() == "true"

# 单轮最多补正文的篇数，避免大号把一轮轮询拖太久
MAX_CONTENT_PER_ROUND = 20


class RSSPoller:
    """后台轮询单例"""

    _instance = None
    _task = None
    _running = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("RSS poller started (interval=%ds)", POLL_INTERVAL)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("RSS poller stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    async def _loop(self):
        while self._running:
            try:
                await self._poll_all()
            except Exception as e:
                logger.error("RSS poll cycle error: %s", e, exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)

    async def poll_now(self):
        """手动触发一次全量轮询"""
        await self._poll_all()

    # ── 采集 ─────────────────────────────────────────────

    async def _poll_one(self, fakeid: str, nickname: str = "") -> int:
        """采集单个公众号并入库，返回新增篇数。异常原样上抛，由调用方处理。"""
        async with WereadClient() as client:
            articles = await client.list_articles(
                fakeid, limit=ARTICLES_PER_POLL, nickname=nickname
            )
            if articles and FETCH_FULL_CONTENT:
                await self._fill_content(client, fakeid, articles)

        new_count = 0
        if articles:
            new_count = rss_store.save_articles(fakeid, articles, source="poll")
            if new_count > 0:
                logger.info("RSS: %d new articles for %s", new_count,
                            nickname or fakeid[:8])
        rss_store.update_last_poll(fakeid)
        return new_count

    async def _fill_content(self, client: WereadClient, fakeid: str,
                            articles: List[Dict]) -> int:
        """补正文。走微信读书正文接口，不碰 mp.weixin.qq.com，没有验证码风险。"""
        targets = [a for a in articles if not a.get("content")][:MAX_CONTENT_PER_ROUND]
        if len(articles) > MAX_CONTENT_PER_ROUND:
            logger.info("文章数 %d 篇超过限制，本轮只补最近 %d 篇正文",
                        len(articles), MAX_CONTENT_PER_ROUND)

        site_url = os.getenv("SITE_URL", "http://localhost:5000").rstrip("/")
        filled = 0
        for article in targets:
            review_id = article.get("review_id") or weread_client.review_id_from_article_url(
                article.get("link", ""), fakeid
            )
            if not review_id:
                continue
            try:
                result = await client.fetch_article_content(
                    review_id, proxy_base_url=site_url
                )
            except WereadError as e:
                logger.warning("[WeRead] 正文获取失败 %s: %s", review_id, e.message)
                if e.is_auth_error:
                    # 登录态失效，本轮剩下的也不用试了
                    break
                if e.code == "intercepted":
                    # 撞上风控验证页：这一轮再抓也是同样的拦截页，继续只会加重风控。
                    # 停手，等下一轮（届时正文仍为空，会自动重试）。
                    logger.warning("[WeRead] 触发微信风控验证页，本轮停止补正文")
                    break
                continue
            article["content"] = result.get("content", "")
            article["plain_content"] = result.get("plain_content", "")
            filled += 1

        if filled:
            logger.info("[WeRead] 补齐 %d 篇正文", filled)
        return filled

    def _blacklist(self, fakeid: str, reason: str, note: str):
        """把采不到的号加入黑名单，避免每轮都白打一遍请求。"""
        sub = rss_store.get_subscription(fakeid)
        nickname = sub.get("nickname", "") if sub else ""
        logger.warning("把 %s (%s) 加入黑名单: %s", fakeid[:8], nickname, reason)
        try:
            rss_store.add_to_blacklist(fakeid, nickname=nickname, reason=reason, note=note)
        except Exception as e:
            logger.warning("加入黑名单失败 %s: %s", fakeid[:8], e)

    async def fetch_now(self, fakeid: str) -> int:
        """立刻采集某一个公众号，返回新增篇数。

        订阅接口用它做「订阅后马上抓一次」—— 否则用户要等下一轮轮询
        （默认 1 小时）才看得到文章，界面上像是没生效。
        """
        if not weread_client.is_enabled():
            logger.warning("立即采集跳过 %s: 未配置微信读书登录态", fakeid[:8])
            return 0
        if rss_store.is_blacklisted(fakeid):
            logger.info("立即采集跳过 %s: 在黑名单里", fakeid[:8])
            return 0

        sub = rss_store.get_subscription(fakeid)
        nickname = (sub or {}).get("nickname", "")
        try:
            count = await self._poll_one(fakeid, nickname)
            logger.info("立即采集完成 %s: 新增 %d 篇", nickname or fakeid[:8], count)
            return count
        except Exception as e:
            logger.error("立即采集失败 %s: %s", fakeid[:8], e)
            return 0

    async def _poll_all(self):
        fakeids = rss_store.get_all_fakeids()
        if not fakeids:
            return

        if not weread_client.is_enabled():
            logger.warning(
                "RSS poll skipped: 未配置微信读书登录态"
                "（到管理页扫码登录微信读书，或设置 WEREAD_COOKIE）"
            )
            return

        # 昵称只用于日志和书架提示，一次查完，别每个号再查一遍库
        nicknames = rss_store.get_nickname_map()
        blacklisted = set(rss_store.get_active_blacklist_fakeids())
        active = [f for f in fakeids if f not in blacklisted]

        skipped = len(fakeids) - len(active)
        if skipped:
            logger.info("RSS poll: %d 个订阅（%d 个在黑名单，跳过）", len(fakeids), skipped)
        else:
            logger.info("RSS poll: 检查 %d 个订阅", len(fakeids))

        for fakeid in active:
            try:
                await self._poll_one(fakeid, nicknames.get(fakeid, ""))
            except WereadError as e:
                if e.code == "invalid_fakeid":
                    self._blacklist(fakeid, "invalid_fakeid",
                                    f"fakeid 无法换算成微信读书 bookId: {e.message}")
                else:
                    logger.error("RSS poll error for %s: %s", fakeid[:8], e.message)
            except Exception as e:
                logger.error("RSS poll error for %s: %s", fakeid[:8], e)
            await asyncio.sleep(3)


rss_poller = RSSPoller()
