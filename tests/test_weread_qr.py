#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信读书扫码登录：取 uid → 出二维码 → 长轮询 → 激活并保存 Cookie。"""

import asyncio
import base64

import httpx
import pytest

from utils import weread_client as wc
from utils import weread_qr as qr
from utils.weread_client import WereadClient, WereadError
from utils.weread_client import parse_cookie
from utils.weread_qr import (WereadQRLogin, build_cookie_candidates,
                             weread_qr_login)

UID = "uid-abc"
GOOD_COOKIE_SKEY = "goodskey"


@pytest.fixture(autouse=True)
async def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    wc.weread_auth._runtime_cookie = ""
    wc.weread_auth._cache = {}
    wc.weread_auth._last_loaded_at = 0.0
    wc.reset_shelf_cache()
    yield
    await weread_qr_login.cancel()
    wc.weread_auth._runtime_cookie = ""
    wc.reset_shelf_cache()


class LoginServer:
    """假的微信读书登录端：按脚本返回 getLoginInfo 的每一轮结果。"""

    def __init__(self, script, *, set_cookies=None):
        self.script = list(script)
        self.set_cookies = set_cookies or [
            "wr_vid=42; Path=/", "wr_skey=shortkey; Path=/",
            "wr_rt=web%40refresh; Path=/",
        ]
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/auth/getLoginUid":
            return httpx.Response(200, json={"uid": UID})
        if path == "/api/auth/getLoginInfo":
            step = self.script.pop(0) if self.script else {"succeed": False, "logicCode": 0}
            if step.get("succeed"):
                headers = [("set-cookie", c) for c in self.set_cookies]
                return httpx.Response(200, json=step, headers=headers)
            return httpx.Response(200, json=step)
        return httpx.Response(404, json={})

    def install(self, monkeypatch):
        transport = httpx.MockTransport(self.handler)
        monkeypatch.setattr(
            WereadQRLogin, "_new_client",
            lambda self: httpx.AsyncClient(transport=transport, follow_redirects=True),
        )
        return self


