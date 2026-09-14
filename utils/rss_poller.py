#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
RSS 后台轮询器
定时拉取订阅号的最新文章列表并缓存到 SQLite。

两条取数通道：
1. 公众号后台 appmsgpublish —— 元数据全、但凭证约 4 天过期且有频率风控；
2. 微信读书 Web（utils/weread_client）—— 独立登录态，后台挂了也能继续采集。

默认 ARTICLE_SOURCE=auto：后台优先，后台不可用时自动切到微信读书；
正文同理，直抓 mp.weixin.qq.com 触发验证码时用微信读书正文接口兜底。
"""

import asyncio
import json
import logging
import os
from typing import List, Dict, Optional

import httpx

from utils.auth_manager import auth_manager
from utils import rss_store
from utils.helpers import extract_article_info, parse_article_url, is_image_text_message, has_article_content, is_article_unavailable, get_unavailable_reason
from utils.http_client import fetch_page
from utils import weread_client
from utils.weread_client import WereadClient, WereadError

logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("RSS_POLL_INTERVAL", "3600"))
ARTICLES_PER_POLL = int(os.getenv("ARTICLES_PER_POLL", "10"))
FETCH_FULL_CONTENT = os.getenv("RSS_FETCH_FULL_CONTENT", "true").lower() == "true"


class WechatInvalidFakeidError(Exception):
    """
    [2026-05-18] 公众号在微信侧已失效（已注销/改名/重新注册）

    触发条件：appmsgpublish 接口返回 ret=200002 且 err_msg="invalid args"
    实测：任何 token+cookie 都无法访问，需要标记为永久失效
    """
    pass


class MpChannelError(Exception):
    """
    公众号后台通道本次不可用（登录过期、频率风控、接口报错等）。

    与 WechatInvalidFakeidError 的区别：这是**暂时**的，换条通道（微信读书）
    或等一会儿就能恢复，不该把公众号拉黑。
    """
    pass


class RSSPoller:
    """后台轮询单例"""

    _instance = None
    _task: Optional[asyncio.Task] = None
    _running = False
    # [2026-05-15 OS-4] 共享 httpx.AsyncClient 避免每轮每 fakeid 都新建（省 DNS+TLS 握手）
    _http_client: Optional[httpx.AsyncClient] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def start(self):
        if self._running:
            return
        self._running = True
        # 创建长连接 client，连接池 + keep-alive 自动复用
        self._http_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
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
        # 关闭共享 client
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
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

    async def _poll_all(self):
        fakeids = rss_store.get_all_fakeids()
        if not fakeids:
            return

        creds = auth_manager.get_credentials()
        mp_ready = bool(creds and creds.get("token") and creds.get("cookie"))
        weread_ready = weread_client.is_enabled()

        # ARTICLE_SOURCE 可以把通道钉死在某一条上，默认 auto（后台优先、读书兜底）
        source = weread_client.article_source()
        if source == "weread":
            mp_ready = False
        elif source == "mp":
            weread_ready = False

        if not mp_ready and not weread_ready:
            logger.warning(
                "RSS poll skipped: 公众号后台未登录，且微信读书通道未启用"
                "（配置 WEREAD_COOKIE 可在后台失效时继续采集）"
            )
            return
        if not mp_ready:
            logger.info("RSS poll: 公众号后台不可用，本轮全部走微信读书通道")

        # 昵称只用于日志和书架提示，一次查完，别每个号再查一遍库
        nicknames = rss_store.get_nickname_map()

        # 获取活跃黑名单
        blacklisted = set(rss_store.get_active_blacklist_fakeids())
        
        # 过滤掉黑名单中的公众号
        active_fakeids = [f for f in fakeids if f not in blacklisted]
        skipped = len(fakeids) - len(active_fakeids)
        
        if skipped > 0:
            logger.info("RSS poll: %d subscriptions (%d blacklisted, skipped)", 
                       len(fakeids), skipped)
        else:
            logger.info("RSS poll: checking %d subscriptions", len(fakeids))

        for fakeid in active_fakeids:
            try:
                articles = await self._fetch_article_list(
                    fakeid, creds,
                    use_mp=mp_ready,
                    use_weread=weread_ready,
                    nickname=nicknames.get(fakeid, ""),
                )
                if articles and FETCH_FULL_CONTENT:
                    # 获取完整文章内容
                    articles = await self._enrich_articles_content(fakeid, articles)

                if articles:
                    # 轮询器拉取的文章标记为 'poll'
                    new_count = rss_store.save_articles(fakeid, articles, source='poll')
                    if new_count > 0:
                        logger.info("RSS: %d new articles for %s", new_count, fakeid[:8])
                rss_store.update_last_poll(fakeid)
            except WechatInvalidFakeidError as e:
                # [2026-05-18] 同步 SaaS 修复：fakeid 在微信侧已失效，自动加入黑名单
                # 取该 fakeid 的 nickname（如果数据库里有）便于后续运维查看
                sub = rss_store.get_subscription(fakeid)
                nickname = sub.get("nickname", "") if sub else ""
                logger.warning("Fakeid %s (%s) is invalid on WeChat, adding to blacklist", fakeid[:8], nickname)
                try:
                    rss_store.add_to_blacklist(
                        fakeid, nickname=nickname, reason="invalid_fakeid",
                        note="[2026-05-18] 微信侧返回 invalid args，fakeid 已失效（注销/改名/重新注册）",
                    )
                except Exception as bl_err:
                    logger.warning("Failed to blacklist invalid fakeid %s: %s", fakeid[:8], bl_err)
            except Exception as e:
                logger.error("RSS poll error for %s: %s", fakeid[:8], e)
            await asyncio.sleep(3)

    async def _fetch_article_list(self, fakeid: str, creds: Dict, *,
                                  use_mp: bool = True, use_weread: bool = False,
                                  nickname: str = "") -> List[Dict]:
        """取文章列表：公众号后台优先，后台不可用时回退微信读书。

        只在后台**明确报错**（登录过期、频率风控、接口异常）时回退；后台正常
        返回空列表说明这个号本来就没发文，不需要多打一次微信读书。
        """
        invalid_fakeid_error = None

        if use_mp:
            try:
                return await self._fetch_from_mp(fakeid, creds)
            except WechatInvalidFakeidError as e:
                if not use_weread:
                    raise
                # 后台说这个号没了，但微信读书可能还读得到 —— 读书也拿不到才拉黑
                invalid_fakeid_error = e
            except MpChannelError as e:
                if not use_weread:
                    return []  # 保持旧行为：后台软失败不打断整轮轮询
                logger.info("[%s] 公众号后台不可用（%s），改用微信读书通道", fakeid[:8], e)
            except Exception as e:
                if not use_weread:
                    raise
                logger.info("[%s] 公众号后台请求异常（%s），改用微信读书通道", fakeid[:8], e)

        if not use_weread:
            return []

        try:
            articles = await self._fetch_from_weread(fakeid, nickname)
        except WereadError as e:
            if invalid_fakeid_error is not None:
                raise invalid_fakeid_error
            logger.warning("[%s] 微信读书通道失败: %s", fakeid[:8], e.user_message)
            return []

        # 后台判定失效、微信读书也一篇都拿不到 → 确认是死号，交给调用方拉黑
        if invalid_fakeid_error is not None and not articles:
            raise invalid_fakeid_error
        return articles

    async def _fetch_from_weread(self, fakeid: str, nickname: str = "") -> List[Dict]:
        """通过微信读书 Web 拉取文章列表（含自动加入书架）。"""
        async with WereadClient() as client:
            articles = await client.list_articles(
                fakeid, limit=ARTICLES_PER_POLL, nickname=nickname
            )
        if articles:
            logger.info("[WeRead] %s 取到 %d 篇文章", nickname or fakeid[:8], len(articles))
        return articles

    async def _fetch_from_mp(self, fakeid: str, creds: Dict) -> List[Dict]:
        params = {
            "sub": "list",
            "search_field": "null",
            "begin": 0,
            "count": ARTICLES_PER_POLL,
            "query": "",
            "fakeid": fakeid,
            "type": "101_1",
            "free_publish_type": 1,
            "sub_action": "list_ex",
            "token": creds["token"],
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://mp.weixin.qq.com/",
            "Cookie": creds["cookie"],
        }

        # [2026-05-15 OS-4] 使用共享 client，省 DNS+TLS 握手
        # 兜底：若 client 未初始化（理论不会发生），退回到每次新建
        if self._http_client is not None:
            resp = await self._http_client.get(
                "https://mp.weixin.qq.com/cgi-bin/appmsgpublish",
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            result = resp.json()
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    "https://mp.weixin.qq.com/cgi-bin/appmsgpublish",
                    params=params,
                    headers=headers,
                )
                resp.raise_for_status()
                result = resp.json()

        base_resp = result.get("base_resp", {})
        if base_resp.get("ret") != 0:
            ret_code = base_resp.get("ret")
            err_msg = base_resp.get("err_msg", "")
            logger.warning("WeChat API error for %s: ret=%s err_msg=%r",
                           fakeid[:8], ret_code, err_msg)
            # [2026-05-18] 同步 SaaS 修复：ret=200002 + "invalid args" → fakeid 已失效
            # 老代码统一返回空 → 静默失败，用户感受不到该号已注销
            # 现在：抛 WechatInvalidFakeidError 让调用方加入黑名单
            if ret_code == 200002 and "invalid arg" in err_msg.lower():
                raise WechatInvalidFakeidError(
                    f"fakeid {fakeid[:8]} 已失效（注销/改名）: {err_msg}"
                )
            raise MpChannelError(f"ret={ret_code} err_msg={err_msg}")

        publish_page = result.get("publish_page", {})
        if isinstance(publish_page, str):
            try:
                publish_page = json.loads(publish_page)
            except (json.JSONDecodeError, ValueError):
                return []

        if not isinstance(publish_page, dict):
            return []

        articles = []
        for item in publish_page.get("publish_list", []):
            publish_info = item.get("publish_info", {})
            if isinstance(publish_info, str):
                try:
                    publish_info = json.loads(publish_info)
                except (json.JSONDecodeError, ValueError):
                    continue
            if not isinstance(publish_info, dict):
                continue
            for a in publish_info.get("appmsgex", []):
                articles.append({
                    "aid": a.get("aid", ""),
                    "title": a.get("title", ""),
                    "link": a.get("link", ""),
                    "digest": a.get("digest", ""),
                    "cover": a.get("cover", ""),
                    "author": a.get("author", ""),
                    "publish_time": a.get("update_time", 0),
                })
        return articles

    async def poll_now(self):
        """手动触发一次轮询"""
        await self._poll_all()
    
    async def _enrich_via_mp(self, fakeid: str, articles: List[Dict]) -> None:
        """直抓 mp.weixin.qq.com 补全正文（原地写回 article["content"]）。

        微信对文章页有风控：命中时返回验证码页，这里只记录不写 content，
        由 _enrich_articles_content 交给微信读书正文接口兜底。
        """
        from utils.article_fetcher import fetch_articles_batch
        from utils.content_processor import process_article_content

        article_links = [a.get("link", "") for a in articles if a.get("link")]
        if not article_links:
            return

        logger.info("开始批量获取 %d 篇文章的完整内容", len(article_links))
        
        # 获取微信凭证（从环境变量读取）
        wechat_token = os.getenv("WECHAT_TOKEN", "")
        wechat_cookie = os.getenv("WECHAT_COOKIE", "")
        
        results = await fetch_articles_batch(
            article_links, 
            max_concurrency=3, 
            timeout=60,
            wechat_token=wechat_token,
            wechat_cookie=wechat_cookie
        )
        
        for article in articles:
            link = article.get("link", "")
            if not link:
                continue
            
            html = results.get(link)
            if not html:
                logger.warning("Empty HTML: %s", link[:80])
                continue
            
            # [2026-05-18] 精确化验证码检测（之前用 "验证" 二字误伤大量正文含此字的文章）
            # 微信风控页特有标记：
            #   1. verifycode 出现在 URL/form/script 中（最强信号）
            #   2. "请输入图片中的字符" — 微信原版验证码提示文案
            #   3. "环境异常" — 微信明确风控提示（保留原检测）
            # 移除单纯的"验证"二字判断 — 文章正文里出现概率高，会导致 content 丢失
            html_lower = html.lower()
            verification_markers = (
                "verifycode" in html_lower
                or "请输入图片中的字符" in html
                or "环境异常" in html
            )
            if verification_markers:
                sub = rss_store.get_subscription(fakeid)
                nickname = sub.get("nickname", "") if sub else ""
                count = rss_store.increment_verification_count(fakeid, nickname)
                logger.warning("Verification triggered for %s (count=%d): %s",
                             fakeid[:8], count, link[:60])
                continue
            
            if is_article_unavailable(html):
                reason = get_unavailable_reason(html) or "unknown"
                logger.warning("Article permanently unavailable (%s): %s", reason, link[:80])
                article["content"] = f"<p>[unavailable] {reason}</p>"
                article["plain_content"] = f"[unavailable] {reason}"
                continue
            if not has_article_content(html):
                logger.warning("No content in HTML: %s", link[:80])
                continue
            
            try:
                # 使用 content_processor 处理文章内容（完美保持图文顺序）
                # 从环境变量读取网站URL,入库时代理图片(与SaaS版策略一致)
                site_url = os.getenv("SITE_URL", "http://localhost:5000").rstrip("/")
                result = process_article_content(html, proxy_base_url=site_url)
                
                # 合并到原文章数据
                article["content"] = result.get("content", "")
                article["plain_content"] = result.get("plain_content", "")
                
                # 如果原始数据没有作者，从 HTML 中提取
                if not article.get("author"):
                    from utils.helpers import extract_article_info, parse_article_url
                    article_info = extract_article_info(html, parse_article_url(link))
                    article["author"] = article_info.get("author", "")
                
                logger.info("Content fetched: %s... (%d chars, %d images)",
                           link[:50],
                           len(article["content"]), 
                           len(result.get("images", [])))
            except Exception as e:
                logger.error("Failed to process content for %s: %s", link[:80], str(e))

    async def _fill_content_via_weread(self, fakeid: str, articles: List[Dict]) -> int:
        """用微信读书正文接口补全正文，返回成功篇数。

        reviewId 优先用列表接口带回来的；公众号后台来的文章没有 reviewId，
        用 <bookId>_<短链 token> 现推一个（bookId 由 fakeid base64 解码得到）。
        """
        if not articles:
            return 0

        site_url = os.getenv("SITE_URL", "http://localhost:5000").rstrip("/")
        filled = 0
        try:
            async with WereadClient() as client:
                for article in articles:
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
                            # Cookie 失效，本轮剩下的也不用试了
                            break
                        continue
                    article["content"] = result.get("content", "")
                    article["plain_content"] = result.get("plain_content", "")
                    filled += 1
        except WereadError as e:
            logger.warning("[WeRead] 正文通道不可用: %s", e.user_message)

        if filled:
            logger.info("[WeRead] 通过微信读书补齐 %d 篇正文", filled)
        return filled

    async def _enrich_articles_content(self, fakeid: str, articles: List[Dict]) -> List[Dict]:
        """
        批量补全文章正文。

        限制：最多获取 20 篇（避免大量文章导致轮询过久）。

        取正文的顺序：
        1. 微信读书列表来的文章 → 直接用微信读书正文接口（不碰 mp.weixin.qq.com，无验证码风险）
        2. 其余文章 → 原有的直抓流程
        3. 直抓被验证码/风控挡下的 → 再用微信读书兜一次底
        """
        if not articles:
            return articles

        max_fetch = 20
        if len(articles) > max_fetch:
            logger.info("文章数 %d 篇超过限制，仅获取最近 %d 篇的完整内容",
                        len(articles), max_fetch)
            articles = articles[:max_fetch]

        weread_ready = weread_client.is_enabled()

        if weread_ready:
            await self._fill_content_via_weread(
                fakeid,
                [a for a in articles if a.get("review_id") and not a.get("content")],
            )

        pending = [a for a in articles if not a.get("content")]
        if pending:
            await self._enrich_via_mp(fakeid, pending)

        if weread_ready:
            missing = [a for a in articles if not a.get("content")]
            if missing:
                logger.info("直抓未拿到正文 %d 篇，改用微信读书正文接口兜底", len(missing))
                await self._fill_content_via_weread(fakeid, missing)

        return articles


rss_poller = RSSPoller()
