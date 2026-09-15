#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
微信读书登录态守护 —— 主动把登录维持住，而不是等它坏了再补救。

原来只有「被动续期」：某个请求撞上 -2012，才顺手续一次。够用，但有三个洞：

1. 容器刚起、或长时间没人调接口时，第一发请求必然先失败一次再续期 ——
   轮询器一小时才跑一轮，这一次失败就是一小时的空窗；
2. `wr_rt` 自己也会过期。它一死，续期就再也成功不了，而用户完全不知道，
   只会发现「RSS 怎么不更新了」，翻日志才查得到；
3. 没有任何可观测的状态：上次检查什么时候、还能不能用、要不要重新扫码。

所以这里加一个后台守护：
- 启动后先查一次（默认 30 秒后，避开启动高峰），之后每 6 小时查一次；
- 发现登录态坏了就主动续期，续完再验一次，确认真的活了；
- 续不回来（多半是 `wr_rt` 也死了）就发 webhook 报警，并在状态里标出
  `needs_relogin`，管理页会挂出横幅提示去重新扫码；
- 恢复正常时再发一条，免得用户以为还挂着。

只做「维持」，不碰采集：守护任务不会去拉文章，单次开销就是一个
/web/shelf/sync 加上可能的一次续期。
"""

import asyncio
import logging
import os
import time
from typing import Dict, Optional

from utils import weread_client
from utils.webhook import webhook

logger = logging.getLogger(__name__)

# 6 小时查一次：wr_skey 的寿命远短于此，但被动续期本来就兜得住，
# 守护的意义是「别让失败发生在用户面前」，不是把间隔压到极限。
DEFAULT_INTERVAL = 6 * 3600
# 启动后先等一会儿再查：让应用先把端口起好，别和初始化抢资源
STARTUP_DELAY = 30.0
# 连续失败几次之后才认定「真的需要重新扫码」，避免一次网络抖动就报警
FAILURES_BEFORE_ALARM = 2


def keepalive_enabled() -> bool:
    """是否开启登录态守护。默认开。"""
    raw = os.getenv("WEREAD_KEEPALIVE", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def keepalive_interval() -> int:
    """检查间隔（秒）。太小没意义还费配额，这里兜底不低于 5 分钟。"""
    try:
        value = int(os.getenv("WEREAD_KEEPALIVE_INTERVAL", str(DEFAULT_INTERVAL)))
    except ValueError:
        return DEFAULT_INTERVAL
    return max(300, value)


class WereadKeeper:
    """登录态守护任务。单例，随应用生命周期起停。"""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_check_at = 0.0          # epoch 秒，0 表示还没查过
        self._last_check_ok: Optional[bool] = None
        self._last_renew_at = 0.0
        self._last_renew_ok: Optional[bool] = None
        self._failures = 0
        self._message = ""
        self._alarmed = False              # 已经报过警，别每轮都刷

    # --- 生命周期 ---

    async def start(self) -> None:
        if self._running or not keepalive_enabled():
            if not keepalive_enabled():
                logger.info("[WeReadKeeper] WEREAD_KEEPALIVE=false，登录态守护未启动")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[WeReadKeeper] 登录态守护已启动（间隔 %d 秒）", keepalive_interval())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("[WeReadKeeper] 登录态守护已停止")

    # --- 状态 ---

    @property
    def needs_relogin(self) -> bool:
        """续不回来了，得人工重新扫码。"""
        return self._failures >= FAILURES_BEFORE_ALARM

    def status(self) -> Dict:
        """给 /api/weread/status 和管理页用的状态摘要。"""
        interval = keepalive_interval()
        next_at = int(self._last_check_at + interval) if self._last_check_at else 0
        return {
            "enabled": keepalive_enabled(),
            "running": self._running,
            "interval": interval,
            "last_check_at": int(self._last_check_at),
            "last_check_ok": self._last_check_ok,
            "last_renew_at": int(self._last_renew_at),
            "last_renew_ok": self._last_renew_ok,
            "next_check_at": next_at,
            "failures": self._failures,
            "needs_relogin": self.needs_relogin,
            "message": self._message,
        }

    # --- 内部 ---

    async def _loop(self) -> None:
        try:
            await asyncio.sleep(STARTUP_DELAY)
            while self._running:
                try:
                    await self.check_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:               # 守护任务不能被任何异常带走
                    logger.error("[WeReadKeeper] 检查异常: %s", exc)
                await asyncio.sleep(keepalive_interval())
        except asyncio.CancelledError:
            raise

    async def check_once(self) -> Dict:
        """查一次登录态，坏了就续。返回本次的状态摘要。"""
        self._last_check_at = time.time()

        if not weread_client.is_enabled():
            self._last_check_ok = False
            self._message = "还没配微信读书 Cookie，先去 /login.html 扫码"
            # 没登录过不算「续期失败」，不报警也不累加失败次数 ——
            # 那是还没开始用，不是坏了
            self._failures = 0
            return self.status()

        ok, message = await self._verify()
        if ok:
            self._on_healthy(message)
            return self.status()

        logger.info("[WeReadKeeper] 登录态异常（%s），尝试续期", message)
        renewed, renew_msg = await self._renew()
        self._last_renew_at = time.time()
        self._last_renew_ok = renewed

        if renewed:
            # 续完必须再验一次 —— 续期接口回 200 不等于新 skey 真能用
            ok, message = await self._verify()
            if ok:
                self._on_healthy("已自动续期并验证通过")
                logger.info("[WeReadKeeper] 自动续期成功")
                return self.status()
            message = f"续期后仍然无效：{message}"
        else:
            message = renew_msg or message

        self._on_broken(message)
        return self.status()

    async def _verify(self):
        try:
            # allow_renew=False：这一步只是「探活」，续期由下面显式走，
            # 否则探活自己把 skey 续了，失败次数就永远统计不准
            async with weread_client.WereadClient(allow_renew=False) as client:
                return await client.verify()
        except weread_client.WereadError as exc:
            return False, exc.user_message
        except Exception as exc:
            return False, f"检查请求失败: {exc}"

    async def _renew(self):
        if not weread_client.auto_renew():
            return False, "WEREAD_AUTO_RENEW=false，未自动续期"
        try:
            async with weread_client.WereadClient(allow_renew=False) as client:
                await client.renew_cookie()
            return True, ""
        except weread_client.WereadError as exc:
            return False, f"续期失败：{exc.user_message}"
        except Exception as exc:
            return False, f"续期请求失败: {exc}"

    def _on_healthy(self, message: str) -> None:
        recovered = self._alarmed
        self._last_check_ok = True
        self._failures = 0
        self._message = message
        self._alarmed = False
        if recovered:
            logger.info("[WeReadKeeper] 登录态已恢复")
            asyncio.create_task(self._notify("login_success", {
                "说明": "微信读书登录态已恢复正常",
            }))

    def _on_broken(self, message: str) -> None:
        self._last_check_ok = False
        self._failures += 1
        self._message = message
        logger.warning("[WeReadKeeper] 登录态维持失败（第 %d 次）：%s",
                       self._failures, message)
        if self.needs_relogin and not self._alarmed:
            self._alarmed = True
            asyncio.create_task(self._notify("login_expired", {
                "原因": message,
                "怎么办": "打开 /login.html 用微信重新扫码登录",
            }))

    @staticmethod
    async def _notify(event: str, data: Dict) -> None:
        try:
            await webhook.notify(event, data)
        except Exception as exc:
            logger.debug("[WeReadKeeper] webhook 发送失败: %s", exc)


weread_keeper = WereadKeeper()
