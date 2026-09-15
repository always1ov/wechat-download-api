#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从微信读书书架找公众号 —— 不依赖公众号后台的入口。

没有公众号的人搜不了 fakeid，也就订阅不了。书架这条路用的是登录态校验
同一个接口（/web/shelf/sync），只要扫码有效就能出结果。
"""

import httpx
import pytest
from fastapi.testclient import TestClient

import routes.search as search_mod
from utils import rss_store
from utils import weread_client as wc
from utils.rss_poller import rss_poller
from utils.weread_client import WereadClient

BOOK_A, FAKEID_A = "MP_WXS_3296382112", "MzI5NjM4MjExMg=="
BOOK_B, FAKEID_B = "MP_WXS_3012726261", "MzAxMjcyNjI2MQ=="

SHELF = {"synckey": 1, "books": [
    {"bookId": BOOK_A, "title": "某公众号", "author": "作者A", "cover": "https://x/a.jpg"},
    {"bookId": BOOK_B, "title": "另一个号", "cover": "https://x/b.jpg"},
    {"bookId": "3300060", "title": "一本电子书", "author": "某作家"},
]}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=42; wr_skey=k; wr_rt=web%40rt")
    monkeypatch.setenv("WEREAD_PAGE_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_AUTO_RENEW", "false")
    wc.reset_shelf_cache()
    yield
    for f in (FAKEID_A, FAKEID_B):
        rss_store.remove_subscription(f)
    wc.reset_shelf_cache()


def install(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        WereadClient, "_new_client",
        lambda self: httpx.AsyncClient(transport=transport, follow_redirects=True),
    )


def shelf_handler(request):
    if request.url.path == "/web/shelf/sync":
        return httpx.Response(200, json=SHELF)
    return httpx.Response(404, json={})


# ── 解析 ──────────────────────────────────────────────────

def test_parse_shelf_accounts_filters_out_ebooks():
    """书架上书和公众号混在一起，只有 MP_WXS_* 是公众号。"""
    out = wc.parse_shelf_accounts(SHELF)
    assert [a["nickname"] for a in out] == ["某公众号", "另一个号"]
    assert out[0]["fakeid"] == FAKEID_A
    assert out[0]["source"] == "weread_shelf"


def test_parse_shelf_accounts_tolerates_empty():
    assert wc.parse_shelf_accounts({}) == []
    assert wc.parse_shelf_accounts({"books": []}) == []
    assert wc.parse_shelf_accounts("nope") == []


def test_parse_shelf_accounts_raises_on_auth_error():
    with pytest.raises(wc.WereadError):
        wc.parse_shelf_accounts({"errCode": -2012, "errMsg": "login timeout"})


async def test_shelf_listing_uses_empty_user_vid(monkeypatch):
    """userVid 必须传空字符串，传真实 vid 反而触发 -2012。"""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=SHELF)

    install(monkeypatch, handler)
    async with WereadClient() as client:
        await client.get_shelf_accounts()

    assert seen["userVid"] == ""


async def test_shelf_listing_marks_accounts_on_shelf(monkeypatch):
    """书架上列出来的号按定义就在书架上，不该再去查/加一遍。"""
    install(monkeypatch, shelf_handler)
    async with WereadClient() as client:
        await client.get_shelf_accounts()
        ok, detail = await client.ensure_on_shelf(BOOK_A, "某公众号")

    assert ok is True
    assert "缓存" in detail


# ── 搜索回退到书架 ────────────────────────────────────────

async def test_search_falls_back_to_shelf_when_store_search_dead(monkeypatch):
    """没有公众号后台、i 域又不通时，至少能从书架里找到已关注的号。"""

    def handler(request):
        if request.url.path == "/store/search":
            return httpx.Response(401, text="unauthorized")
        return shelf_handler(request)

    install(monkeypatch, handler)

    accounts, err = await search_mod.searchbiz_raw("另一个")

    assert err is None
    assert [a["nickname"] for a in accounts] == ["另一个号"]


async def test_shelf_search_matches_nothing_returns_empty(monkeypatch):

    def handler(request):
        if request.url.path == "/store/search":
            return httpx.Response(401, text="unauthorized")
        return shelf_handler(request)

    install(monkeypatch, handler)

    accounts, err = await search_mod.searchbiz_raw("压根不存在的号")
    assert accounts == []
    assert err is None


# ── 接口 ──────────────────────────────────────────────────

@pytest.fixture
def client():
    import app
    with TestClient(app.app) as c:
        yield c


def test_shelf_accounts_endpoint(client, monkeypatch):
    install(monkeypatch, shelf_handler)
    body = client.get("/api/weread/shelf/accounts").json()

    assert body["success"] is True
    assert body["data"]["total"] == 2
    assert all(a["subscribed"] is False for a in body["data"]["list"])


def test_shelf_import_subscribes_and_fetches(client, monkeypatch):
    install(monkeypatch, shelf_handler)
    fetched = []

    async def fake_fetch(fakeid):
        fetched.append(fakeid)
        return 1

    monkeypatch.setattr(rss_poller, "fetch_now", fake_fetch)

    body = client.post("/api/weread/shelf/import", json={}).json()

    assert body["success"] is True
    assert body["data"]["total"] == 2
    assert set(rss_store.get_all_fakeids()) >= {FAKEID_A, FAKEID_B}
    assert sorted(fetched) == sorted([FAKEID_A, FAKEID_B])


def test_shelf_import_can_select_subset(client, monkeypatch):
    install(monkeypatch, shelf_handler)
    monkeypatch.setattr(rss_poller, "fetch_now", lambda f: None)

    body = client.post("/api/weread/shelf/import",
                       json={"fakeids": [FAKEID_B]}).json()

    assert [i["nickname"] for i in body["data"]["imported"]] == ["另一个号"]
    assert FAKEID_A not in rss_store.get_all_fakeids()


def test_shelf_import_skips_blacklisted(client, monkeypatch):
    install(monkeypatch, shelf_handler)
    monkeypatch.setattr(rss_poller, "fetch_now", lambda f: None)
    rss_store.add_to_blacklist(FAKEID_A, reason="test")
    try:
        body = client.post("/api/weread/shelf/import", json={}).json()
        assert [s["nickname"] for s in body["data"]["skipped"]] == ["某公众号"]
        assert [i["nickname"] for i in body["data"]["imported"]] == ["另一个号"]
    finally:
        rss_store.remove_from_blacklist(FAKEID_A)


def test_shelf_endpoint_without_cookie(client, monkeypatch):
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    body = client.get("/api/weread/shelf/accounts").json()
    assert body["success"] is False
    assert "未配置" in body["error"]
