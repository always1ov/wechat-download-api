#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
微信读书（weread.qq.com）采集通道 —— 公众号后台不可用时的备用数据源。

为什么需要它：公众号后台（mp.weixin.qq.com/cgi-bin）的凭证约 4 天过期，
appmsgpublish 有频率风控，正文页直抓还会触发验证码；一旦踩中，轮询器和
/api/article 都只能静默失败。微信读书 Web 端用自己的一套登录态
（wr_skey / wr_vid / wr_rt），能读到同样的公众号文章列表与正文，且不共享
公众号后台的风控额度，因此可以在后台通道失效时顶上。

接口来源：rachelos/we-mp-rss#442
- POST /mp/shelf/addToShelf   订阅公众号（加入书架）
- GET  /web/shelf/bookIds     查询是否已订阅
- GET  /web/mp/articles       文章列表（offset 分页）
- GET  /api/mp/cover          仅最新一篇（列表接口被风控时兜底）
- GET  /web/mp/content        文章正文 HTML

标识换算：
    bookId   = MP_WXS_<fakeid 的 base64 解码>   例：MzI5NjM4MjExMg== → MP_WXS_3296382112
    reviewId = <bookId>_<文章短链 token>         例：MP_WXS_3296382112_AbC~dEf
    原文链接 = https://mp.weixin.qq.com/s/<文章短链 token>
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx

logger = logging.getLogger(__name__)

WEREAD_BASE = "https://weread.qq.com"
# i 域是微信读书 App 的接口域，能力比网页域多（可以搜公众号）。
# 注意：它只认 App 的 UA，也只认真正有效的 wr_skey —— 网页登录直接下发的短值会被
# 401 顶回来，必须先经 /web/login/renewal 续期。
WEREAD_APP_BASE = "https://i.weread.qq.com"
MP_BOOK_PREFIX = "MP_WXS_"

# 微信读书的鉴权/风控错误码。重试没有意义：要么 Cookie 过期要么被限流，
# 必须让用户重新贴 Cookie（或退回公众号后台通道）。
AUTH_ERROR_CODES = (-2010, -2012, -2041)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 微信读书 Android App 的 UA。i 域按 App 客户端校验，用浏览器 UA 会被拒。
APP_USER_AGENT = (
    "WeRead/9.3.1 WRBrand/vivo Dalvik/2.1.0 "
    "(Linux; U; Android 14; V2171A Build/UP1A.231005.007)"
)

COOKIE_MISSING_MSG = "还没登录微信读书，请打开 /login.html 扫码登录（或设置 WEREAD_COOKIE 环境变量）"
# 两种「过期」要分开说：没有 wr_rt 是压根续不了，跟续了但没续回来是两回事，
# 前者用户再等也不会自愈，得知道必须重新扫码。
COOKIE_EXPIRED_MSG = "微信读书登录已过期，自动续期没能救回来，请打开 /login.html 重新扫码"
COOKIE_NO_REFRESH_MSG = (
    "微信读书登录已过期，而且当前登录态里没有 wr_rt（长期令牌），"
    "无法自动续期 —— 请打开 /login.html 重新扫码登录"
)

# 文章短链 token 里的 '~' 是合法字符（如 4OcS7~rrtk2Lwe4P0YPiGg），
# 转义成 %7E 会让微信 302 跳转、部分阅读器打不开，必须原样保留。
_TOKEN_SAFE_CHARS = "~"


# ── 配置 ─────────────────────────────────────────────────

def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return max(float(_env_str(name) or default), 0.0)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(_env_str(name) or default), 1)
    except ValueError:
        return default


def content_interval() -> float:
    """正文请求最小间隔（秒）。微信读书对正文接口限流较严，默认 2s。"""
    return _env_float("WEREAD_CONTENT_INTERVAL", 2.0)


def page_interval() -> float:
    """列表翻页最小间隔（秒）。"""
    return _env_float("WEREAD_PAGE_INTERVAL", 1.0)


def max_pages() -> int:
    """单次列表采集最多翻几页（每页约 50 条）。"""
    return _env_int("WEREAD_MAX_PAGES", 5)


def auto_add_to_shelf() -> bool:
    """是否自动把公众号加入微信读书书架。

    微信读书只对**书架上**的公众号返回文章列表，所以默认开启；
    介意改动书架的用户可以设 WEREAD_AUTO_ADD_SHELF=false 后手动关注。
    """
    return _env_bool("WEREAD_AUTO_ADD_SHELF", True)


def user_agent() -> str:
    """请求用的 UA，可用 WEREAD_USER_AGENT 覆盖。"""
    return _env_str("WEREAD_USER_AGENT") or DEFAULT_USER_AGENT


def app_api_enabled() -> bool:
    """是否启用 i 域（App 接口）。关掉就只走网页域，行为回到接入 i 域之前。"""
    return _env_bool("WEREAD_APP_API", True)


def app_user_agent() -> str:
    return _env_str("WEREAD_APP_USER_AGENT") or APP_USER_AGENT


def auto_renew() -> bool:
    """wr_skey 失效时是否自动用 wr_rt 续期。"""
    return _env_bool("WEREAD_AUTO_RENEW", True)


def is_enabled() -> bool:
    """微信读书通道是否可用。

    WEREAD_ENABLED 显式开/关；不设则按「配了 Cookie 就启用」自动判断。
    """
    raw = _env_str("WEREAD_ENABLED").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool(weread_auth.get_cookie())


# ── 错误 ─────────────────────────────────────────────────

