#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端：公众号后台完全没登录时，整轮轮询靠微信读书把文章（含正文）落库。

这是本次修复要保证的核心行为 —— 后台凭证过期后 RSS 仍然更新。
"""

import pytest

import utils.article_fetcher as article_fetcher
from utils import rss_store
from utils import weread_client as wc
from utils.auth_manager import auth_manager
from utils.rss_poller import rss_poller
from utils.weread_client import WereadClient

FAKEID = "MzI5NjM4MjExMg=="
BOOK_ID = "MP_WXS_3296382112"


@pytest.fixture
def subscription():
    rss_store.add_subscription(FAKEID, nickname="测试公众号")
    yield FAKEID
    rss_store.remove_subscription(FAKEID)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=1; wr_skey=test")
    monkeypatch.setenv("WEREAD_CONTENT_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_PAGE_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_AUTO_ADD_SHELF", "false")
    monkeypatch.delenv("ARTICLE_SOURCE", raising=False)
    monkeypatch.delenv("WEREAD_ENABLED", raising=False)
    # 公众号后台完全没登录
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    wc.reset_shelf_cache()
    yield
    wc.reset_shelf_cache()


async def test_poll_cycle_lands_articles_with_content(subscription, monkeypatch):
    async def _must_not_scrape(*args, **kwargs):
        raise AssertionError("不该直抓 mp.weixin.qq.com")

    monkeypatch.setattr(article_fetcher, "fetch_articles_batch", _must_not_scrape)
    monkeypatch.setattr(rss_poller, "_http_client", None)
    # 轮询器每个号之间会 sleep 3s，测试里跳过
    monkeypatch.setattr("utils.rss_poller.asyncio.sleep", _noop_sleep)

    async def _request(self, method, path, *, params=None, json_data=None,
                       as_json=True, interval=0.0):
        if path == "/web/mp/articles":
            if int(params.get("offset", 0)) != 0:
                return {"reviews": []}
            return {"reviews": [{
                "createTime": 1778580000,
                "subReviews": [{"review": {
                    "reviewId": f"{BOOK_ID}_tok1",
                    "createTime": 1778580000,
                    "mpInfo": {"title": "读书渠道文章", "time": 1778580000,
                               "originalId": "tok1", "content": "摘要"},
                }}],
            }]}
        if path == "/web/mp/content":
            return '<div id="js_content"><p>这是通过微信读书拿到的正文</p></div>'
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(WereadClient, "_request", _request)

    await rss_poller._poll_all()

    stored = rss_store.get_articles(FAKEID, limit=10)
    assert len(stored) == 1
    assert stored[0]["title"] == "读书渠道文章"
    assert stored[0]["link"] == "https://mp.weixin.qq.com/s/tok1"
    assert "微信读书拿到的正文" in stored[0]["content"]
    assert "微信读书拿到的正文" in stored[0]["plain_content"]


async def test_poll_cycle_noop_without_any_channel(subscription, monkeypatch):
    """两条通道都没有时，轮询安静跳过，不该炸也不该写库。"""
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("不该发起任何采集请求")

    monkeypatch.setattr(WereadClient, "_request", _must_not_run)
    monkeypatch.setattr(article_fetcher, "fetch_articles_batch", _must_not_run)

    await rss_poller._poll_all()
    assert rss_store.get_articles(FAKEID, limit=10) == []


async def _noop_sleep(_seconds):
    return None
