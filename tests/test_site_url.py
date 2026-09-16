#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SITE_URL 配错不能顺着写进 RSS。

真实事故：用户把文档里的中文占位符原样粘进去（SITE_URL=http://你的IP:5000），
RSS 里的图片地址就成了 http://你的IP:5000/api/image?url=...，
Read You 阅读器拿它当 Referer 时直接崩：

    java.lang.IllegalArgumentException:
        Unexpected char 0x4f60 at 0 in Referer value: 你的IP
"""

import pytest

from utils import site_url


@pytest.fixture(autouse=True)
def _reset():
    site_url.reset_warning()
    yield
    site_url.reset_warning()


class FakeRequest:
    def __init__(self, host="127.0.0.1:5000", proto=None):
        self.headers = {"Host": host}
        if proto:
            self.headers["X-Forwarded-Proto"] = proto

        class _U:
            scheme = "http"
        self.url = _U()


@pytest.mark.parametrize("value,ok", [
    ("http://192.168.1.10:5000", True),
    ("https://rss.example.net", True),
    ("http://localhost:5000/", True),
    ("http://你的IP:5000", False),        # 用户真正踩到的
    ("https://你的域名.com", False),
    ("http://your-domain.com", False),
    ("https://example.com", False),
    ("ftp://192.168.1.10", False),
    ("192.168.1.10:5000", False),         # 没有协议
    ("http://", False),                   # 没有主机
    ("", False),
])
def test_validate(value, ok):
    assert site_url.validate(value)[0] is ok


def test_non_ascii_host_reason_mentions_the_crash():
    ok, reason = site_url.validate("http://你的IP:5000")
    assert ok is False
    assert "非 ASCII" in reason or "占位符" in reason


def test_bad_site_url_falls_back_to_request_host(monkeypatch):
    """配错时必须回退，而不是把坏地址写进 RSS。"""
    monkeypatch.setenv("SITE_URL", "http://你的IP:5000")

    assert site_url.configured() == ""
    assert site_url.base_url(FakeRequest("10.0.0.5:5000")) == "http://10.0.0.5:5000"


def test_good_site_url_is_used(monkeypatch):
    monkeypatch.setenv("SITE_URL", "https://rss.example.net/")
    assert site_url.base_url(FakeRequest()) == "https://rss.example.net"


def test_warning_is_logged_once(monkeypatch, caplog):
    monkeypatch.setenv("SITE_URL", "http://你的IP:5000")
    with caplog.at_level("ERROR"):
        site_url.configured()
        site_url.configured()
        site_url.configured()
    assert sum("SITE_URL 配置有问题" in r.message for r in caplog.records) == 1


def test_rss_never_emits_non_ascii_image_host(monkeypatch):
    """端到端的底线：不管 SITE_URL 配成什么，RSS 里不能出现非 ASCII 的地址。"""
    monkeypatch.setenv("SITE_URL", "http://你的IP:5000")
    monkeypatch.delenv("RSS_DIRECT_IMAGES", raising=False)
    from utils import rss_streaming

    article = {
        "aid": "a1", "title": "标题", "link": "https://mp.weixin.qq.com/s/a1",
        "digest": "", "cover": "", "author": "某号", "publish_time": 1757000000,
        "content": '<img src="https://mmbiz.qpic.cn/a/640">',
    }
    item = rss_streaming._build_item_xml(article, site_url.base_url(FakeRequest("10.0.0.5:5000")))

    assert "你的IP" not in item
    item.encode("ascii", "strict") if False else None   # 正文本身可以有中文
    import re
    for url in re.findall(r'(?:src|href)="([^"]+)"', item):
        url.encode("ascii")     # 地址必须是纯 ASCII，否则阅读器会崩


# ── 存量正文里的旧地址要按当前请求重写 ──────────────────────

def test_stored_proxy_url_is_repointed_to_current_host():
    """正文里存的是采集那一刻的绝对地址，输出时必须换成读者用的地址。

    否则没配 SITE_URL 时存下的 localhost、或者换 IP/域名之前的旧地址，
    会让读者在别的设备上打开时图全挂（指向他自己的机器）。
    """
    from utils.image_proxy import proxy_image_url

    stored = "http://localhost:5000/api/image?url=https%3A%2F%2Fmmbiz.qpic.cn%2Fa%2F640"
    out = proxy_image_url(stored, "https://rss.example.net")

    assert out == ("https://rss.example.net/api/image?url="
                   "https%3A%2F%2Fmmbiz.qpic.cn%2Fa%2F640")


def test_repointing_does_not_double_wrap():
    from utils.image_proxy import proxy_image_url

    stored = "http://old:5000/api/image?url=https%3A%2F%2Fmmbiz.qpic.cn%2Fa%2F640"
    out = proxy_image_url(stored, "https://new.example.net")

    assert out.count("/api/image?url=") == 1
