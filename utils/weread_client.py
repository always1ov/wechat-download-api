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
MP_BOOK_PREFIX = "MP_WXS_"

# 微信读书的鉴权/风控错误码。重试没有意义：要么 Cookie 过期要么被限流，
# 必须让用户重新贴 Cookie（或退回公众号后台通道）。
AUTH_ERROR_CODES = (-2010, -2012, -2041)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

COOKIE_MISSING_MSG = "未配置微信读书 Cookie，请在管理页面配置或设置 WEREAD_COOKIE 环境变量"
COOKIE_EXPIRED_MSG = "微信读书登录已过期，请到管理页面重新配置微信读书 Cookie"

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


def article_source() -> str:
    """文章来源策略：auto（后台优先、读书兜底）/ mp（只用后台）/ weread（只用读书）。"""
    value = _env_str("ARTICLE_SOURCE", "auto").lower()
    return value if value in ("auto", "mp", "weread") else "auto"


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
            return COOKIE_EXPIRED_MSG
        return f"微信读书接口失败: {self.message}"


# ── 标识换算 ──────────────────────────────────────────────

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
        """当前生效的 Cookie（环境变量优先）。"""
        env_cookie = self.normalize_cookie(os.getenv("WEREAD_COOKIE", ""))
        if env_cookie:
            return env_cookie
        return self.normalize_cookie(self._load().get("cookie", ""))

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


# 已确认在书架上的 bookId，避免每轮轮询都重复 addToShelf
_shelf_confirmed: set = set()


def reset_shelf_cache():
    """换 Cookie（换账号）后书架状态不再可信，清空缓存。"""
    _shelf_confirmed.clear()


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


# ── 客户端 ────────────────────────────────────────────────

class WereadClient:
    """微信读书 Web 接口客户端。

    可直接调用（每次请求新建连接），也可 `async with WereadClient() as c:`
    复用连接 —— 轮询器批量抓正文时用后者省掉每篇一次 TLS 握手。
    """

    def __init__(self, cookie: Optional[str] = None, timeout: float = 30.0):
        self._cookie = WereadAuth.normalize_cookie(cookie) if cookie else ""
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "WereadClient":
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )
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
                 json_body: bool = False) -> Dict[str, str]:
        headers = {
            "Cookie": self.cookie,
            "User-Agent": _env_str("WEREAD_USER_AGENT") or DEFAULT_USER_AGENT,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": WEREAD_BASE,
            "Referer": f"{WEREAD_BASE}/",
        }
        if json_body:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        return headers

    async def _request(self, method: str, path: str, *, params: Optional[Dict] = None,
                       json_data: Optional[Dict] = None, as_json: bool = True,
                       interval: float = 0.0):
        if not self.cookie:
            raise WereadError("missing_cookie", COOKIE_MISSING_MSG, retriable=False)

        await _throttle(interval)

        url = f"{WEREAD_BASE}{path}"
        headers = self._headers(
            accept="text/html,application/xhtml+xml,*/*" if not as_json else "application/json, text/plain, */*",
            json_body=json_data is not None,
        )
        try:
            if self._client is not None:
                resp = await self._client.request(
                    method, url, params=params, json=json_data, headers=headers
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                    resp = await client.request(
                        method, url, params=params, json=json_data, headers=headers
                    )
        except httpx.RequestError as exc:
            raise WereadError("network_error", str(exc)) from exc

        if resp.status_code != 200:
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
        return payload

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

    async def verify(self) -> Tuple[bool, str]:
        """打一次最轻的接口确认 Cookie 还有效。返回 (是否有效, 说明)。"""
        try:
            await self.get_shelf_book_ids([])
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
                for item in page_articles:
                    if item["review_id"] in seen:
                        continue
                    seen.add(item["review_id"])
                    articles.append(item)
                if group_count == 0 or len(articles) >= limit:
                    break
                # offset 按顶层分组数累加，不是按文章条数
                page_offset += group_count
        except WereadError as exc:
            if articles:
                logger.warning("[WeRead] %s 翻页中断（已取到 %d 篇）: %s",
                               nickname or book_id, len(articles), exc)
                return articles[:limit]
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
