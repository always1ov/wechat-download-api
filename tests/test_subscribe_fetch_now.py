#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订阅后立刻采集一次。

以前 /rss/subscribe 只写库，要等下一轮轮询（默认 3600 秒）才抓文章 ——
用户订阅完去看是空的，看起来就像「拉不到文章」。
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from utils import rss_store
from utils import weread_client as wc
from utils.rss_poller import rss_poller
from utils.weread_client import WereadClient

FAKEID = "MzI5NjM4MjExMg=="
BOOK_ID = "MP_WXS_3296382112"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=42; wr_skey=k; wr_rt=web%40rt")
    monkeypatch.setenv("WEREAD_PAGE_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_CONTENT_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_AUTO_ADD_SHELF", "false")
    monkeypatch.setenv("WEREAD_AUTO_RENEW", "false")
    monkeypatch.setenv("RSS_FETCH_FULL_CONTENT", "false")
    wc.reset_shelf_cache()
    rss_store.add_subscription(FAKEID, nickname="测试号")
    yield
    rss_store.remove_subscription(FAKEID)
    wc.reset_shelf_cache()


def install_weread(monkeypatch, titles=("新文章",)):
    def handler(request):
        p = request.url.path
        if p == "/web/mp/articles":
            if int(request.url.params.get("offset", 0)) != 0:
                return httpx.Response(200, json={"reviews": []})
            return httpx.Response(200, json={"reviews": [
                {"createTime": 1778580000, "subReviews": [{"review": {
                    "reviewId": f"{BOOK_ID}_{t}",
                    "createTime": 1778580000,
                    "mpInfo": {"title": t, "time": 1778580000, "originalId": t},
                }}]} for t in titles
            ]})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        WereadClient, "_new_client",
        lambda self: httpx.AsyncClient(transport=transport, follow_redirects=True),
    )


async def test_fetch_now_pulls_and_saves(monkeypatch):
    install_weread(monkeypatch)
    assert rss_store.get_articles(FAKEID, limit=10) == []

    count = await rss_poller.fetch_now(FAKEID)

    assert count == 1
    stored = rss_store.get_articles(FAKEID, limit=10)
    assert [a["title"] for a in stored] == ["新文章"]


async def test_fetch_now_skips_blacklisted(monkeypatch):
    def boom(request):
        pytest.fail("黑名单里的号不该发起采集请求")

    monkeypatch.setattr(
        WereadClient, "_new_client",
        lambda self: httpx.AsyncClient(transport=httpx.MockTransport(boom)),
    )
    rss_store.add_to_blacklist(FAKEID, reason="test")
    try:
        assert await rss_poller.fetch_now(FAKEID) == 0
    finally:
        rss_store.remove_from_blacklist(FAKEID)


async def test_fetch_now_without_any_channel(monkeypatch):
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    assert await rss_poller.fetch_now(FAKEID) == 0


async def test_fetch_now_swallows_errors(monkeypatch):
    """立即采集是订阅的附带动作，失败不该把订阅请求带崩。"""
    def handler(request):
        return httpx.Response(500, text="boom")

    monkeypatch.setattr(
        WereadClient, "_new_client",
        lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await rss_poller.fetch_now(FAKEID) == 0


def test_subscribe_triggers_immediate_fetch(monkeypatch):
    """订阅接口必须触发一次采集，否则用户要干等一小时。"""
    called = []

    async def fake_fetch(fakeid):
        called.append(fakeid)
        return 3

    monkeypatch.setattr(rss_poller, "fetch_now", fake_fetch)

    import app
    with TestClient(app.app) as c:
        r = c.post("/api/rss/subscribe", json={"fakeid": FAKEID, "nickname": "测试号"})

    assert r.json()["success"] is True
    assert "拉取" in r.json()["message"]
    assert called == [FAKEID], "BackgroundTasks 应该调用了 fetch_now"
