#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 接口层：公众号后台不可用时改走微信读书。"""

import pytest
from fastapi.testclient import TestClient

from utils import weread_client as wc
from utils.auth_manager import auth_manager
from utils.rate_limiter import rate_limiter
from utils.weread_client import WereadClient, WereadError

FAKEID = "MzI5NjM4MjExMg=="
BOOK_ID = "MP_WXS_3296382112"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=1; wr_skey=test")
    monkeypatch.setenv("WEREAD_CONTENT_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_PAGE_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_AUTO_ADD_SHELF", "false")
    monkeypatch.delenv("WEREAD_ENABLED", raising=False)
    monkeypatch.delenv("ARTICLE_SOURCE", raising=False)
    # 限频器在 import 时就固化了阈值，且 /api/article 默认 3 秒一篇 —— 测试里关掉
    monkeypatch.setattr(rate_limiter, "ARTICLE_INTERVAL", 0)
    monkeypatch.setattr(rate_limiter, "GLOBAL_LIMIT", 10 ** 6)
    monkeypatch.setattr(rate_limiter, "IP_LIMIT", 10 ** 6)
    wc.reset_shelf_cache()
    yield
    wc.reset_shelf_cache()


@pytest.fixture
def client():
    import app

    with TestClient(app.app) as c:
        yield c


def _install_weread(monkeypatch, *, titles=(), content="", error=None):
    async def _request(self, method, path, *, params=None, json_data=None,
                       as_json=True, interval=0.0):
        if error is not None:
            raise error
        if path == "/web/mp/articles":
            if int(params.get("offset", 0)) != 0:
                return {"reviews": []}
            return {"reviews": [
                {"createTime": 1778580000, "subReviews": [{"review": {
                    "reviewId": f"{BOOK_ID}_{t}",
                    "createTime": 1778580000,
                    "mpInfo": {"title": t, "time": 1778580000, "originalId": t},
                }}]}
                for t in titles
            ]}
        if path == "/web/mp/content":
            return content
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(WereadClient, "_request", _request)


# ── /api/public/articles ─────────────────────────────────

def test_articles_uses_weread_when_backend_not_logged_in(client, monkeypatch):
    """后台没登录（凭证过期是常态）也能出文章列表，而不是直接 401。"""
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    _install_weread(monkeypatch, titles=["tok1", "tok2"])

    r = client.get("/api/public/articles", params={"fakeid": FAKEID, "count": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["source"] == "weread"
    assert [a["title"] for a in body["data"]["articles"]] == ["tok1", "tok2"]


def test_articles_401_when_both_channels_unavailable(client, monkeypatch):
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)

    r = client.get("/api/public/articles", params={"fakeid": FAKEID})
    assert r.status_code == 401


def test_articles_source_mp_disables_weread(client, monkeypatch):
    """ARTICLE_SOURCE=mp 时不许偷偷走微信读书。"""
    monkeypatch.setenv("ARTICLE_SOURCE", "mp")
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})

    r = client.get("/api/public/articles", params={"fakeid": FAKEID})
    assert r.status_code == 401


def test_articles_keyword_filters_weread_results(client, monkeypatch):
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    _install_weread(monkeypatch, titles=["alpha", "beta"])

    r = client.get("/api/public/articles",
                   params={"fakeid": FAKEID, "keyword": "alph"})
    assert [a["title"] for a in r.json()["data"]["articles"]] == ["alpha"]


def test_articles_reports_weread_failure(client, monkeypatch):
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    _install_weread(monkeypatch, error=WereadError(-2012, "login timeout", retriable=False))

    body = client.get("/api/public/articles", params={"fakeid": FAKEID}).json()
    assert body["success"] is False
    assert "微信读书" in body["error"]


# ── /api/article ─────────────────────────────────────────

def test_article_falls_back_to_weread_content(client, monkeypatch):
    """后台没登录时，带上 fakeid 仍可通过微信读书拿到正文。"""
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    _install_weread(monkeypatch, content='<div id="js_content"><p>读书正文</p></div>')

    r = client.post("/api/article", json={
        "url": "https://mp.weixin.qq.com/s/tok1",
        "fakeid": FAKEID,
    })
    body = r.json()
    assert body["success"] is True, body
    assert "读书正文" in body["data"]["content"]