def install_verifier(monkeypatch, *, accept_skey=GOOD_COOKIE_SKEY):
    """只认 wr_skey == accept_skey 的 Cookie，其余一律判 -2012。"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = wc.parse_cookie(request.headers.get("Cookie", ""))
        seen.append(cookie)
        if request.url.path == "/web/shelf/sync":
            if cookie.get("wr_skey") == accept_skey:
                return httpx.Response(200, json={"synckey": 1, "books": []})
            return httpx.Response(200, json={"errCode": -2012, "errMsg": "login timeout"})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        WereadClient, "_new_client",
        lambda self: httpx.AsyncClient(transport=transport, follow_redirects=True),
    )
    return seen


async def drain(timeout=3.0):
    """等后台轮询任务跑完。"""
    task = weread_qr_login._task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


# ── 二维码 ────────────────────────────────────────────────

def test_build_qr_data_uri_is_a_png():
    uri = qr.build_qr_data_uri("https://weread.qq.com/web/confirm?uid=x")
    assert uri.startswith("data:image/png;base64,")
    raw = base64.b64decode(uri.split(",", 1)[1])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_build_cookie_candidates():
    jar = {"wr_vid": "42", "wr_skey": "short", "wr_rt": "web%40rt"}
    cands = qr.build_cookie_candidates(jar, "42", "web@rt")
    assert len(cands) == 2
    # 第一个是 Set-Cookie 原样
    assert wc.parse_cookie(cands[0])["wr_skey"] == "short"
    # 第二个把 wr_skey 换成 refreshToken（长期令牌）
    assert wc.parse_cookie(cands[1])["wr_skey"] == "web@rt"
    assert wc.parse_cookie(cands[1])["wr_rt"] == "web%40rt"


def test_build_cookie_candidates_fills_vid_from_response():
    cands = qr.build_cookie_candidates({"wr_skey": "s"}, "99", "")
    assert wc.parse_cookie(cands[0])["wr_vid"] == "99"


# ── 扫码流程 ──────────────────────────────────────────────

async def test_start_returns_qr_and_waiting_state(monkeypatch):
    LoginServer([{"succeed": False, "logicCode": 0}] * 50).install(monkeypatch)
    install_verifier(monkeypatch)

    data = await weread_qr_login.start()

    assert data["uid"] == UID
    assert data["qr_image"].startswith("data:image/png;base64,")
    assert data["confirm_url"] == f"https://weread.qq.com/web/confirm?uid={UID}"
    assert data["state"] == "waiting"
    assert weread_qr_login.status()["state"] == "waiting"


async def test_scanned_state_is_reported(monkeypatch):
    LoginServer([{"succeed": False, "logicCode": 1}] * 50).install(monkeypatch)
    install_verifier(monkeypatch)

    await weread_qr_login.start()
    for _ in range(40):
        await asyncio.sleep(0.05)
        if weread_qr_login.status()["state"] == "scanned":
            break

    status = weread_qr_login.status()
    assert status["state"] == "scanned"
    assert "确认" in status["message"]


async def test_successful_login_saves_verified_cookie(monkeypatch):
    """登录下发的 wr_skey 直接可用时，原样保存。"""
    LoginServer(
        [{"succeed": False, "logicCode": 1},
         {"succeed": True, "vid": 42, "refreshToken": "web@refresh"}],
        set_cookies=[f"wr_vid=42; Path=/", f"wr_skey={GOOD_COOKIE_SKEY}; Path=/",
                     "wr_rt=web%40refresh; Path=/"],
    ).install(monkeypatch)
    install_verifier(monkeypatch)

    await weread_qr_login.start()
    await drain()

    status = weread_qr_login.status()
    assert status["state"] == "confirmed", status
    assert status["vid"] == "42"
    saved = wc.weread_auth.get_cookie()
    assert f"wr_skey={GOOD_COOKIE_SKEY}" in saved
    assert "wr_rt=web%40refresh" in saved


async def test_short_skey_is_activated_via_renewal(monkeypatch):
    """登录下发的短 wr_skey 被判 -2012 时，用 wr_rt 续期换一个能用的。"""
    LoginServer(
        [{"succeed": True, "vid": 42, "refreshToken": "web@refresh"}],
        set_cookies=["wr_vid=42; Path=/", "wr_skey=shortkey; Path=/",
                     "wr_rt=web%40refresh; Path=/"],
    ).install(monkeypatch)
    install_verifier(monkeypatch)

    renewed_from = []

    async def fake_renew(cookie, **kwargs):
        renewed_from.append(cookie)
        pairs = wc.parse_cookie(cookie)
        pairs["wr_skey"] = GOOD_COOKIE_SKEY
        return wc.format_cookie(pairs)

    monkeypatch.setattr(qr, "renew_cookie_value", fake_renew)

    await weread_qr_login.start()
    await drain()

    assert weread_qr_login.status()["state"] == "confirmed"
    assert renewed_from, "短 skey 被拒后应该尝试续期"
    assert f"wr_skey={GOOD_COOKIE_SKEY}" in wc.weread_auth.get_cookie()


async def test_login_fails_when_no_candidate_verifies(monkeypatch):
    LoginServer(
        [{"succeed": True, "vid": 42, "refreshToken": "web@refresh"}],
    ).install(monkeypatch)
    install_verifier(monkeypatch)

    async def fake_renew(cookie, **kwargs):
        raise WereadError("renew_failed", "no new skey")

    monkeypatch.setattr(qr, "renew_cookie_value", fake_renew)

    await weread_qr_login.start()
    await drain()

    assert weread_qr_login.status()["state"] == "error"
    assert wc.weread_auth.get_cookie() == ""


async def test_expired_qr_is_reported(monkeypatch):
    LoginServer([{"succeed": False, "logicCode": "LOGIN_TIMEOUT"}]).install(monkeypatch)
    install_verifier(monkeypatch)

    await weread_qr_login.start()
    await drain()

    status = weread_qr_login.status()
    assert status["state"] == "expired"
    assert "过期" in status["message"]


async def test_need_otp_is_reported(monkeypatch):
    LoginServer([{"succeed": False, "logicCode": "NEED_OTP"}]).install(monkeypatch)
    install_verifier(monkeypatch)

    await weread_qr_login.start()
    await drain()

    assert weread_qr_login.status()["state"] == "error"
    assert "验证码" in weread_qr_login.status()["message"]


async def test_cancel_stops_polling(monkeypatch):
    LoginServer([{"succeed": False, "logicCode": 0}] * 50).install(monkeypatch)
    install_verifier(monkeypatch)

    await weread_qr_login.start()
    await weread_qr_login.cancel()

    assert weread_qr_login.status()["state"] == "idle"
    assert weread_qr_login._task is None


# ── wr_rt 必须被保住，否则登录完只能活 1.5 小时 ──────────────

def test_candidates_always_carry_refresh_token():
    """每个候选都得带上 wr_rt。

    微信读书的 Set-Cookie 不一定下发 wr_rt，refreshToken 是从登录响应体里拿的。
    原来 wr_rt 只出现在「wr_skey 换成 refreshToken」那个备选里，而
    _activate_cookie 取的是第一个验证通过的候选 —— 刚扫完码的 wr_skey 必然有效，
    于是永远命中不含 wr_rt 的那份，wr_rt 被丢掉。
    表现就是：登录当时一切正常，约 1.5 小时后 wr_skey 过期，自动续期却发现
    没有 wr_rt，救不回来，用户只能反复重新扫码。
    """
    jar = {"wr_vid": "42", "wr_skey": "short1"}      # Set-Cookie 里没有 wr_rt
    candidates = build_cookie_candidates(jar, "42", "the-refresh-token")

    assert candidates, "至少要有一个候选"
    for cookie in candidates:
        assert parse_cookie(cookie).get("wr_rt"), \
            f"候选里没有 wr_rt，过期后必然救不回来: {cookie}"


def test_jar_refresh_token_is_not_overwritten():
    """Set-Cookie 自己给了 wr_rt 时，以它为准，别用响应体里的覆盖掉。"""
    jar = {"wr_vid": "42", "wr_skey": "short1", "wr_rt": "from-set-cookie"}
    candidates = build_cookie_candidates(jar, "42", "from-body")

    assert parse_cookie(candidates[0])["wr_rt"] == "from-set-cookie"


def test_no_refresh_token_anywhere_still_yields_a_candidate():
    """确实哪儿都没有 wr_rt 时不能崩，照样给出候选（后续会提示重新扫码）。"""
    jar = {"wr_vid": "42", "wr_skey": "short1"}
    candidates = build_cookie_candidates(jar, "42", "")

    assert len(candidates) == 1
    assert parse_cookie(candidates[0]).get("wr_rt") is None


async def test_login_persists_wr_rt_when_set_cookie_omits_it(monkeypatch, tmp_path):
    """端到端：Set-Cookie 不给 wr_rt 时，登录存下来的 Cookie 仍必须含 wr_rt。

    这是「自动维护救不回来」的根因回归测试 —— 登录当时看不出问题，
    要等 wr_skey 过期才暴露，所以必须在这里钉住。
    """
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    wc.weread_auth._runtime_cookie = ""

    def handler(request):
        path = request.url.path
        if path.endswith("/api/auth/getLoginUid"):
            return httpx.Response(200, json={"uid": "uid-1"})
        if path.endswith("/api/auth/getLoginInfo"):
            # 关键：Set-Cookie 只给 wr_skey / wr_vid，wr_rt 只在响应体里
            return httpx.Response(
                200,
                json={"succeed": 1, "vid": 42, "refreshToken": "long-lived-token"},
                headers={"set-cookie": "wr_skey=short1; Path=/"},
            )
        if path == "/web/shelf/sync":
            return httpx.Response(200, json={"synckey": 1, "books": []})
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(WereadQRLogin, "_new_client",
                        lambda self: httpx.AsyncClient(transport=transport,
                                                       follow_redirects=True))
    monkeypatch.setattr(WereadClient, "_new_client",
                        lambda self: httpx.AsyncClient(transport=transport,
                                                       follow_redirects=True))

    login = WereadQRLogin()
    login._instance = None
    await login.start()
    for _ in range(50):
        if login.status()["state"] in ("confirmed", "error", "expired"):
            break
        await asyncio.sleep(0.05)

    assert login.status()["state"] == "confirmed", login.status()["message"]
    saved = wc.weread_auth.get_cookie()
    assert "wr_rt" in saved, f"登录存下的 Cookie 没有 wr_rt，1.5 小时后就救不回来: {saved}"
    assert wc.weread_auth.has_refresh_token() is True

    wc.weread_auth._runtime_cookie = ""
