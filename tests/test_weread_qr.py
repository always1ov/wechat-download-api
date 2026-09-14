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
from utils.weread_qr import WereadQRLogin, weread_qr_login

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