class WereadError(Exception):
    """微信读书接口错误。code 可能是接口 errCode、HTTP 状态码或内部标识字符串。"""

    def __init__(self, code, message: str, retriable: bool = True):
        super().__init__(f"WeRead error {code}: {message}")
        self.code = code
        self.message = message
        self.retriable = retriable

    @property
    def is_auth_error(self) -> bool:
        return self.code in AUTH_ERROR_CODES or self.code == "missing_cookie"

    @property
    def user_message(self) -> str:
        """给终端用户看的中文提示。"""
        if self.code == "missing_cookie":
            return COOKIE_MISSING_MSG
        if self.code in AUTH_ERROR_CODES:
            # 没有 wr_rt 时再怎么等也不会自愈，直接说清楚，别让用户干等
            return COOKIE_EXPIRED_MSG if weread_auth.has_refresh_token() else COOKIE_NO_REFRESH_MSG
        # 字符串 code 是本地判定出来的问题（缺字段、解析不出正文等），
        # message 本身就是完整的中文说明，再套一层「接口失败」反而绕。
        # network_error 例外：它的 message 是原始异常文本，需要前缀点题。
        if isinstance(self.code, str) and self.code != "network_error":
            return self.message
        return f"微信读书接口失败: {self.message}"


# ── 标识换算 ──────────────────────────────────────────────

def parse_cookie(cookie: str) -> Dict[str, str]:
    """Cookie 串 → 字段字典（保持原顺序）。"""
    pairs: Dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            pairs[key] = value.strip()
    return pairs


def format_cookie(pairs: Dict[str, str]) -> str:
    """字段字典 → Cookie 串（丢掉空值）。"""
    return "; ".join(f"{k}={v}" for k, v in pairs.items() if v)


def fakeid_to_book_id(fakeid: str) -> str:
    """fakeid（base64）→ 微信读书 bookId。已经是 bookId 或纯数字 biz 时原样/补前缀。"""
    value = str(fakeid or "").strip()
    if not value:
        raise WereadError("invalid_fakeid", "fakeid 为空", retriable=False)
    if value.startswith(MP_BOOK_PREFIX):
        return value
    if value.isdigit():
        return MP_BOOK_PREFIX + value
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", "ignore").strip()
    except Exception as exc:  # binascii.Error 等
        raise WereadError(
            "invalid_fakeid", f"无法从 fakeid 推导 bookId: {value}", retriable=False
        ) from exc
    # b64decode 对非法输入不一定报错，只会解出乱码 —— 用「解码后必须是十进制 biz」兜住
    if not decoded.isdigit():
        raise WereadError(
            "invalid_fakeid", f"无法从 fakeid 推导 bookId: {value}", retriable=False
        )
    return MP_BOOK_PREFIX + decoded


def book_id_to_fakeid(book_id: str) -> str:
    """微信读书 bookId → 公众号 fakeid（base64），与 fakeid_to_book_id 互逆。"""
    value = str(book_id or "").strip()
    if not value.startswith(MP_BOOK_PREFIX):
        return value
    biz = value[len(MP_BOOK_PREFIX):]
    return base64.b64encode(biz.encode("utf-8")).decode("ascii")


def build_mp_url(article_token: str) -> str:
    """文章短链 token → mp.weixin.qq.com 原文链接。"""
    token = str(article_token or "").strip()
    if not token:
        return ""
    if token.startswith("http://") or token.startswith("https://"):
        return token
    return f"https://mp.weixin.qq.com/s/{quote(token, safe=_TOKEN_SAFE_CHARS)}"


def review_id_to_mp_url(review_id: str, book_id: str = "") -> str:
    """reviewId → 原文链接。reviewId 形如 <bookId>_<token>，末段即短链 token。"""
    value = str(review_id or "").strip()
    if not value:
        return ""
    token = value
    prefix = f"{book_id}_" if book_id else ""
    if prefix and value.startswith(prefix):
        token = value[len(prefix):]
    elif "_" in token:
        token = token.split("_")[-1]
    return build_mp_url(token)


def extract_article_token(url: str) -> str:
    """从 https://mp.weixin.qq.com/s/<token> 取出短链 token；长链（/s?__biz=...）返回空。"""
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        path = urlparse(value).path
    except ValueError:
        return ""
    match = re.match(r"^/s/([^/?#]+)", path)
    return match.group(1) if match else ""


def build_review_id(fakeid_or_book_id: str, article_token: str) -> str:
    """(fakeid|bookId, 短链 token) → reviewId。"""
    token = str(article_token or "").strip()
    if not token:
        return ""
    return f"{fakeid_to_book_id(fakeid_or_book_id)}_{token}"


def review_id_from_article_url(url: str, fakeid_or_book_id: str) -> str:
    """(原文短链, fakeid|bookId) → reviewId；无法推导时返回空串。"""
    token = extract_article_token(url)
    if not token:
        return ""
    try:
        return build_review_id(fakeid_or_book_id, token)
    except WereadError:
        return ""


# ── Cookie 存储 ───────────────────────────────────────────