def test_article_without_fakeid_explains_what_is_missing(client, monkeypatch):
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    _install_weread(monkeypatch, content="<p>x</p>")

    body = client.post("/api/article",
                       json={"url": "https://mp.weixin.qq.com/s/unknown-token"}).json()
    assert body["success"] is False
    assert "fakeid" in body["error"]


def test_article_long_link_not_supported_by_weread(client, monkeypatch):
    """长链没有短链 token，推不出 reviewId —— 提示要说清楚。"""
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    _install_weread(monkeypatch, content="<p>x</p>")

    body = client.post("/api/article", json={
        "url": "https://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=1&sn=2",
        "fakeid": FAKEID,
    }).json()
    assert body["success"] is False
    assert "短链" in body["error"]


def test_article_requires_some_channel(client, monkeypatch):
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)

    body = client.post("/api/article",
                       json={"url": "https://mp.weixin.qq.com/s/tok1"}).json()
    assert body["success"] is False
    assert "扫码登录" in body["error"]


# ── /api/weread/* ────────────────────────────────────────

def test_weread_status_reports_config(client):
    data = client.get("/api/weread/status").json()["data"]
    assert data["configured"] is True
    assert data["enabled"] is True
    assert data["env_managed"] is True
    assert data["vid"] == "1"
    assert "wr_skey=test" not in data["cookie_preview"]


def test_weread_articles_endpoint(client, monkeypatch):
    _install_weread(monkeypatch, titles=["tok1"])
    body = client.get("/api/weread/articles", params={"fakeid": FAKEID}).json()
    assert body["success"] is True
    assert body["data"]["book_id"] == BOOK_ID
    assert body["data"]["articles"][0]["review_id"] == f"{BOOK_ID}_tok1"


def test_weread_content_endpoint_accepts_url_and_fakeid(client, monkeypatch):
    _install_weread(monkeypatch, content='<div id="js_content"><p>正文</p></div>')
    body = client.get("/api/weread/content", params={
        "url": "https://mp.weixin.qq.com/s/tok1", "fakeid": FAKEID,
    }).json()
    assert body["success"] is True
    assert body["data"]["review_id"] == f"{BOOK_ID}_tok1"
    assert "正文" in body["data"]["content"]


def test_weread_content_endpoint_requires_identifiers(client):
    body = client.get("/api/weread/content").json()
    assert body["success"] is False
    assert "review_id" in body["error"]


# ── 扫码登录 / 续期接口 ────────────────────────────────────

def test_weread_qrcode_endpoint(client, monkeypatch):
    import httpx

    from utils.weread_qr import WereadQRLogin, weread_qr_login

    def handler(request):
        if request.url.path == "/api/auth/getLoginUid":
            return httpx.Response(200, json={"uid": "uid-1"})
        return httpx.Response(200, json={"succeed": False, "logicCode": 0})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        WereadQRLogin, "_new_client",
        lambda self: httpx.AsyncClient(transport=transport, follow_redirects=True),
    )

    body = client.post("/api/weread/qrcode").json()
    assert body["success"] is True
    assert body["data"]["uid"] == "uid-1"
    assert body["data"]["qr_image"].startswith("data:image/png;base64,")
    assert body["data"]["state"] == "waiting"

    status = client.get("/api/weread/qrcode/status").json()
    assert status["data"]["state"] in ("waiting", "scanned")

    assert client.request("DELETE", "/api/weread/qrcode").json()["data"]["state"] == "idle"


def test_weread_renew_requires_cookie(client, monkeypatch):
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    body = client.post("/api/weread/renew").json()
    assert body["success"] is False
    assert "未配置" in body["error"]


def test_weread_renew_reports_missing_wr_rt(client, monkeypatch):
    """没有 wr_rt 就续不了期，提示要说清楚下一步做什么。"""
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=1; wr_skey=only")
    body = client.post("/api/weread/renew").json()
    assert body["success"] is False
    assert "wr_rt" in body["error"]
