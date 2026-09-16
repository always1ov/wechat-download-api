#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信图片本地缓存：同一张图只向微信要一次。

原来两条路都在反复重抓：
- /api/image 代理：每个订阅者 × 每次刷新 × 每台设备
- 导出内嵌：每导一种格式重抓一遍全部图
请求量被成倍放大，而微信风控看的正是请求量。
"""

import httpx
import pytest

from utils import image_cache

URL = "https://mmbiz.qpic.cn/mmbiz_jpg/abc/640"


@pytest.fixture(autouse=True)
def _tmp_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGE_CACHE_DIR", str(tmp_path / "imgcache"))
    monkeypatch.delenv("IMAGE_CACHE", raising=False)
    monkeypatch.delenv("IMAGE_CACHE_MAX_MB", raising=False)
    yield


def test_put_then_get_roundtrip():
    assert image_cache.get(URL) is None
    assert image_cache.put(URL, b"\xff\xd8binary", "image/png") is True

    blob, ctype = image_cache.get(URL)
    assert blob == b"\xff\xd8binary"
    assert ctype == "image/png"


def test_cache_can_be_disabled(monkeypatch):
    monkeypatch.setenv("IMAGE_CACHE", "false")
    assert image_cache.put(URL, b"x") is False
    assert image_cache.get(URL) is None


def test_eviction_keeps_cache_under_limit(monkeypatch):
    monkeypatch.setenv("IMAGE_CACHE_MAX_MB", "16")        # 下限就是 16MB
    blob = b"x" * (1024 * 1024)                            # 每张 1MB
    for i in range(20):
        image_cache.put(f"{URL}/{i}", blob)

    st = image_cache.stats()
    assert st["bytes"] <= st["max_bytes"], "缓存没有被淘汰，会无限涨"


def test_extract_wechat_images_handles_proxy_urls():
    html = (
        '<img src="http://host:5000/api/image?url='
        'https%3A%2F%2Fmmbiz.qpic.cn%2Fa%2F640">'
        '<img data-src="https://mmbiz.qpic.cn/b/640">'
        '<img src="https://example.com/not-wechat.png">'
        '<img src="https://mmbiz.qpic.cn/a/640">'          # 与第一张同图，应去重
    )
    urls = image_cache.extract_wechat_images(html)

    assert urls == ["https://mmbiz.qpic.cn/a/640", "https://mmbiz.qpic.cn/b/640"]


async def test_prefetch_skips_already_cached(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, content=b"img", headers={"content-type": "image/jpeg"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler),
                                 follow_redirects=True),
    )

    assert await image_cache.prefetch([URL]) == 1
    assert calls["n"] == 1

    # 第二次同一张图：不该再打微信
    assert await image_cache.prefetch([URL]) == 0
    assert calls["n"] == 1, "已缓存的图又去抓了一遍"


# ── RSS 正文里的图必须指向自家代理，不能甩给微信 ────────────────

def test_rss_content_keeps_images_on_our_proxy(monkeypatch):
    """默认不把图还原成微信直链。

    直链意味着每个订阅者、每次刷新都直接打微信 CDN，而且读者访问不到微信时
    整篇文章的图全挂 —— 跟「RSS 要能离线看完整图文」相反。
    """
    monkeypatch.delenv("RSS_DIRECT_IMAGES", raising=False)
    from utils import rss_streaming

    article = {
        "aid": "a1", "title": "标题", "link": "https://mp.weixin.qq.com/s/a1",
        "digest": "", "cover": "", "author": "某号", "publish_time": 1757000000,
        "content": '<p>正文</p><img src="https://mmbiz.qpic.cn/a/640">',
    }
    item = rss_streaming._build_item_xml(article, "http://host:5000")

    assert "/api/image?url=" in item, "图没走自家代理，读者会被甩去微信"
    assert 'src="https://mmbiz.qpic.cn' not in item


def test_direct_images_can_be_opted_back_in(monkeypatch):
    monkeypatch.setenv("RSS_DIRECT_IMAGES", "true")
    from utils import rss_streaming

    article = {
        "aid": "a1", "title": "标题", "link": "https://mp.weixin.qq.com/s/a1",
        "digest": "", "cover": "", "author": "某号", "publish_time": 1757000000,
        "content": '<img src="http://host:5000/api/image?url='
                   'https%3A%2F%2Fmmbiz.qpic.cn%2Fa%2F640">',
    }
    item = rss_streaming._build_item_xml(article, "http://host:5000")

    assert "mmbiz.qpic.cn" in item
    assert "/api/image?url=" not in item