class WereadAuth:
    """微信读书 Cookie 存储。

    优先级与 AuthManager 一致：环境变量 > data/.weread.json。
    环境变量存在时视为「部署配置托管」，管理页写入不覆盖它。
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.base_dir = Path(__file__).parent.parent
        self.credentials_file = self.base_dir / "data" / ".weread.json"
        self._cache: Dict = {}
        self._last_loaded_at = 0.0
        self._load_ttl = 30.0
        # 续期换来的新 Cookie：优先级最高，连 WEREAD_COOKIE 环境变量也盖过。
        # 环境变量里那份此时已经是过期的 wr_skey，继续用它只会一直 -2012。
        self._runtime_cookie = ""
        self._initialized = True

    # --- 内部 ---

    @staticmethod
    def normalize_cookie(cookie: str) -> str:
        """折叠换行/多余空白 —— 用户从浏览器复制的 Cookie 常带换行。"""
        return re.sub(r"\s+", " ", str(cookie or "")).strip().strip(";").strip()

    @staticmethod
    def extract_vid(cookie: str) -> str:
        match = re.search(r"wr_vid=(\d+)", cookie or "")
        return match.group(1) if match else ""

    def _load(self, force: bool = False) -> Dict:
        now = time.time()
        if not force and (now - self._last_loaded_at) < self._load_ttl:
            return self._cache
        self._last_loaded_at = now
        data = {}
        if self.credentials_file.exists():
            try:
                with open(self.credentials_file, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception as e:
                logger.warning("读取微信读书凭证失败 %s: %s", self.credentials_file, e)
                data = {}
        self._cache = data if isinstance(data, dict) else {}
        return self._cache

    # --- 对外 ---

    def get_cookie(self) -> str:
        """当前生效的 Cookie：续期结果 > 环境变量 > data/.weread.json。"""
        if self._runtime_cookie:
            return self._runtime_cookie
        env_cookie = self.normalize_cookie(os.getenv("WEREAD_COOKIE", ""))
        if env_cookie:
            return env_cookie
        return self.normalize_cookie(self._load().get("cookie", ""))

    def set_runtime_cookie(self, cookie: str) -> None:
        """记下续期后的 Cookie。

        内存里立刻生效；非环境变量托管时同时落盘，重启后不用重新续期。
        环境变量托管的情况只留在内存 —— 改不了别人的部署配置。
        """
        normalized = self.normalize_cookie(cookie)
        if not normalized:
            return
        self._runtime_cookie = normalized
        if self.is_env_managed():
            return
        payload = dict(self._load() or {})
        payload.update({
            "cookie": normalized,
            "vid": self.extract_vid(normalized) or payload.get("vid", ""),
            "renewed_at": int(time.time()),
        })
        try:
            self.credentials_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.credentials_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("续期后的微信读书 Cookie 落盘失败（内存仍生效）: %s", e)
            return
        self._cache = payload
        self._last_loaded_at = time.time()

    def has_refresh_token(self) -> bool:
        """当前登录态里有没有 wr_rt（长期 refreshToken）。

        没有它，wr_skey 一过期就只能重新扫码 —— 自动维护在这种情况下
        无论如何都救不回来，属于配置问题而不是故障。
        """
        return bool(parse_cookie(self.get_cookie()).get("wr_rt"))

    def is_env_managed(self) -> bool:
        return bool(self.normalize_cookie(os.getenv("WEREAD_COOKIE", "")))

    def is_configured(self) -> bool:
        return bool(self.get_cookie())

    def save_cookie(self, cookie: str) -> Tuple[bool, str]:
        """保存 Cookie 到 data/.weread.json。返回 (是否成功, 提示)。"""
        normalized = self.normalize_cookie(cookie)
        if not normalized:
            return False, "Cookie 不能为空"
        # 只认微信读书自己的登录字段，挡住粘错域名（如公众号后台 Cookie）的常见误操作
        if not re.search(r"\bwr_(skey|rt|vid)=", normalized):
            return False, "Cookie 格式不正确：未找到 wr_skey / wr_rt / wr_vid 字段"
        payload = {
            "cookie": normalized,
            "vid": self.extract_vid(normalized),
            "updated_at": int(time.time()),
        }
        try:
            self.credentials_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.credentials_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error("保存微信读书凭证失败: %s", e)
            return False, f"保存失败: {e}"
        self._cache = payload
        self._last_loaded_at = time.time()
        self._runtime_cookie = ""   # 用户手动换了 Cookie，旧的续期结果作废
        reset_shelf_cache()
        if self.is_env_managed():
            return True, "已保存，但 WEREAD_COOKIE 环境变量优先生效，需改环境变量才会生效"
        return True, "微信读书 Cookie 已保存"

    def clear(self) -> bool:
        try:
            if self.credentials_file.exists():
                self.credentials_file.unlink()
        except Exception as e:
            logger.warning("删除微信读书凭证失败: %s", e)
            return False
        self._cache = {}
        self._last_loaded_at = time.time()
        self._runtime_cookie = ""
        reset_shelf_cache()
        return True

    def get_info(self) -> Dict:
        """状态摘要（不回传完整 Cookie）。"""
        cookie = self.get_cookie()
        stored = self._load()
        return {
            "configured": bool(cookie),
            "enabled": is_enabled(),
            "env_managed": self.is_env_managed(),
            "vid": self.extract_vid(cookie),
            # 没有它就没法自动续期，这是「维持不住」最常见的根因，
            # 得让前端在还没坏的时候就能提醒用户
            "has_wr_rt": self.has_refresh_token(),
            "cookie_preview": (cookie[:12] + "..." + cookie[-6:]) if len(cookie) > 24 else ("***" if cookie else ""),
            "updated_at": int(stored.get("updated_at", 0) or 0),
        }


weread_auth = WereadAuth()


# ── 请求限速 ──────────────────────────────────────────────
# 进程级串行 + 最小间隔：多个轮询任务并发时也不会把微信读书打到限流。

_throttle_lock = asyncio.Lock()
_last_request_at = 0.0


async def _throttle(interval: float):
    global _last_request_at
    if interval <= 0:
        return
    async with _throttle_lock:
        wait = interval - (time.monotonic() - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()


# 续期：进程内串行 + 冷却，避免多个轮询任务同时去打续期接口
_renew_lock = asyncio.Lock()
_last_renew_at = 0.0
_last_renew_ok = False

# 最近一次请求成功的时刻。守护任务靠它判断「登录态刚被真实流量验证过」，
# 从而跳过自己那一次探活 —— 采集本身就在证明登录态是活的，
# 再额外打一次接口纯属给微信读书添无谓的请求。
_last_success_at = 0.0


def last_success_at() -> float:
    """最近一次微信读书请求成功的 epoch 秒；0 表示本次启动以来还没成功过。"""
    return _last_success_at


def _mark_success() -> None:
    global _last_success_at
    _last_success_at = time.time()
RENEW_COOLDOWN = 30.0


# i 域可用性：探测失败后冷却一段时间，别每次请求都去撞一次
_app_domain_blocked_until = 0.0
APP_DOMAIN_COOLDOWN = 600.0


def app_domain_usable() -> bool:
    return app_api_enabled() and time.monotonic() >= _app_domain_blocked_until


def mark_app_domain_unusable(reason: str = ""):
    """i 域打不通（401/风控等）时记一笔，冷却期内不再尝试。"""
    global _app_domain_blocked_until
    _app_domain_blocked_until = time.monotonic() + APP_DOMAIN_COOLDOWN
    logger.info("[WeRead] i 域暂不可用（%s），%.0f 秒内只走网页域",
                reason or "unknown", APP_DOMAIN_COOLDOWN)


# 已确认在书架上的 bookId，避免每轮轮询都重复 addToShelf
_shelf_confirmed: set = set()


def mark_on_shelf(book_id: str):
    """记下某个号确认在书架上，省掉后续多余的查询/添加请求。"""
    if book_id:
        _shelf_confirmed.add(book_id)


def reset_shelf_cache():
    """换 Cookie（换账号）后书架状态、续期冷却、i 域探测结果都不再可信，一起清掉。"""
    global _last_renew_at, _last_renew_ok, _app_domain_blocked_until
    _shelf_confirmed.clear()
    _last_renew_at = 0.0
    _last_renew_ok = False
    _app_domain_blocked_until = 0.0


# ── 响应解析 ──────────────────────────────────────────────

def raise_for_payload(payload: Dict):
    """按 errCode 抛 WereadError；errCode 为 0/缺省表示成功。"""
    if not isinstance(payload, dict):
        raise WereadError("invalid_response", "响应不是 JSON 对象")
    raw_code = payload.get("errCode", payload.get("errcode", 0))
    try:
        code = int(raw_code or 0)
    except (TypeError, ValueError):
        code = 0
    if code == 0:
        return
    message = payload.get("errMsg") or payload.get("errmsg") or str(code)
    raise WereadError(code, message, retriable=code not in AUTH_ERROR_CODES)


def parse_mp_articles(payload: Dict, book_id: str = "") -> Tuple[List[Dict], int]:
    """解析 /web/mp/articles 响应。

    返回 (文章列表, 顶层分组数)。分组数就是 offset 的步长 —— 微信读书的
    offset 按顶层 reviews 分组计，不是按文章条数计，翻页时必须用它累加。
    """
    raise_for_payload(payload)
    groups = payload.get("reviews") or []
    if not isinstance(groups, list):
        return [], 0

    articles = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_time = group.get("createTime", 0)
        # 列表接口两种形态都见过：分组（subReviews）和平铺（直接挂 review）
        sub_reviews = group.get("subReviews")
        if not isinstance(sub_reviews, list) or not sub_reviews:
            sub_reviews = [group]
        for sub_review in sub_reviews:
            if not isinstance(sub_review, dict):
                continue
            article = _parse_review(sub_review, group_time, book_id)
            if article:
                articles.append(article)
    return articles, len(groups)


def _parse_review(sub_review: Dict, group_time: int, book_id: str) -> Optional[Dict]:
    review = sub_review.get("review")
    if not isinstance(review, dict):
        review = sub_review
    mp_info = review.get("mpInfo")
    if not isinstance(mp_info, dict):
        mp_info = {}

    review_id = review.get("reviewId") or sub_review.get("reviewId")
    if not review_id:
        return None

    create_time = int(review.get("createTime") or group_time or 0)
    update_time = int(mp_info.get("time") or create_time or 0)
    link = build_mp_url(mp_info.get("originalId", "")) or review_id_to_mp_url(review_id, book_id)

    return {
        "aid": review_id,
        "review_id": review_id,
        "title": mp_info.get("title", ""),
        "link": link,
        "digest": mp_info.get("content") or review.get("content", "") or "",
        "cover": mp_info.get("pic_url", "") or "",
        "author": mp_info.get("author") or mp_info.get("mpName") or "",
        "publish_time": update_time or create_time,
        "create_time": create_time,
        "update_time": update_time or create_time,
        "read_num": mp_info.get("readNum", 0),
        "like_num": mp_info.get("likeNum", 0),
        "source": "weread",
    }


def parse_mp_cover(payload: Dict, book_id: str) -> Optional[Dict]:
    """解析 /api/mp/cover 响应（只含最新一篇）。"""
    if not isinstance(payload, dict):
        return None
    review_id = str(payload.get("reviewId") or "").strip()
    if not review_id:
        raise_for_payload(payload)
        raise WereadError(
            "empty_cover", "该公众号在微信读书没有可见文章", retriable=False
        )
    now = int(time.time())
    return {
        "aid": review_id,
        "review_id": review_id,
        "title": payload.get("title", "") or "",
        "link": review_id_to_mp_url(review_id, book_id),
        "digest": payload.get("digest", "") or "",
        "cover": payload.get("pic", "") or payload.get("cover", "") or "",
        "author": payload.get("name", "") or "",
        # cover 接口不带发布时间，只能退回抓取时间
        "publish_time": now,
        "create_time": now,
        "update_time": now,
        "source": "weread",
    }


def _unwrap_book(item) -> Optional[Dict]:
    """搜索结果的一项可能是书本身，也可能包在 bookInfo 里。"""
    if not isinstance(item, dict):
        return None
    inner = item.get("bookInfo")
    return inner if isinstance(inner, dict) else item


def parse_store_search(payload) -> List[Dict]:
    """解析 i 域 /store/search，挑出其中的公众号（bookId 以 MP_WXS_ 开头）。

    微信读书的搜索结果里书和公众号混在一起，只有 MP_WXS_* 才是公众号；
    换算回 fakeid 后就能直接喂给本项目既有的订阅/文章接口。
    """
    if not isinstance(payload, dict):
        return []
    raise_for_payload(payload)

    items = None
    for key in ("books", "mpInfos", "data", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            items = value
            break
    if items is None:
        return []

    accounts, seen = [], set()
    for item in items:
        book = _unwrap_book(item)
        if not book:
            continue
        book_id = str(book.get("bookId") or "").strip()
        if not book_id.startswith(MP_BOOK_PREFIX) or book_id in seen:
            continue
        seen.add(book_id)
        try:
            fakeid = book_id_to_fakeid(book_id)
        except Exception:
            continue
        accounts.append({
            "fakeid": fakeid,
            "book_id": book_id,
            "nickname": book.get("title") or book.get("mpName") or "",
            "alias": book.get("author") or "",
            "round_head_img": book.get("cover") or book.get("pic") or "",
            "service_type": 0,
            "source": "weread",
        })
    return accounts


def parse_app_articles(payload: Dict, book_id: str = "") -> Tuple[List[Dict], int]:
    """解析 i 域 /book/articles。

    这个接口和网页域的 /web/mp/articles 返回形态不完全一样：见过 reviews 分组，
    也见过直接平铺的文章数组。两种都认，拿不准的字段交给 _parse_review 兜。
    """
    if not isinstance(payload, dict):
        raise WereadError("invalid_response", "文章列表不是 JSON 对象")
    raise_for_payload(payload)

    if isinstance(payload.get("reviews"), list):
        return parse_mp_articles(payload, book_id)

    items = None
    for key in ("articles", "updated", "mpArticles", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            items = value
            break
    if items is None:
        return [], 0

    articles = []
    for item in items:
        article = _parse_review(item, 0, book_id)
        if article:
            articles.append(article)
    return articles, len(items)


def parse_shelf_accounts(payload) -> List[Dict]:
    """从 /web/shelf/sync 里挑出公众号。

    书架上书和公众号混在一起，bookId 以 MP_WXS_ 开头的才是公众号。
    这条路的价值在于：完全不需要公众号后台 —— 用户在微信读书 App 里关注了谁，
    这里就能列出谁，换算回 fakeid 后可直接订阅。
    """
    if not isinstance(payload, dict):
        return []
    raise_for_payload(payload)

    books = payload.get("books")
    if not isinstance(books, list):
        return []

    accounts, seen = [], set()
    for item in books:
        if not isinstance(item, dict):
            continue
        book_id = str(item.get("bookId") or "").strip()
        if not book_id.startswith(MP_BOOK_PREFIX) or book_id in seen:
            continue
        seen.add(book_id)
        try:
            fakeid = book_id_to_fakeid(book_id)
        except Exception:
            continue
        accounts.append({
            "fakeid": fakeid,
            "book_id": book_id,
            "nickname": item.get("title", "") or "",
            "alias": item.get("author", "") or "",
            "round_head_img": item.get("cover", "") or "",
            "intro": item.get("intro", "") or "",
            "update_time": item.get("updateTime", 0) or 0,
            "service_type": 0,
            "source": "weread_shelf",
        })
    return accounts


def parse_shelf_book_ids(payload) -> Optional[List[str]]:
    """解析 /web/shelf/bookIds 响应，取出已订阅的 bookId 列表。无法识别返回 None。"""
    if isinstance(payload, list):
        return [str(x) for x in payload]
    if not isinstance(payload, dict):
        return None
    for key in ("bookIds", "bookIdList", "books", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            out = []
            for item in value:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict) and item.get("bookId"):
                    out.append(str(item["bookId"]))
            return out
    return None


def process_content_html(html: str, proxy_base_url: Optional[str] = None,
                         review_id: str = "") -> Dict:
    """把 /web/mp/content 的响应解析成 content / plain_content / images。

    用 content_processor 而不是 helpers.extract_article_info：后者的正文正则要求
    js_content 后面跟着 <script>，而微信读书返回的是干净片段，匹配不上会得到空正文。
    """
    from utils.content_processor import process_article_content

    if not html or not html.strip():
        raise WereadError("empty_content", f"正文为空: {review_id or '(unknown)'}")

    result = process_article_content(html, proxy_base_url=proxy_base_url)
    if not result.get("content"):
        # /web/mp/content 有时直接返回正文片段，没有 js_content 容器 —— 补一层再解析
        result = process_article_content(
            f'<div id="js_content">{html}</div>', proxy_base_url=proxy_base_url
        )
    if not result.get("content"):
        raise WereadError(
            "empty_content", f"未能从响应中解析出正文: {review_id or '(unknown)'}"
        )
    return result


async def renew_cookie_value(cookie: str, timeout: float = 30.0,
                             client_factory=None) -> str:
    """POST /web/login/renewal，用 wr_rt 换一个新的 wr_skey，返回更新后的 Cookie 串。

    wr_skey 是短效令牌（登录下发的往往只有 8 字符），过期后接口返回 -2012；
    wr_rt 是长期 refreshToken，拿它就能一直换新的 wr_skey，不用重新扫码。

    实测要点（来自 rachelos/we-mp-rss 的 driver/weread_qr.py）：
    - 必须带上完整登录 Cookie，否则服务端按游客处理，返回 -2013 鉴权失败；
    - body 固定为 {"rq": "%2Fweb%2Fbook%2Fread", "ql": true}；
    - 新 wr_skey 由 Set-Cookie 下发，可能仍是 8 字符短值 —— 不能按长度判成败。

    本函数只负责换，不负责存 —— 登录流程要先验证再决定存哪份。
    """
    pairs = parse_cookie(cookie)
    if not pairs.get("wr_rt"):
        raise WereadError(
            "missing_wr_rt",
            "Cookie 里没有 wr_rt，无法自动续期（请重新扫码或重贴 Cookie）",
            retriable=False,
        )

    headers = {
        "Cookie": format_cookie(pairs),
        "User-Agent": user_agent(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": WEREAD_BASE,
        "Referer": f"{WEREAD_BASE}/",
        "Content-Type": "application/json;charset=UTF-8",
    }
    body = json.dumps({"rq": "%2Fweb%2Fbook%2Fread", "ql": True},
                      separators=(",", ":"))

    def _default_factory():
        return httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    factory = client_factory or _default_factory
    try:
        async with factory() as client:
            resp = await client.post(
                f"{WEREAD_BASE}/web/login/renewal",
                content=body.encode("utf-8"),
                headers=headers,
            )
    except httpx.RequestError as exc:
        raise WereadError("network_error", f"续期请求失败: {exc}") from exc

    updated = dict(pairs)
    new_skey = ""
    for name in ("wr_skey", "wr_vid", "wr_rt"):
        value = resp.cookies.get(name)
        if value:
            updated[name] = value
            if name == "wr_skey":
                new_skey = value

    # 兜底：有的响应不走 Set-Cookie，把新 skey 直接放在 JSON body 里
    data = None
    if not new_skey:
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            for field in ("wr_skey", "skey", "accessToken"):
                value = data.get(field)
                if isinstance(value, str) and value:
                    new_skey = value
                    updated["wr_skey"] = value
                    break

    if not new_skey:
        # 续期失败时微信读书通常把原因写在 body 的 errcode 里（比如 wr_rt 也过期了
        # 会回 -2012）。以前直接吞掉，用户只看到一句「未下发新 wr_skey (HTTP 200)」，
        # 完全不知道该重新扫码还是该等一等 —— 先把真实错误码抛出来。
        if isinstance(data, dict):
            raise_for_payload(data)
        raise WereadError(
            "renew_failed",
            f"续期接口未下发新 wr_skey (HTTP {resp.status_code})，"
            f"多半是 wr_rt 也失效了，请打开 /login.html 重新扫码"
        )

    logger.info("[WeRead] wr_skey 续期成功 (len=%d)", len(new_skey))
    return format_cookie(updated)


# ── 客户端 ────────────────────────────────────────────────

class WereadClient:
    """微信读书 Web 接口客户端。

    可直接调用（每次请求新建连接），也可 `async with WereadClient() as c:`
    复用连接 —— 轮询器批量抓正文时用后者省掉每篇一次 TLS 握手。
    """

    def __init__(self, cookie: Optional[str] = None, timeout: float = 30.0,
                 allow_renew: bool = True):
        self._cookie = WereadAuth.normalize_cookie(cookie) if cookie else ""
        self._timeout = timeout
        # 扫码登录时要关掉：那会儿还在挑哪份 Cookie 能用，续期结果不该写进存储
        self._allow_renew = allow_renew
        self._client: Optional[httpx.AsyncClient] = None

    def _new_client(self) -> httpx.AsyncClient:
        """建 HTTP 客户端。集中一处，测试可以在这里换 MockTransport。"""
        return httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )

    async def __aenter__(self) -> "WereadClient":
        self._client = self._new_client()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    @property
    def cookie(self) -> str:
        return self._cookie or weread_auth.get_cookie()

    def _headers(self, accept: str = "application/json, text/plain, */*",
                 json_body: bool = False, app: bool = False) -> Dict[str, str]:
        headers = {
            "Cookie": self.cookie,
            "User-Agent": app_user_agent() if app else user_agent(),
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if not app:
            # i 域是 App 接口，不带浏览器的 Origin/Referer
            headers["Origin"] = WEREAD_BASE
            headers["Referer"] = f"{WEREAD_BASE}/"
        if json_body:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        return headers

    async def _request(self, method: str, path: str, *, params: Optional[Dict] = None,
                       json_data: Optional[Dict] = None, as_json: bool = True,
                       interval: float = 0.0, app: bool = False):
        """发请求；wr_skey 过期时自动续期并重试一次。

        wr_skey 是短效令牌，过期后接口返回 -2012/-2041。只要 Cookie 里还有
        wr_rt（长期 refreshToken），就能用 /web/login/renewal 换一个新的，
        用户不必重新扫码或重贴 Cookie。
        """
        try:
            payload = await self._request_once(
                method, path, params=params, json_data=json_data,
                as_json=as_json, interval=interval, app=app,
            )
            _mark_success()
            return payload
        except WereadError as exc:
            if not (self._allow_renew and exc.is_auth_error
                    and exc.code in AUTH_ERROR_CODES and auto_renew()):
                raise
            if not await self._try_renew():
                raise
            logger.info("[WeRead] wr_skey 已续期，重试 %s", path)
            payload = await self._request_once(
                method, path, params=params, json_data=json_data,
                as_json=as_json, interval=interval, app=app,
            )
            _mark_success()
            return payload

    async def _request_once(self, method: str, path: str, *, params: Optional[Dict] = None,
                            json_data: Optional[Dict] = None, as_json: bool = True,
                            interval: float = 0.0, app: bool = False):
        if not self.cookie:
            raise WereadError("missing_cookie", COOKIE_MISSING_MSG, retriable=False)

        await _throttle(interval)

        url = f"{WEREAD_APP_BASE if app else WEREAD_BASE}{path}"
        headers = self._headers(
            accept="text/html,application/xhtml+xml,*/*" if not as_json else "application/json, text/plain, */*",
            json_body=json_data is not None,
            app=app,
        )
        try:
            if self._client is not None:
                resp = await self._client.request(
                    method, url, params=params, json=json_data, headers=headers
                )
            else:
                async with self._new_client() as client:
                    resp = await client.request(
                        method, url, params=params, json=json_data, headers=headers
                    )
        except httpx.RequestError as exc:
            raise WereadError("network_error", str(exc)) from exc

        if resp.status_code != 200:
            # i 域对无效/未续期 skey 直接 401，这时退回网页域，别反复撞
            if app and resp.status_code in (401, 403):
                mark_app_domain_unusable(f"HTTP {resp.status_code}")
            raise WereadError(
                resp.status_code,
                f"{path} 返回 HTTP {resp.status_code}",
                retriable=resp.status_code >= 500 or resp.status_code == 429,
            )

        if not as_json:
            return resp.text

        try:
            payload = resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            # Cookie 失效时微信读书会返回登录页 HTML 而不是 JSON
            raise WereadError(
                "invalid_json", f"{path} 未返回 JSON（Cookie 可能已失效）"
            ) from exc

        # 鉴权/风控错误码在这里就抛，_request 才能据此触发续期重试；
        # 其它业务错误码留给各自的解析函数处理。
        if isinstance(payload, dict):
            raw_code = payload.get("errCode", payload.get("errcode", 0))
            try:
                code = int(raw_code or 0)
            except (TypeError, ValueError):
                code = 0
            if code in AUTH_ERROR_CODES:
                raise WereadError(
                    code,
                    payload.get("errMsg") or payload.get("errmsg") or str(code),
                    retriable=False,
                )
        return payload

    # --- 登录态续期 ---

    async def _try_renew(self) -> bool:
        """续期 wr_skey。进程内串行 + 冷却，避免并发轮询时一窝蜂去续。"""
        global _last_renew_at, _last_renew_ok
        async with _renew_lock:
            # 别的协程刚续过，直接复用它的结果，别重复打续期接口
            if time.monotonic() - _last_renew_at < RENEW_COOLDOWN:
                return _last_renew_ok
            _last_renew_at = time.monotonic()
            try:
                _last_renew_ok = await self.renew_cookie()
            except WereadError as exc:
                logger.warning("[WeRead] 续期失败: %s", exc.message)
                _last_renew_ok = False
            return _last_renew_ok

    async def renew_cookie(self) -> bool:
        """续期当前 Cookie 并写回存储。"""
        renewed = await renew_cookie_value(self.cookie, timeout=self._timeout,
                                           client_factory=self._new_client)
        weread_auth.set_runtime_cookie(renewed)
        if self._cookie:
            self._cookie = renewed
        return True

    # --- 兼容旧调用点 ---

    # --- 书架 ---

    async def get_shelf_book_ids(self, book_ids: List[str]) -> Optional[List[str]]:
        """查询这些 bookId 中哪些已在书架上。接口形态不可识别时返回 None。"""
        payload = await self._request(
            "GET", "/web/shelf/bookIds", params={"bookIds": ",".join(book_ids)}
        )
        if isinstance(payload, dict):
            raise_for_payload(payload)
        return parse_shelf_book_ids(payload)

    async def add_to_shelf(self, book_id: str) -> Dict:
        """把公众号加入书架（= 在微信读书里关注它）。"""
        try:
            payload = await self._request(
                "POST", "/mp/shelf/addToShelf", json_data={"bookIds": [book_id]}
            )
        except WereadError as exc:
            # 老版本微信读书用 /web/shelf/add，接口不存在（404）时退回去试一次
            if exc.code != 404:
                raise
            payload = await self._request(
                "POST", "/web/shelf/add", json_data={"bookIds": [book_id]}
            )
        if isinstance(payload, dict):
            raise_for_payload(payload)
        return payload if isinstance(payload, dict) else {}

    async def ensure_on_shelf(self, book_id: str, name: str = "") -> Tuple[bool, str]:
        """确保公众号在书架上（微信读书只对书架上的号返回文章）。

        返回 (是否可采集, 说明)。失败不抛异常 —— 采集接口自己会给出最终结果。
        """
        if not str(book_id or "").startswith(MP_BOOK_PREFIX):
            return True, "非公众号 bookId，跳过书架检查"
        if book_id in _shelf_confirmed:
            return True, "已在书架（缓存）"
        if not auto_add_to_shelf():
            return True, "未启用自动加入书架（WEREAD_AUTO_ADD_SHELF=false）"

        try:
            on_shelf = await self.get_shelf_book_ids([book_id])
        except WereadError as exc:
            if exc.is_auth_error:
                return False, exc.user_message
            logger.debug("书架状态查询失败（继续尝试添加）: %s", exc)
            on_shelf = None

        if on_shelf is not None and book_id in on_shelf:
            _shelf_confirmed.add(book_id)
            return True, "已在书架"

        try:
            await self.add_to_shelf(book_id)
        except WereadError as exc:
            return False, f"加入书架失败: {exc.user_message}"
        _shelf_confirmed.add(book_id)
        return True, f"「{name or book_id}」已自动加入微信读书书架"

    async def get_shelf_accounts(self) -> List[Dict]:
        """列出书架上的公众号。

        用的就是 verify() 那个接口，所以只要登录态有效这里就能出结果 ——
        这是不依赖公众号后台、最稳的一条「找公众号」的路。
        userVid 必须传空字符串，传真实 vid 反而触发 -2012。
        """
        payload = await self._request(
            "GET", "/web/shelf/sync",
            params={"userVid": "", "synckey": 0, "lectureSynckey": 0},
            interval=page_interval(),
        )
        accounts = parse_shelf_accounts(payload)
        # 书架上列出来的，按定义就在书架上 —— 记下来，采集时不必再查/再加一次
        for acc in accounts:
            mark_on_shelf(acc["book_id"])
        return accounts

    async def verify(self) -> Tuple[bool, str]:
        """打一次最轻的接口确认 Cookie 还有效。返回 (是否有效, 说明)。

        用 /web/shelf/sync 且 userVid 必须传**空字符串** —— rachelos/we-mp-rss
        实测：userVid 传了真实 vid 反而会触发 -2012「登录超时」。
        """
        try:
            payload = await self._request(
                "GET", "/web/shelf/sync", params={"userVid": "", "synckey": 0}
            )
        except WereadError as exc:
            return False, exc.user_message
        if isinstance(payload, dict):
            try:
                raise_for_payload(payload)
            except WereadError as exc:
                return False, exc.user_message
        return True, "微信读书 Cookie 有效"

    # --- 文章 ---

    async def get_articles_page(self, book_id: str, offset: int = 0) -> Dict:
        return await self._request(
            "GET", "/web/mp/articles",
            params={"bookId": book_id, "offset": offset},
            interval=page_interval(),
        )

    async def search_mp_accounts(self, keyword: str, count: int = 15) -> List[Dict]:
        """用微信读书搜公众号（i 域 /store/search）。

        这是 i 域比网页域多出来的能力，也是本项目摆脱「搜索必须依赖公众号后台」
        的唯一路子。i 域不可用时抛错，由调用方回退到后台 searchbiz。
        """
        if not app_domain_usable():
            raise WereadError("app_domain_unavailable", "微信读书 App 接口当前不可用")
        payload = await self._request(
            "GET", "/store/search",
            params={"v": 2, "scope": 2, "count": count, "type": 0, "keyword": keyword},
            interval=page_interval(),
            app=True,
        )
        return parse_store_search(payload)

    async def get_app_articles_page(self, book_id: str, offset: int = 0,
                                    count: int = 20) -> Dict:
        """i 域的文章列表接口，作为网页域 /web/mp/articles 之外的另一条路。"""
        if not app_domain_usable():
            raise WereadError("app_domain_unavailable", "微信读书 App 接口当前不可用")
        return await self._request(
            "GET", "/book/articles",
            params={"bookId": book_id, "offset": offset, "count": count, "synckey": 0},
            interval=page_interval(),
            app=True,
        )

    async def get_cover(self, book_id: str) -> Dict:
        return await self._request(
            "GET", "/api/mp/cover", params={"bookId": book_id}, interval=page_interval()
        )

    async def get_content_html(self, review_id: str) -> str:
        return await self._request(
            "GET", "/web/mp/content",
            params={"reviewId": review_id},
            as_json=False,
            interval=content_interval(),
        )

    async def list_articles(self, fakeid: str, limit: int = 10, offset: int = 0,
                            nickname: str = "") -> List[Dict]:
        """拉取公众号文章列表。

        主路径 /web/mp/articles 翻页；该接口被风控（-2041 等）且一条都没拿到时，
        退回 /api/mp/cover 只取最新一篇，保证采集不中断。
        """
        book_id = fakeid_to_book_id(fakeid)

        ok, detail = await self.ensure_on_shelf(book_id, nickname)
        if not ok:
            logger.warning("[WeRead] %s 书架检查失败: %s", nickname or book_id, detail)
        else:
            logger.debug("[WeRead] %s 书架检查: %s", nickname or book_id, detail)

        articles: List[Dict] = []
        seen = set()
        page_offset = max(int(offset or 0), 0)
        try:
            for _ in range(max_pages()):
                payload = await self.get_articles_page(book_id, offset=page_offset)
                page_articles, group_count = parse_mp_articles(payload, book_id)
                added = 0
                for item in page_articles:
                    if item["review_id"] in seen:
                        continue
                    seen.add(item["review_id"])
                    articles.append(item)
                    added += 1
                if group_count == 0 or len(articles) >= limit:
                    break
                if added == 0:
                    # 这一页全是见过的：要么到底了，要么这个号的 offset 不按预期推进。
                    # 再翻下去只会拿到同样的东西，白白多打 WEREAD_MAX_PAGES-1 次接口 ——
                    # 订阅号一多，每轮就是几十上百个无用请求，纯粹给风控送素材。
                    logger.debug("[WeRead] %s 本页没有新文章，停止翻页",
                                 nickname or book_id)
                    break
                # offset 按顶层分组数累加，不是按文章条数
                page_offset += group_count
        except WereadError as exc:
            if articles:
                logger.warning("[WeRead] %s 翻页中断（已取到 %d 篇）: %s",
                               nickname or book_id, len(articles), exc)
                return articles[:limit]

            # 网页域列表不可用（曾整体被废弃过一阵），先试 i 域的同名能力
            if app_domain_usable():
                try:
                    payload = await self.get_app_articles_page(book_id, count=limit)
                    app_articles, _ = parse_app_articles(payload, book_id)
                    if app_articles:
                        logger.info("[WeRead] %s 网页域列表不可用，改用 i 域取到 %d 篇",
                                    nickname or book_id, len(app_articles))
                        return app_articles[:limit]
                except Exception as app_exc:
                    # i 域只是额外加的一条路，它出什么问题都不该影响下面的 cover 兜底
                    logger.info("[WeRead] %s i 域列表也不可用: %s",
                                nickname or book_id, app_exc)

            logger.warning("[WeRead] %s 列表接口不可用（%s），回退到仅取最新一篇",
                           nickname or book_id, exc)
            payload = await self.get_cover(book_id)
            latest = parse_mp_cover(payload, book_id)
            return [latest] if latest else []

        return articles[:limit]

    async def fetch_article_content(self, review_id: str,
                                    proxy_base_url: Optional[str] = None) -> Dict:
        """抓取正文并复用项目的图文处理管线（保序 + 图片代理）。

        返回 process_article_content 的结果：content / plain_content / images。
        """
        html = await self.get_content_html(review_id)
        return process_content_html(html, proxy_base_url=proxy_base_url,
                                    review_id=review_id)
