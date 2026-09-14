#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""i 域（i.weread.qq.com，微信读书 App 接口）支持。

i 域比网页域多一个关键能力：能搜公众号。代价是它只认 App 的 UA 和已续期的
wr_skey，拿不准能不能用 —— 所以全部挂在回退结构下，不通就退回网页域。
"""

import httpx
import pytest

from utils import weread_client as wc
from utils.weread_client import WereadClient, WereadError

BOOK_ID = "MP_WXS_3296382112"
FAKEID = "MzI5NjM4MjExMg=="


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=42; wr_skey=k; wr_rt=web%40rt")
    monkeypatch.setenv("WEREAD_PAGE_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_CONTENT_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_AUTO_ADD_SHELF", "false")
    monkeypatch.delenv("WEREAD_APP_API", raising=False)
    monkeypatch.delenv("WEREAD_AUTO_RENEW", raising=False)
    wc.reset_shelf_cache()
    yield
    wc.reset_shelf_cache()


def install(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        WereadClient, "_new_client",
        lambda self: httpx.AsyncClient(transport=transport, follow_redirects=True),
    )


# ── 解析 /store/search ────────────────────────────────────

def test_parse_store_search_keeps_only_official_accounts():
    """搜索结果里书和公众号混在一起，只有 MP_WXS_* 是公众号。"""
    payload = {"books": [
        {"bookInfo": {"bookId": BOOK_ID, "title": "某公众号",
                      "author": "作者", "cover": "https://x/c.jpg"}},
        {"bookInfo": {"bookId": "3300060", "title": "一本普通电子书"}},
    ]}
    out = wc.parse_store_search(payload)
    assert len(out) == 1
    assert out[0]["fakeid"] == FAKEID          # bookId 换算回 fakeid
    assert out[0]["book_id"] == BOOK_ID
    assert out[0]["nickname"] == "某公众号"
    assert out[0]["round_head_img"] == "https://x/c.jpg"
    assert out[0]["source"] == "weread"


def test_parse_store_search_accepts_unwrapped_items():
    out = wc.parse_store_search({"books": [{"bookId": BOOK_ID, "title": "直接平铺"}]})
    assert out[0]["nickname"] == "直接平铺"


def test_parse_store_search_dedupes():
    out = wc.parse_store_search({"books": [
        {"bookId": BOOK_ID, "title": "A"}, {"bookId": BOOK_ID, "title": "A"}]})
    assert len(out) == 1


def test_parse_store_search_handles_unknown_shape():
    assert wc.parse_store_search({"nothing": 1}) == []
    assert wc.parse_store_search("not a dict") == []


def test_parse_store_search_raises_on_auth_error():
    with pytest.raises(WereadError) as exc:
        wc.parse_store_search({"errCode": -2012, "errMsg": "login timeout"})
    assert exc.value.is_auth_error


# ── 解析 /book/articles ───────────────────────────────────

def test_parse_app_articles_handles_flat_list():
    payload = {"articles": [
        {"reviewId": f"{BOOK_ID}_tok1", "mpInfo": {"title": "平铺文章", "time": 100}}]}
    articles, count = wc.parse_app_articles(payload, BOOK_ID)
    assert count == 1
    assert articles[0]["title"] == "平铺文章"
    assert articles[0]["link"] == "https://mp.weixin.qq.com/s/tok1"


def test_parse_app_articles_reuses_web_parser_for_grouped_shape():
    payload = {"reviews": [{"createTime": 5, "subReviews": [
        {"review": {"reviewId": f"{BOOK_ID}_t", "mpInfo": {"title": "分组形态"}}}]}]}
    articles, _ = wc.parse_app_articles(payload, BOOK_ID)
    assert articles[0]["title"] == "分组形态"


# ── 请求形态 ──────────────────────────────────────────────

async def test_app_requests_use_app_host_and_ua(monkeypatch):
    """i 域按 App 客户端校验：必须用 App 的 UA，且不带浏览器的 Origin/Referer。"""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["ua"] = request.headers.get("User-Agent", "")
        seen["origin"] = request.headers.get("Origin")
        seen["referer"] = request.headers.get("Referer")
        return httpx.Response(200, json={"books": []})

    install(monkeypatch, handler)
    async with WereadClient() as client:
        await client.search_mp_accounts("测试")

    assert seen["url"].startswith("https://i.weread.qq.com/store/search")
    assert seen["ua"].startswith("WeRead/")
    assert seen["origin"] is None
    assert seen["referer"] is None


async def test_web_requests_keep_browser_headers(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["ua"] = request.headers.get("User-Agent", "")
        seen["referer"] = request.headers.get("Referer")
        return httpx.Response(200, json={"reviews": []})

    install(monkeypatch, handler)
    async with WereadClient() as client:
        await client.get_articles_page(BOOK_ID)

    assert seen["url"].startswith("https://weread.qq.com/web/mp/articles")
    assert seen["ua"].startswith("Mozilla/")
    assert seen["referer"] == "https://weread.qq.com/"


async def test_search_sends_zlzchat_query_params(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"books": []})

    install(monkeypatch, handler)
    async with WereadClient() as client:
        await client.search_mp_accounts("人民日报", count=15)

    assert seen == {"v": "2", "scope": "2", "count": "15", "type": "0",
                    "keyword": "人民日报"}


async def test_app_article_list_params(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"articles": []})

    install(monkeypatch, handler)
    async with WereadClient() as client:
        await client.get_app_articles_page(BOOK_ID, offset=20, count=20)

    assert seen == {"bookId": BOOK_ID, "offset": "20", "count": "20", "synckey": "0"}


# ── 不可用时的退让 ────────────────────────────────────────

async def test_401_marks_app_domain_unusable(monkeypatch):
    """i 域对未续期 skey 直接 401；标记冷却，别每次请求都去撞。"""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(401, text="unauthorized")

    install(monkeypatch, handler)
    async with WereadClient() as client:
        with pytest.raises(WereadError):
            await client.search_mp_accounts("x")
        assert wc.app_domain_usable() is False
        # 冷却期内直接短路，不再发请求
        with pytest.raises(WereadError) as exc:
            await client.search_mp_accounts("x")

    assert exc.value.code == "app_domain_unavailable"
    assert len(calls) == 1


async def test_app_api_can_be_disabled(monkeypatch):
    monkeypatch.setenv("WEREAD_APP_API", "false")
    install(monkeypatch, lambda r: pytest.fail("不该发起 i 域请求"))

    async with WereadClient() as client:
        with pytest.raises(WereadError) as exc:
            await client.search_mp_accounts("x")
    assert exc.value.code == "app_domain_unavailable"


async def test_list_articles_uses_app_domain_when_web_list_blocked(monkeypatch):
    """网页域列表被风控 → 试 i 域 → 都不行才退 cover。"""
    def handler(request):
        path = request.url.path
        if path == "/web/mp/articles":
            return httpx.Response(200, json={"errCode": -2041, "errMsg": "blocked"})
        if path == "/book/articles":
            return httpx.Response(200, json={"articles": [
                {"reviewId": f"{BOOK_ID}_tok1", "mpInfo": {"title": "i 域取到的"}}]})
        return httpx.Response(404, json={})

    install(monkeypatch, handler)
    monkeypatch.setenv("WEREAD_AUTO_RENEW", "false")
    async with WereadClient() as client:
        articles = await client.list_articles(FAKEID, limit=5)

    assert [a["title"] for a in articles] == ["i 域取到的"]


async def test_cover_fallback_still_works_when_app_domain_dead(monkeypatch):
    """i 域挂掉不能连累原有的 cover 兜底。"""
    def handler(request):
        path = request.url.path
        if path == "/web/mp/articles":
            return httpx.Response(200, json={"errCode": -2041, "errMsg": "blocked"})
        if path == "/book/articles":
            return httpx.Response(500, text="boom")
        if path == "/api/mp/cover":
            return httpx.Response(200, json={"reviewId": f"{BOOK_ID}_tok", "title": "最新"})
        return httpx.Response(404, json={})

    install(monkeypatch, handler)
    monkeypatch.setenv("WEREAD_AUTO_RENEW", "false")
    async with WereadClient() as client:
        articles = await client.list_articles(FAKEID, limit=5)

    assert [a["title"] for a in articles] == ["最新"]
