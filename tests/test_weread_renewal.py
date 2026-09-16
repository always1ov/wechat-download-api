#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wr_skey 过期后用 wr_rt 自动续期（POST /web/login/renewal）。

这些用例走 httpx.MockTransport，真实地过一遍请求头 / body / Set-Cookie 解析，
而不是把 _request 整个 mock 掉。
"""

import httpx
import pytest

from utils import weread_client as wc
from utils.weread_client import WereadClient, WereadError

BOOK_ID = "MP_WXS_3296382112"
FAKEID = "MzI5NjM4MjExMg=="
LIVE_COOKIE = "wr_vid=42; wr_skey=oldskey1; wr_rt=web%40refresh-token"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.setenv("WEREAD_COOKIE", LIVE_COOKIE)
    monkeypatch.setenv("WEREAD_CONTENT_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_PAGE_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_AUTO_ADD_SHELF", "false")
    monkeypatch.delenv("WEREAD_AUTO_RENEW", raising=False)
    wc.weread_auth._runtime_cookie = ""
    wc.reset_shelf_cache()
    yield
    wc.weread_auth._runtime_cookie = ""
    wc.reset_shelf_cache()


class Server:
    """假的微信读书：第一次列表请求判 -2012，续期之后才放行。"""

    def __init__(self, *, renew_status=200, renew_sets_cookie=True,
                 renew_body=None, expire_until_renewed=True):
        self.renew_status = renew_status
        self.renew_sets_cookie = renew_sets_cookie
        self.renew_body = renew_body
        self.expire_until_renewed = expire_until_renewed
        self.renewed = False       # 续期接口被调用过
        self.renewed_ok = False    # 续期真的换到了新 skey
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/web/login/renewal":
            self.renewed = True
            headers = {}
            if self.renew_sets_cookie:
                headers["set-cookie"] = "wr_skey=newskey2; Domain=.weread.qq.com; Path=/"
            body = self.renew_body or {}
            # 没下发新 skey 就不算续期成功，登录态照旧是过期的
            if self.renew_sets_cookie or body.get("wr_skey"):
                self.renewed_ok = True
            return httpx.Response(self.renew_status, headers=headers, json=body)

        if path == "/web/mp/articles":
            if self.expire_until_renewed and not self.renewed_ok:
                return httpx.Response(200, json={"errCode": -2012,
                                                 "errMsg": "login timeout"})
            if int(request.url.params.get("offset", 0)) != 0:
                return httpx.Response(200, json={"reviews": []})
            return httpx.Response(200, json={"reviews": [{
                "createTime": 1778580000,
                "subReviews": [{"review": {
                    "reviewId": f"{BOOK_ID}_tok1",
                    "createTime": 1778580000,
                    "mpInfo": {"title": "续期后取到的文章", "time": 1778580000,
                               "originalId": "tok1"},
                }}],
            }]})

        return httpx.Response(404, json={"errCode": -1, "errMsg": f"no route {path}"})

    def install(self, monkeypatch):
        transport = httpx.MockTransport(self.handler)
        monkeypatch.setattr(
            WereadClient, "_new_client",
            lambda self: httpx.AsyncClient(transport=transport, follow_redirects=True),
        )
        return self


# ── Cookie 串解析 ─────────────────────────────────────────

def test_parse_and_format_cookie_roundtrip():
    pairs = wc.parse_cookie(LIVE_COOKIE)
    assert pairs == {"wr_vid": "42", "wr_skey": "oldskey1",
                     "wr_rt": "web%40refresh-token"}
    assert wc.format_cookie(pairs) == LIVE_COOKIE


def test_format_cookie_drops_empty_values():
    assert wc.format_cookie({"a": "1", "b": "", "c": "3"}) == "a=1; c=3"


def test_parse_cookie_tolerates_junk():
    assert wc.parse_cookie("  ; a=1 ;; noequals ; b = 2 ") == {"a": "1", "b": "2"}


# ── 续期主流程 ────────────────────────────────────────────

async def test_auth_error_triggers_renewal_and_retry(monkeypatch):
    server = Server().install(monkeypatch)

    async with WereadClient() as client:
        articles = await client.list_articles(FAKEID, limit=5)

    assert [a["title"] for a in articles] == ["续期后取到的文章"]
    assert server.renewed is True
    # 先失败一次 → 续期 → 原样重试
    paths = [r.url.path for r in server.requests]
    assert paths[:3] == ["/web/mp/articles", "/web/login/renewal", "/web/mp/articles"]
    assert paths.count("/web/login/renewal") == 1


async def test_renewal_request_shape(monkeypatch):
    """续期必须带完整登录 Cookie，否则服务端按游客处理直接 -2013。"""
    server = Server().install(monkeypatch)

    async with WereadClient() as client:
        await client.list_articles(FAKEID, limit=5)

    renew = next(r for r in server.requests if r.url.path == "/web/login/renewal")
    assert renew.method == "POST"
    assert renew.headers["Cookie"] == LIVE_COOKIE
    assert "wr_rt=web%40refresh-token" in renew.headers["Cookie"]
    assert renew.headers["Content-Type"].startswith("application/json")
    assert renew.headers["Referer"] == "https://weread.qq.com/"
    assert renew.content == b'{"rq":"%2Fweb%2Fbook%2Fread","ql":true}'


async def test_new_skey_replaces_old_one_in_cookie(monkeypatch):
    server = Server().install(monkeypatch)

    async with WereadClient() as client:
        await client.list_articles(FAKEID, limit=5)

    # 重试那次必须带上新 skey，其余字段原样保留
    retry = [r for r in server.requests if r.url.path == "/web/mp/articles"][-1]
    pairs = wc.parse_cookie(retry.headers["Cookie"])
    assert pairs["wr_skey"] == "newskey2"
    assert pairs["wr_vid"] == "42"
    assert pairs["wr_rt"] == "web%40refresh-token"


async def test_renewed_cookie_overrides_env_cookie(monkeypatch):
    """环境变量里那份的 wr_skey 已经过期了，续期结果必须压过它。"""
    Server().install(monkeypatch)

    async with WereadClient() as client:
        await client.list_articles(FAKEID, limit=5)

    assert wc.weread_auth.get_cookie() != LIVE_COOKIE
    assert "wr_skey=newskey2" in wc.weread_auth.get_cookie()


async def test_renewed_cookie_persists_when_not_env_managed(monkeypatch, tmp_path):
    """非环境变量托管时要落盘，重启后不用再续一次。"""
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    wc.weread_auth.save_cookie(LIVE_COOKIE)
    Server().install(monkeypatch)

    async with WereadClient() as client:
        await client.list_articles(FAKEID, limit=5)

    import json
    stored = json.loads((tmp_path / ".weread.json").read_text(encoding="utf-8"))
    assert "wr_skey=newskey2" in stored["cookie"]
    assert stored["renewed_at"] > 0


async def test_renewal_takes_skey_from_json_body(monkeypatch):
    """有的响应不走 Set-Cookie，把新 skey 放在 body 里。"""
    server = Server(renew_sets_cookie=False,
                    renew_body={"wr_skey": "bodyskey"}).install(monkeypatch)

    async with WereadClient() as client:
        await client.list_articles(FAKEID, limit=5)

    assert server.renewed is True
    assert "wr_skey=bodyskey" in wc.weread_auth.get_cookie()


async def test_short_skey_is_accepted(monkeypatch):
    """wr_skey 本身就是 8 字符短效令牌，不能按长度判续期失败。"""
    server = Server(renew_sets_cookie=False,
                    renew_body={"wr_skey": "abcd1234"}).install(monkeypatch)

    async with WereadClient() as client:
        articles = await client.list_articles(FAKEID, limit=5)

    assert len(articles) == 1
    assert "wr_skey=abcd1234" in wc.weread_auth.get_cookie()


# ── 不该续期的情况 ────────────────────────────────────────

async def test_no_wr_rt_means_no_renewal(monkeypatch):
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=42; wr_skey=oldskey1")
    server = Server().install(monkeypatch)

    async with WereadClient() as client:
        with pytest.raises(WereadError) as exc:
            await client.get_articles_page(BOOK_ID)

    assert exc.value.code == -2012
    assert server.renewed is False


async def test_auto_renew_can_be_disabled(monkeypatch):
    monkeypatch.setenv("WEREAD_AUTO_RENEW", "false")
    server = Server().install(monkeypatch)

    async with WereadClient() as client:
        with pytest.raises(WereadError):
            await client.get_articles_page(BOOK_ID)

    assert server.renewed is False


async def test_failed_renewal_surfaces_original_auth_error(monkeypatch):
    server = Server(renew_status=200, renew_sets_cookie=False,
                    renew_body={}).install(monkeypatch)

    async with WereadClient() as client:
        with pytest.raises(WereadError) as exc:
            await client.get_articles_page(BOOK_ID)

    assert exc.value.code == -2012
    assert exc.value.user_message == wc.COOKIE_EXPIRED_MSG
    assert server.renewed is True


async def test_renewal_is_attempted_once_within_cooldown(monkeypatch):
    """续期失败后冷却期内不再重复打续期接口，避免风控上加风控。"""
    server = Server(renew_sets_cookie=False, renew_body={}).install(monkeypatch)

    async with WereadClient() as client:
        for _ in range(3):
            with pytest.raises(WereadError):
                await client.get_articles_page(BOOK_ID)

    assert [r.url.path for r in server.requests].count("/web/login/renewal") == 1


async def test_non_auth_errors_do_not_trigger_renewal(monkeypatch):
    """普通业务错误不该去续期。"""
    def handler(request):
        if request.url.path == "/web/mp/articles":
            return httpx.Response(200, json={"errCode": -1, "errMsg": "boom"})
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    calls = []
    monkeypatch.setattr(
        WereadClient, "_new_client",
        lambda self: httpx.AsyncClient(transport=transport, follow_redirects=True),
    )
    monkeypatch.setattr(WereadClient, "renew_cookie",
                        lambda self: calls.append("renew"))

    async with WereadClient() as client:
        payload = await client.get_articles_page(BOOK_ID)

    # -1 不是鉴权码，_request 原样返回，由解析函数抛业务错误
    with pytest.raises(WereadError):
        wc.parse_mp_articles(payload, BOOK_ID)
    assert calls == []


# ── 续期失败要说清原因，登录态没坏时别乱报错 ──────────────────

async def test_renewal_surfaces_weread_error_code(monkeypatch):
    """wr_rt 也过期时，微信读书把原因写在 body 的 errcode 里。

    以前这个码被吞掉，用户只看到「续期接口未下发新 wr_skey (HTTP 200)」，
    完全不知道该重新扫码。
    """
    def handler(request):
        if request.url.path == "/web/login/renewal":
            return httpx.Response(200, json={"errcode": -2012, "errmsg": "token expired"})
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        WereadClient, "_new_client",
        lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                       follow_redirects=True),
    )

    with pytest.raises(WereadError) as exc:
        await wc.renew_cookie_value(
            LIVE_COOKIE,
            client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(handler), follow_redirects=True),
        )

    assert exc.value.code == -2012
    assert exc.value.is_auth_error


async def test_renewal_without_error_code_tells_user_to_rescan(monkeypatch):
    """没有 errcode 可报时，兜底提示也得给出下一步动作。"""
    def handler(request):
        return httpx.Response(200, json={})

    with pytest.raises(WereadError) as exc:
        await wc.renew_cookie_value(
            LIVE_COOKIE,
            client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(handler), follow_redirects=True),
        )

    assert "重新扫码" in exc.value.message
