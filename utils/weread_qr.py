#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
微信读书扫码登录

免去「浏览器 F12 复制 Cookie」这一步：拿 uid 生成二维码，微信扫一下就拿到
登录态，和公众号后台的扫码登录体验一致。

流程（接口来自 rachelos/we-mp-rss 的 driver/weread_qr.py）：
1. GET  /api/auth/getLoginUid              取 uid
2. 二维码内容 = https://weread.qq.com/web/confirm?uid=<uid>
3. GET  /api/auth/getLoginInfo?uid=&otp=   长轮询扫码状态（最长挂 70s）
4. 成功后 Set-Cookie 下发 wr_skey / wr_rt，用 /web/login/renewal 激活再验证

注意第 4 步：登录直接下发的 wr_skey 是 8 字符短值（accessToken），不少环境下
会被服务端以 -2012 拒掉；真正的长期令牌是 refreshToken（即 wr_rt）。所以拿到
Cookie 后要先续期换一个可用的 wr_skey，验证通过才算登录成功。
"""

import asyncio
import base64
import io
import logging
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from utils.weread_client import (
    WEREAD_BASE,
    WereadClient,
    WereadError,
    format_cookie,
    parse_cookie,
    renew_cookie_value,
    reset_shelf_cache,
    user_agent,
    weread_auth,
)

logger = logging.getLogger(__name__)

# getLoginInfo 是长轮询，官方前端用 70s；短了会把正常的等待当成超时
LONG_POLL_TIMEOUT = 75.0
# 二维码有效期，超过就让用户重新点一次
LOGIN_TIMEOUT = 300.0

STATE_IDLE = "idle"
STATE_WAITING = "waiting"        # 等待扫码
STATE_SCANNED = "scanned"        # 已扫码，等手机上确认
STATE_CONFIRMED = "confirmed"    # 登录成功且 Cookie 已验证
STATE_EXPIRED = "expired"
STATE_ERROR = "error"


def build_qr_data_uri(confirm_url: str) -> str:
    """把确认链接画成二维码，返回 data URI。

    刻意本地生成而不是调在线二维码服务：国内网络访问不到那些服务，
    前端只会看到一张空白图。
    """
    try:
        import qrcode
    except ImportError as exc:
        raise WereadError(
            "missing_qrcode",
            "缺少 qrcode 库，无法生成二维码。请执行 pip install qrcode Pillow 后重试",
            retriable=False,
        ) from exc

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(confirm_url)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def build_cookie_candidates(jar: Dict[str, str], vid: str,
                            refresh_token: str) -> List[str]:
    """登录后可能可用的 Cookie 组合，按可能性排序。

    只留两种：Set-Cookie 原样，以及「wr_skey 换成 refreshToken」。
    其余组合交给续期解决 —— 续期才是官方前端的做法，靠枚举撞对反而脆。
    """
    base = {k: v for k, v in jar.items() if v}
    if vid:
        base.setdefault("wr_vid", str(vid))

    candidates = [dict(base)]
    if refresh_token:
        variant = dict(base)
        variant["wr_skey"] = refresh_token
        variant["wr_rt"] = quote(refresh_token, safe="")
        candidates.append(variant)

    out, seen = [], set()
    for cand in candidates:
        cookie = format_cookie(cand)
        if cookie and cookie not in seen:
            seen.add(cookie)
            out.append(cookie)
    return out


class WereadQRLogin:
    """微信读书扫码登录状态机（单例，同一时刻只跑一个登录会话）。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._uid = ""
        self._qr: str = ""
        self._state = STATE_IDLE
        self._message = ""
        self._vid = ""
        self._started_at = 0.0
        self._initialized = True

    # --- 对外 ---

    async def start(self) -> Dict:
        """开一次扫码登录，返回二维码。重复调用会重开一次。"""
        async with self._lock:
            await self._cleanup()

            self._client = self._new_client()

            uid = await self._get_login_uid()
            confirm_url = f"{WEREAD_BASE}/web/confirm?uid={uid}"

            self._uid = uid
            self._qr = build_qr_data_uri(confirm_url)
            self._state = STATE_WAITING
            self._message = "请用微信扫码"
            self._started_at = time.monotonic()
            self._task = asyncio.create_task(self._poll_loop())

            return {
                "uid": uid,
                "qr_image": self._qr,
                "confirm_url": confirm_url,
                "state": self._state,
                "expires_in": int(LOGIN_TIMEOUT),
            }

    def status(self) -> Dict:
        """当前扫码状态。前端轮询这个，不要直接打微信读书的长轮询接口。"""
        return {
            "state": self._state,
            "message": self._message,
            "uid": self._uid,
            "vid": self._vid,
            "qr_image": self._qr if self._state in (STATE_WAITING, STATE_SCANNED) else "",
        }

    async def cancel(self) -> None:
        async with self._lock:
            await self._cleanup()
            self._state = STATE_IDLE
            self._message = ""

    # --- 内部 ---

    def _new_client(self) -> httpx.AsyncClient:
        """登录会话客户端。整个流程共用一个 cookie jar —— getLoginInfo 的
        Set-Cookie 就是登录态本身，换客户端就丢了。集中一处便于测试替换。"""
        return httpx.AsyncClient(
            timeout=httpx.Timeout(LONG_POLL_TIMEOUT, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": user_agent(),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": WEREAD_BASE,
                "Referer": f"{WEREAD_BASE}/",
            },
        )

    async def _cleanup(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
        self._uid = ""
        self._qr = ""
        self._vid = ""

    async def _get_login_uid(self) -> str:
        try:
            resp = await self._client.get(f"{WEREAD_BASE}/api/auth/getLoginUid",
                                          timeout=20.0)
        except httpx.RequestError as exc:
            raise WereadError("network_error", f"获取登录 uid 失败: {exc}") from exc
        if resp.status_code != 200:
            raise WereadError(resp.status_code,
                              f"getLoginUid 返回 HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise WereadError("invalid_json", "getLoginUid 未返回 JSON") from exc
        uid = data.get("uid") or (data.get("data") or {}).get("uid")
        if not uid:
            raise WereadError("no_uid", "getLoginUid 未返回 uid")
        return str(uid)

    async def _poll_loop(self) -> None:
        """长轮询扫码状态，直到成功 / 超时 / 被取消。"""
        try:
            while time.monotonic() - self._started_at < LOGIN_TIMEOUT:
                try:
                    resp = await self._client.get(
                        f"{WEREAD_BASE}/api/auth/getLoginInfo",
                        params={"uid": self._uid, "otp": ""},
                    )
                except httpx.RequestError as exc:
                    logger.debug("[WeReadQR] 轮询网络异常: %s", exc)
                    await asyncio.sleep(2)
                    continue

                if resp.status_code != 200:
                    await asyncio.sleep(2)
                    continue

                try:
                    data = resp.json()
                except ValueError:
                    await asyncio.sleep(2)
                    continue

                # 登录信息有时在顶层、有时在 data 子层
                inner = data.get("data") or {}
                if data.get("succeed") or inner.get("succeed"):
                    await self._on_login_success(data, inner)
                    return

                logic_code = str(data.get("logicCode", inner.get("logicCode", "")))
                if logic_code == "1":
                    self._state = STATE_SCANNED
                    self._message = "已扫码，请在手机上确认"
                elif logic_code in ("LOGIN_TIMEOUT", "-2013"):
                    self._state = STATE_EXPIRED
                    self._message = "二维码已过期，请重新获取"
                    return
                elif logic_code == "NEED_OTP":
                    self._state = STATE_ERROR
                    self._message = "该账号需要验证码，暂不支持扫码登录"
                    return

                await asyncio.sleep(1)

            self._state = STATE_EXPIRED
            self._message = "二维码已过期，请重新获取"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[WeReadQR] 轮询异常: %s", exc)
            self._state = STATE_ERROR
            self._message = f"扫码登录异常: {exc}"

    async def _on_login_success(self, data: Dict, inner: Dict) -> None:
        vid = str(data.get("vid") or inner.get("vid") or "")
        refresh_token = str(data.get("refreshToken") or inner.get("refreshToken") or "")

        jar = {c.name: c.value for c in self._client.cookies.jar if c.value}
        cookie, error = await self._activate_cookie(jar, vid, refresh_token)

        if not cookie:
            self._state = STATE_ERROR
            self._message = error or "登录成功但 Cookie 验证未通过，请重新扫码"
            logger.warning("[WeReadQR] %s", self._message)
            return

        ok, message = weread_auth.save_cookie(cookie)
        if not ok:
            self._state = STATE_ERROR
            self._message = f"Cookie 保存失败: {message}"
            return

        reset_shelf_cache()
        self._vid = vid or weread_auth.extract_vid(cookie)
        self._state = STATE_CONFIRMED
        self._message = "登录成功"
        self._qr = ""
        logger.info("[WeReadQR] 登录成功, vid=%s", self._vid or "?")

    async def _activate_cookie(self, jar: Dict[str, str], vid: str,
                               refresh_token: str) -> Tuple[str, str]:
        """把登录下发的 Cookie 变成一份真正能用的。

        对每个候选：先直接验证；不行就用 wr_rt 续期换新的 wr_skey 再验证。
        返回 (可用 Cookie, 失败说明)。
        """
        last_error = ""
        for candidate in build_cookie_candidates(jar, vid, refresh_token):
            ok, message = await self._verify(candidate)
            if ok:
                return candidate, ""
            last_error = message

            if not parse_cookie(candidate).get("wr_rt"):
                continue
            try:
                renewed = await renew_cookie_value(candidate)
            except WereadError as exc:
                last_error = exc.user_message
                continue
            ok, message = await self._verify(renewed)
            if ok:
                logger.info("[WeReadQR] 登录 Cookie 经续期后可用")
                return renewed, ""
            last_error = message

        return "", last_error

    @staticmethod
    async def _verify(cookie: str) -> Tuple[bool, str]:
        try:
            # allow_renew=False：这一步只是在挑能用的 Cookie，
            # 不能让验证过程顺手把中间结果写进存储
            async with WereadClient(cookie=cookie, allow_renew=False) as client:
                return await client.verify()
        except WereadError as exc:
            return False, exc.user_message
        except Exception as exc:
            return False, f"验证请求失败: {exc}"


weread_qr_login = WereadQRLogin()
