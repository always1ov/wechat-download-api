#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
微信图片本地缓存 —— 同一张图只向微信要一次。

为什么需要：原来两条路都在反复重抓同一张图，白白把请求量做大，
而微信的风控恰恰看请求量。

- `/api/image` 代理：RSS 阅读器每刷新一次、每换一个设备、每多一个订阅者，
  都会把文章里的图重新拉一遍；
- 导出内嵌（PDF / Word / EPUB）：`inline_images` 每次导出都把全部图重抓，
  同一个号导三种格式就是三遍。

微信图的 URL 是内容寻址式的（路径里带图片自身的标识），同一个 URL 的内容
不会变，所以可以放心长期缓存，不需要 TTL —— 只按总体积做 LRU 淘汰。

缓存落在 data/ 下，和文章库一起持久化；删掉整个目录也只是下次重抓，不会丢数据。
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent


def cache_enabled() -> bool:
    """是否启用图片缓存。默认开 —— 关掉只会让微信请求变多。"""
    raw = (os.getenv("IMAGE_CACHE", "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def cache_dir() -> Path:
    raw = (os.getenv("IMAGE_CACHE_DIR", "") or "").strip()
    return Path(raw) if raw else (_BASE_DIR / "data" / "imgcache")


def max_bytes() -> int:
    """缓存总上限（字节）。超了按最久未用淘汰。"""
    try:
        mb = int(os.getenv("IMAGE_CACHE_MAX_MB", "512"))
    except ValueError:
        mb = 512
    return max(mb, 16) * 1024 * 1024


def _paths(url: str) -> Tuple[Path, Path]:
    """URL → (数据文件, 元数据文件)。按哈希前两位分桶，避免单目录几万个文件。"""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    bucket = cache_dir() / digest[:2]
    return bucket / digest, bucket / f"{digest}.json"


def get(url: str) -> Optional[Tuple[bytes, str]]:
    """命中返回 (图片字节, content-type)，未命中返回 None。"""
    if not cache_enabled():
        return None
    data_path, meta_path = _paths(url)
    try:
        if not data_path.is_file():
            return None
        blob = data_path.read_bytes()
        if not blob:
            return None
        content_type = "image/jpeg"
        try:
            content_type = json.loads(meta_path.read_text("utf-8")).get(
                "content_type", content_type)
        except Exception:
            pass
        _touch(data_path)
        return blob, content_type
    except Exception as exc:
        logger.debug("[ImageCache] 读取失败 %s: %s", url[:60], exc)
        return None


def put(url: str, blob: bytes, content_type: str = "image/jpeg") -> bool:
    """写入缓存。失败只记日志，不影响调用方。"""
    if not cache_enabled() or not blob:
        return False
    data_path, meta_path = _paths(url)
    try:
        data_path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再改名：并发或中途失败时不会留下半截图
        tmp = data_path.with_suffix(".part")
        tmp.write_bytes(blob)
        tmp.replace(data_path)
        meta_path.write_text(
            json.dumps({"content_type": content_type, "url": url}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("[ImageCache] 写入失败 %s: %s", url[:60], exc)
        return False
    _evict_if_needed()
    return True


def _touch(path: Path) -> None:
    """标记最近使用。一小时内不重复 touch，免得每次读都写一次盘。"""
    try:
        now = time.time()
        if now - path.stat().st_mtime > 3600:
            os.utime(path, (now, now))
    except Exception:
        pass


def _evict_if_needed() -> None:
    """超出上限时按最久未用删，删到上限的 80%。"""
    limit = max_bytes()
    try:
        entries = []
        total = 0
        for data_path in cache_dir().rglob("*"):
            if not data_path.is_file() or data_path.suffix in (".json", ".part"):
                continue
            stat = data_path.stat()
            total += stat.st_size
            entries.append((stat.st_mtime, stat.st_size, data_path))
        if total <= limit:
            return
        entries.sort()                       # 最久未用排前面
        target = int(limit * 0.8)
        removed = 0
        for _, size, data_path in entries:
            if total <= target:
                break
            try:
                data_path.unlink(missing_ok=True)
                data_path.with_suffix(".json").unlink(missing_ok=True)
                total -= size
                removed += 1
            except Exception:
                continue
        if removed:
            logger.info("[ImageCache] 超出上限，淘汰 %d 张图", removed)
    except Exception as exc:
        logger.debug("[ImageCache] 淘汰失败: %s", exc)


def stats() -> dict:
    """缓存概况，给 /api/health 和排查用。"""
    count = 0
    total = 0
    try:
        for data_path in cache_dir().rglob("*"):
            if data_path.is_file() and data_path.suffix not in (".json", ".part"):
                count += 1
                total += data_path.stat().st_size
    except Exception:
        pass
    return {
        "enabled": cache_enabled(),
        "count": count,
        "bytes": total,
        "max_bytes": max_bytes(),
        "dir": str(cache_dir()),
    }


def clear() -> int:
    """清空缓存，返回删除的图片数。"""
    removed = 0
    try:
        for data_path in cache_dir().rglob("*"):
            if data_path.is_file():
                try:
                    data_path.unlink()
                    if data_path.suffix not in (".json", ".part"):
                        removed += 1
                except Exception:
                    continue
    except Exception as exc:
        logger.debug("[ImageCache] 清空失败: %s", exc)
    return removed


# ── 预热 ────────────────────────────────────────────────

_WX_IMG_RE = None


def extract_wechat_images(html: str, limit: int = 60) -> list:
    """从正文里挑出微信图 URL（去重、保序）。"""
    global _WX_IMG_RE
    import re
    from urllib.parse import unquote

    if _WX_IMG_RE is None:
        _WX_IMG_RE = re.compile(r'(?:src|data-src)="([^"]+)"', re.IGNORECASE)

    out, seen = [], set()
    for raw in _WX_IMG_RE.findall(html or ""):
        url = raw
        # 正文里存的是 /api/image?url=<编码原图>，取出真实地址
        marker = "/api/image?url="
        if marker in url:
            url = unquote(url.split(marker, 1)[1])
        if not url.startswith("http"):
            continue
        if "qpic.cn" not in url and "qlogo.cn" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out


def prefetch_enabled() -> bool:
    """采集到正文时要不要顺手把图也抓进缓存。默认开。

    开着的话，读者打开 RSS 时图已经在本地，既快又不会临时去打微信；
    关掉则退化成「读者第一次看时才抓」。
    """
    raw = (os.getenv("IMAGE_PREFETCH", "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


async def prefetch(urls, interval: float = 0.0, timeout: float = 15.0) -> int:
    """把这些图抓进缓存，返回新抓的张数。已缓存的直接跳过。"""
    import asyncio

    import httpx

    if not cache_enabled() or not urls:
        return 0

    todo = [u for u in urls if get(u) is None]
    if not todo:
        return 0

    fetched = 0
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for url in todo:
            try:
                resp = await client.get(
                    url, headers={"Referer": "https://mp.weixin.qq.com/"})
                if resp.status_code == 200 and resp.content:
                    put(url, resp.content,
                        resp.headers.get("content-type", "image/jpeg"))
                    fetched += 1
            except Exception as exc:
                logger.debug("[ImageCache] 预热失败 %s: %s", url[:60], exc)
            if interval > 0:
                await asyncio.sleep(interval)
    if fetched:
        logger.info("[ImageCache] 预热 %d 张图", fetched)
    return fetched
