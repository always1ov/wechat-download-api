#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录态守护：主动探活 + 续期 + 续不回来时报警。

走 httpx.MockTransport，真实地过一遍 /web/shelf/sync 和 /web/login/renewal，
而不是把 check_once 里的步骤 mock 掉。
"""

import time

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
    # 「最近成功过」是模块级全局，不清会串到下一个用例，
    # 让守护误以为登录态刚被验证过而跳过探活
    wc._last_success_at = 0.0
    yield
    wc.weread_auth._runtime_cookie = ""
    wc.reset_shelf_cache()
    wc._last_success_at = 0.0


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
    assert wk.keepalive_interval() == wk.MIN_INTERVAL, \
        "查得再勤也不会更早发现 wr_rt 失效，只会平白多打接口"


def test_interval_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("WEREAD_KEEPALIVE_INTERVAL", "abc")
    assert wk.keepalive_interval() == wk.DEFAULT_INTERVAL



# ── 频率：别给微信读书添无谓的请求，也别把节奏做成机器 ──────────

async def test_skips_check_when_traffic_recently_succeeded(install):
    """采集刚成功过就说明登录态是活的，守护这一轮不该再打接口。"""
    server = install(Server())
    wc._last_success_at = time.time()     # 假装一分钟内刚采过
    keeper = WereadKeeper()

    status = await keeper.check_once()

    assert server.verify_calls == 0, "登录态刚被真实流量验证过，不该再探活"
    assert status["last_check_ok"] is True
    assert status["skipped"] == 1


async def test_manual_check_never_skips(install):
    """用户手动点「立刻检查」，就得给个真实结果，不能拿缓存糊弄。"""
    server = install(Server())
    wc._last_success_at = time.time()
    keeper = WereadKeeper()

    status = await keeper.check_once(force=True)

    assert server.verify_calls == 1
    assert status["skipped"] == 0


async def test_stale_traffic_does_not_skip(install):
    """上次成功已经很久了，就得老老实实查一次。"""
    server = install(Server())
    wc._last_success_at = time.time() - wk.SKIP_IF_ALIVE_WITHIN - 1
    keeper = WereadKeeper()

    await keeper.check_once()

    assert server.verify_calls == 1


def test_interval_is_jittered():
    """严格等间隔本身就是机器特征，必须带抖动。"""
    base = wk.keepalive_interval()
    samples = [wk._next_delay() for _ in range(40)]

    assert len(set(samples)) > 1, "每轮间隔完全一样，等于给风控送把柄"
    lo = base * (1 - wk.INTERVAL_JITTER)
    hi = base * (1 + wk.INTERVAL_JITTER)
    assert all(lo <= x <= hi for x in samples), "抖动不该超出 ±15%"


def test_default_interval_is_conservative():
    """默认间隔要明显宽于同类项目的同步频率（2~3 小时）。"""
    assert wk.DEFAULT_INTERVAL >= 3 * 3600
