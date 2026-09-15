#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 接口层：文章列表 / 正文 / 微信读书管理接口。"""

import pytest
from fastapi.testclient import TestClient

from utils import weread_client as wc
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
    _install_weread(monkeypatch, titles=["tok1", "tok2"])

    r = client.get("/api/public/articles", params={"fakeid": FAKEID, "count": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["source"] == "weread"
    assert [a["title"] for a in body["data"]["articles"]] == ["tok1", "tok2"]


def test_articles_without_login_explains_what_to_do(client, monkeypatch):
    """没登录微信读书时给可操作的提示，而不是一个裸 401。"""
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)

    body = client.get("/api/public/articles", params={"fakeid": FAKEID}).json()
    assert body["success"] is False
    assert body["error"] == wc.COOKIE_MISSING_MSG


def test_articles_keyword_filters_weread_results(client, monkeypatch):
    _install_weread(monkeypatch, titles=["alpha", "beta"])

    r = client.get("/api/public/articles",
                   params={"fakeid": FAKEID, "keyword": "alph"})
    assert [a["title"] for a in r.json()["data"]["articles"]] == ["alpha"]


def test_articles_reports_weread_failure(client, monkeypatch):
    _install_weread(monkeypatch, error=WereadError(-2012, "login timeout", retriable=False))

    body = client.get("/api/public/articles", params={"fakeid": FAKEID}).json()
    assert body["success"] is False
    assert "微信读书" in body["error"]


# ── /api/article ─────────────────────────────────────────

def test_article_falls_back_to_weread_content(client, monkeypatch):
    """后台没登录时，带上 fakeid 仍可通过微信读书拿到正文。"""
    _install_weread(monkeypatch, content='<div id="js_content"><p>读书正文</p></div>')

    r = client.post("/api/article", json={
        "url": "https://mp.weixin.qq.com/s/tok1",
        "fakeid": FAKEID,
    })
    body = r.json()
    assert body["success"] is True, body
    assert "读书正文" in body["data"]["content"]


def test_article_without_fakeid_explains_what_is_missing(client, monkeypatch):
    _install_weread(monkeypatch, content="<p>x</p>")

    body = client.post("/api/article",
                       json={"url": "https://mp.weixin.qq.com/s/unknown-token"}).json()
    assert body["success"] is False
    assert "fakeid" in body["error"]


def test_article_long_link_not_supported_by_weread(client, monkeypatch):
    """长链没有短链 token，推不出 reviewId —— 提示要说清楚。"""
    _install_weread(monkeypatch, content="<p>x</p>")

    body = client.post("/api/article", json={
        "url": "https://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=1&sn=2",
        "fakeid": FAKEID,
    }).json()
    assert body["success"] is False
    assert "短链" in body["error"]


def test_article_requires_login(client, monkeypatch):
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)

    body = client.post("/api/article",
                       json={"url": "https://mp.weixin.qq.com/s/tok1"}).json()
    assert body["success"] is False
    assert body["error"] == wc.COOKIE_MISSING_MSG


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
    assert body["error"] == wc.COOKIE_MISSING_MSG


def test_weread_renew_reports_missing_wr_rt(client, monkeypatch):
    """没有 wr_rt 就续不了期，提示要说清楚下一步做什么。"""
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=1; wr_skey=only")
    body = client.post("/api/weread/renew").json()
    assert body["success"] is False
    assert "wr_rt" in body["error"]


def test_weread_diagnose_reports_each_step(client, monkeypatch):
    """诊断接口要把每一步单独试完，不能前面失败就整体中断。"""
    import httpx

    def handler(request):
        p = request.url.path
        if p == "/web/shelf/sync":
            return httpx.Response(200, json={"errCode": -2012, "errMsg": "login timeout"})
        if p == "/api/mp/cover":
            return httpx.Response(200, json={"reviewId": f"{BOOK_ID}_tok", "title": "最新"})
        return httpx.Response(401, text="unauthorized")

    monkeypatch.setattr(
        WereadClient, "_new_client",
        lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                       follow_redirects=True),
    )

    body = client.get("/api/weread/diagnose", params={"fakeid": FAKEID}).json()
    steps = {s["step"]: s for s in body["data"]["steps"]}

    # 登录态失败了，但后面的步骤仍然要各自跑完
    assert steps["登录态有效 (/web/shelf/sync)"]["ok"] is False
    assert steps["fakeid → bookId"]["ok"] is True
    assert steps["fakeid → bookId"]["detail"] == BOOK_ID
    assert steps["最新一篇兜底 (/api/mp/cover)"]["ok"] is True
    assert body["data"]["failed"], "应当列出失败环节"


def test_weread_diagnose_without_cookie(client, monkeypatch):
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    body = client.get("/api/weread/diagnose").json()
    assert body["success"] is False
    assert body["data"]["steps"][0]["ok"] is False


# ── 退出登录：前端要靠 message 解释为什么退不掉 ────────────────

def test_logout_env_managed_explains_why_it_failed(client, monkeypatch):
    """WEREAD_COOKIE 托管时退不掉，必须说清楚原因。

    前端以前只显示干巴巴一句「退出失败」，用户完全不知道该改环境变量；
    现在它读 message，所以这里把契约钉住：失败也必须带可读的 message。
    """
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=1; wr_skey=test")

    body = client.post("/api/admin/logout").json()

    assert body["success"] is False
    assert "WEREAD_COOKIE" in body["message"]


def test_logout_without_env_cookie_succeeds_with_message(client, monkeypatch):
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)

    body = client.post("/api/admin/logout").json()

    assert body["success"] is True
    assert body["message"]
