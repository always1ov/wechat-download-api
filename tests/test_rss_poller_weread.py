#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轮询器行为（只走微信读书）。"""

import httpx
import pytest

from utils import rss_store
from utils import weread_client as wc
from utils.rss_poller import rss_poller
from utils.weread_client import WereadClient, WereadError

FAKEID = "MzI5NjM4MjExMg=="
BOOK_ID = "MP_WXS_3296382112"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=1; wr_skey=test; wr_rt=web%40rt")
    monkeypatch.setenv("WEREAD_CONTENT_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_PAGE_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_AUTO_ADD_SHELF", "false")
    monkeypatch.setenv("WEREAD_AUTO_RENEW", "false")
    wc.reset_shelf_cache()
    rss_store.add_subscription(FAKEID, nickname="测试号")
    yield
    rss_store.remove_subscription(FAKEID)
    wc.reset_shelf_cache()


async def _noop_sleep(_seconds):
    return None


def install(monkeypatch, *, articles=("tok1",), content="", list_error=None,
            content_error=False):
    calls = []

    async def _request(self, method, path, *, params=None, json_data=None,
                       as_json=True, interval=0.0, app=False):
        calls.append(path)
        if path == "/web/mp/articles":
            if list_error is not None:
                raise list_error
            if int(params.get("offset", 0)) != 0:
                return {"reviews": []}
            return {"reviews": [
                {"createTime": 1778580000, "subReviews": [{"review": {
                    "reviewId": f"{BOOK_ID}_{t}",
                    "createTime": 1778580000,
                    "mpInfo": {"title": t, "time": 1778580000, "originalId": t},
                }}]} for t in articles
            ]}
        if path == "/web/mp/content":
            if content_error:
                raise WereadError(-1, "content boom")
            return content or '<div id="js_content"><p>正文</p></div>'
        if path == "/api/mp/cover":
            raise WereadError("empty_cover", "no cover", retriable=False)
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(WereadClient, "_request", _request)
    return calls


async def test_poll_one_saves_articles_with_content(monkeypatch):
    install(monkeypatch, articles=("tok1", "tok2"))

    count = await rss_poller._poll_one(FAKEID, "测试号")

    assert count == 2
    stored = rss_store.get_articles(FAKEID, limit=10)
    assert {a["title"] for a in stored} == {"tok1", "tok2"}
    assert all("正文" in a["content"] for a in stored)


async def test_content_failure_still_saves_metadata(monkeypatch):
    """正文抓不到时文章元数据仍要入库 —— RSS 里至少能点开原文链接。"""
    install(monkeypatch, content_error=True)

    count = await rss_poller._poll_one(FAKEID, "测试号")

    assert count == 1
    stored = rss_store.get_articles(FAKEID, limit=10)
    assert stored[0]["title"] == "tok1"
    assert stored[0]["content"] == ""


async def test_auth_error_stops_fetching_rest_of_content(monkeypatch):
    """登录态失效后本轮剩下的正文不必再试，省得白打一串请求。"""
    calls = []

    async def _request(self, method, path, *, params=None, json_data=None,
                       as_json=True, interval=0.0, app=False):
        calls.append(path)
        if path == "/web/mp/articles":
            if int(params.get("offset", 0)) != 0:
                return {"reviews": []}
            return {"reviews": [
                {"createTime": 1, "subReviews": [{"review": {
                    "reviewId": f"{BOOK_ID}_{t}", "createTime": 1,
                    "mpInfo": {"title": t, "time": 1, "originalId": t},
                }}]} for t in ("a", "b", "c")
            ]}
        raise WereadError(-2012, "login timeout", retriable=False)

    monkeypatch.setattr(WereadClient, "_request", _request)
    await rss_poller._poll_one(FAKEID, "测试号")

    assert calls.count("/web/mp/content") == 1, "第一次就该判定登录失效并停手"


async def test_fetch_now_requires_login(monkeypatch):
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("未登录时不该发请求")

    monkeypatch.setattr(WereadClient, "_request", _must_not_run)
    assert await rss_poller.fetch_now(FAKEID) == 0


async def test_fetch_now_skips_blacklisted(monkeypatch):
    async def _must_not_run(*args, **kwargs):
        raise AssertionError("黑名单号不该发请求")

    monkeypatch.setattr(WereadClient, "_request", _must_not_run)
    rss_store.add_to_blacklist(FAKEID, reason="test")
    try:
        assert await rss_poller.fetch_now(FAKEID) == 0
    finally:
        rss_store.remove_from_blacklist(FAKEID)


async def test_fetch_now_swallows_errors(monkeypatch):
    """立即采集是订阅的附带动作，失败不该把订阅请求带崩。"""
    install(monkeypatch, list_error=WereadError(-1, "boom"))
    assert await rss_poller.fetch_now(FAKEID) == 0


async def test_poll_all_blacklists_unconvertible_fakeid(monkeypatch):
    """fakeid 换不出 bookId 的号每轮都注定失败，拉黑省得白打。"""
    monkeypatch.setattr("utils.rss_poller.asyncio.sleep", _noop_sleep)
    rss_store.add_subscription("不是合法fakeid", nickname="坏号")
    try:
        install(monkeypatch)
        await rss_poller._poll_all()
        assert rss_store.is_blacklisted("不是合法fakeid")
    finally:
        rss_store.remove_from_blacklist("不是合法fakeid")
        rss_store.remove_subscription("不是合法fakeid")
