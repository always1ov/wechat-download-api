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


频率是怎么定的（关键，别随手改小）
----------------------------------

公开资料里 `wr_skey` 的有效期约 **5400 秒（1.5 小时）**，过期后接口判 401 /
`-2012`。照着这个数字，「让 skey 永不过期」需要每小时续一次，一天 24 次 ——
这个方向是错的，理由有三：

1. **没必要。** 续期本来就是按需触发的：任何请求撞上 -2012 都会当场续期并
   重试，用户无感。守护不是为了「让 skey 永远新鲜」，而是为了**尽早发现
   `wr_rt` 也死了**这种人工才能解决的情况。这种事一个月未必遇到一次，
   6 小时内发现完全够。
2. **反而更危险。** 微信读书的风控除了看请求频率，还看**请求规律性** ——
   严格等间隔的请求本身就是机器特征。所以这里不但不压缩间隔，还给它加了
   ±15% 的抖动。
3. **重复。** 轮询器默认一小时跑一轮，本来就在持续证明登录态是活的。
   守护再定时打一次接口纯属重复，所以下面有 `_recently_proven_alive()`：
   最近有真实请求成功过就直接跳过这一轮，正常运行时守护几乎不产生额外流量。

作为参照，同类项目（如 weread2notion-pro）的同步频率是 2~3 小时一次且长期
在跑。本项目守护一次只打一个 `/web/shelf/sync`，比那轻得多。

真正需要担心的封号风险不在这儿，而在**采集频率**（`RSS_POLL_INTERVAL`、
`WEREAD_CONTENT_INTERVAL`）以及**多端同时登录同一账号**（会互相踢掉登录态）。

只做「维持」，不碰采集：守护任务不会去拉文章，单次开销就是一个
/web/shelf/sync 加上可能的一次续期。
"""

import asyncio
import logging
import os
import random
import time
from typing import Dict, Optional

from utils import weread_client
from utils.webhook import webhook

logger = logging.getLogger(__name__)

# 6 小时查一次。wr_skey 只活 1.5 小时，但按需续期本来就兜得住；
# 守护要抓的是「wr_rt 也死了」这种人工才能解决的事，6 小时内发现足够。
DEFAULT_INTERVAL = 6 * 3600
# 间隔抖动 ±15%：严格等间隔的请求本身就是机器特征，别给风控送把柄
INTERVAL_JITTER = 0.15
# 最近这么久内有真实请求成功过，就跳过这一轮探活 ——
# 采集已经在证明登录态是活的，不必再额外打一次接口。
# 取 1 小时是因为轮询器默认就是一小时一轮，正好错开。
SKIP_IF_ALIVE_WITHIN = 3600
# 启动后先等一会儿再查：让应用先把端口起好，别和初始化抢资源
STARTUP_DELAY = 30.0
# 连续失败几次之后才认定「真的需要重新扫码」，避免一次网络抖动就报警
FAILURES_BEFORE_ALARM = 2
# 间隔下限。低于这个值对「发现 wr_rt 失效」没有任何帮助，只是徒增请求。
MIN_INTERVAL = 1800


def keepalive_enabled() -> bool:
    """是否开启登录态守护。默认开。"""
    raw = os.getenv("WEREAD_KEEPALIVE", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def keepalive_interval() -> int:
    """检查间隔（秒）。

    兜底不低于 MIN_INTERVAL（30 分钟）：守护要发现的是 `wr_rt` 失效，
    查得再勤也不会更早发现，只会平白多打接口、还把请求节奏做得更像机器。
    """
    try:
        value = int(os.getenv("WEREAD_KEEPALIVE_INTERVAL", str(DEFAULT_INTERVAL)))
    except ValueError:
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, value)


def _next_delay() -> float:
    """下一轮的等待时间，带 ±15% 抖动，避免固定节奏被当成机器。"""
    base = keepalive_interval()
    return base * random.uniform(1 - INTERVAL_JITTER, 1 + INTERVAL_JITTER)


def _recently_proven_alive() -> bool:
    """最近有真实请求成功过吗？有的话这一轮就不用自己去探活了。"""
    last = weread_client.last_success_at()
    return bool(last) and (time.time() - last) < SKIP_IF_ALIVE_WITHIN


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
        self._skipped = 0                  # 因为「真实流量刚验证过」而跳过的轮数

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
            "skipped": self._skipped,
            "skip_if_alive_within": SKIP_IF_ALIVE_WITHIN,
            "last_traffic_ok_at": int(weread_client.last_success_at()),
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
                await asyncio.sleep(_next_delay())
        except asyncio.CancelledError:
            raise

    async def check_once(self, force: bool = False) -> Dict:
        """查一次登录态，坏了就续。返回本次的状态摘要。

        `force=False`（后台轮到的那次）时，如果最近有真实请求成功过就直接跳过 ——
        采集本身已经证明登录态是活的，没必要再打一次接口。
        手动触发（force=True）不跳，用户点了就得真查。
        """
        if not force and _recently_proven_alive():
            self._skipped += 1
            self._last_check_ok = True
            self._failures = 0
            self._message = "最近的采集请求刚验证过登录态，本轮跳过"
            logger.debug("[WeReadKeeper] 登录态刚被真实流量验证过，跳过本轮探活")
            return self.status()

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
