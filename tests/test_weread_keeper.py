#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录态守护：主动探活 + 续期 + 续不回来时报警。

走 httpx.MockTransport，真实地过一遍 /web/shelf/sync 和 /web/login/renewal，
而不是把 check_once 里的步骤 mock 掉。
"""

import httpx
import pytest

from utils import weread_client as wc
from utils import weread_keeper as wk
from utils.weread_client import WereadClient
from utils.weread_keeper import WereadKeeper

LIVE_COOKIE = "wr_vid=42; wr_skey=oldskey1; wr_rt=web%40refresh-token"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.setenv("WEREAD_COOKIE", LIVE_COOKIE)
    monkeypatch.setenv("WEREAD_CONTENT_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_PAGE_INTERVAL", "0")
    monkeypatch.delenv("WEREAD_AUTO_RENEW", raising=False)
    monkeypatch.delenv("WEREAD_KEEPALIVE", raising=False)
    monkeypatch.delenv("WEREAD_KEEPALIVE_INTERVAL", raising=False)
    wc.weread_auth._runtime_cookie = ""
    wc.reset_shelf_cache()
    yield
    wc.weread_auth._runtime_cookie = ""
    wc.reset_shelf_cache()


class Server:
    """假微信读书。skey 过期时 /web/shelf/sync 判 -2012，续期后放行。"""

    def __init__(self, *, expired=False, renew_works=True):
        self.expired = expired
        self.renew_works = renew_works
        self.verify_calls = 0
        self.renew_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/web/login/renewal":
            self.renew_calls += 1
            if not self.renew_works:
                # wr_rt 也死了：微信读书这时回的就是登录态错误码
                return httpx.Response(200, json={"errcode": -2012, "errmsg": "token expired"})
            self.expired = False
            return httpx.Response(200, json={},
                                  headers={"set-cookie": "wr_skey=freshkey; Path=/"})
        if path == "/web/shelf/sync":
            self.verify_calls += 1
            if self.expired:
                return httpx.Response(200, json={"errcode": -2012, "errmsg": "login timeout"})
            return httpx.Response(200, json={"synckey": 1, "books": []})
        return httpx.Response(200, json={})


@pytest.fixture
def install(monkeypatch):
    def _install(server: Server):
        monkeypatch.setattr(
            WereadClient, "_new_client",
            lambda self: httpx.AsyncClient(transport=httpx.MockTransport(server),
                                           follow_redirects=True),
        )
        return server
    return _install


# ── 正常情况：探活通过就别去打续期接口 ──────────────────────

async def test_healthy_check_does_not_renew(install):
    server = install(Server())
    keeper = WereadKeeper()

    status = await keeper.check_once()

    assert status["last_check_ok"] is True
    assert status["needs_relogin"] is False
    assert server.renew_calls == 0, "登录态好好的，不该去续期"
    assert status["last_check_at"] > 0


# ── 过期：主动续回来，用户无感 ───────────────────────────

async def test_expired_is_renewed_automatically(install):
    server = install(Server(expired=True))
    keeper = WereadKeeper()

    status = await keeper.check_once()

    assert server.renew_calls == 1
    assert status["last_renew_ok"] is True
    assert status["last_check_ok"] is True
    assert status["needs_relogin"] is False
    # 续完必须再验一次，确认新 skey 真能用
    assert server.verify_calls == 2
    assert "freshkey" in wc.weread_auth.get_cookie()


# ── wr_rt 也死了：连续失败到阈值才认定要重新扫码 ─────────────

async def test_dead_refresh_token_eventually_needs_relogin(install):
    server = install(Server(expired=True, renew_works=False))
    keeper = WereadKeeper()

    first = await keeper.check_once()
    assert first["last_check_ok"] is False
    assert first["failures"] == 1
    assert first["needs_relogin"] is False, "抖动一次不该立刻报警"

    second = await keeper.check_once()
    assert second["failures"] == 2
    assert second["needs_relogin"] is True
    assert second["message"]


async def test_recovery_resets_failures(install):
    server = Server(expired=True, renew_works=False)
    install(server)
    keeper = WereadKeeper()

    await keeper.check_once()
    await keeper.check_once()
    assert keeper.needs_relogin is True

    # 用户重新扫码了：探活恢复正常
    server.expired = False
    server.renew_works = True
    status = await keeper.check_once()

    assert status["last_check_ok"] is True
    assert status["failures"] == 0
    assert status["needs_relogin"] is False


# ── 没登录过：那是还没开始用，不是坏了，不该报警 ───────────

async def test_not_configured_is_not_an_alarm(monkeypatch, install):
    install(Server())
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    keeper = WereadKeeper()

    status = await keeper.check_once()

    assert status["last_check_ok"] is False
    assert status["failures"] == 0
    assert status["needs_relogin"] is False
    assert "扫码" in status["message"]


# ── 关掉自动续期时，守护不该偷偷去续 ────────────────────────

async def test_respects_auto_renew_off(monkeypatch, install):
    server = install(Server(expired=True))
    monkeypatch.setenv("WEREAD_AUTO_RENEW", "false")
    keeper = WereadKeeper()

    status = await keeper.check_once()

    assert server.renew_calls == 0
    assert status["last_check_ok"] is False
    assert "WEREAD_AUTO_RENEW" in status["message"]


# ── 配置 ────────────────────────────────────────────────

def test_keepalive_can_be_disabled(monkeypatch):
    monkeypatch.setenv("WEREAD_KEEPALIVE", "false")
    assert wk.keepalive_enabled() is False


def test_interval_has_a_floor(monkeypatch):
    monkeypatch.setenv("WEREAD_KEEPALIVE_INTERVAL", "5")
    assert wk.keepalive_interval() == 300, "间隔太小会白白消耗微信读书配额"


def test_interval_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("WEREAD_KEEPALIVE_INTERVAL", "abc")
    assert wk.keepalive_interval() == wk.DEFAULT_INTERVAL
