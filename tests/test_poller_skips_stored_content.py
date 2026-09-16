#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正文落盘之后就别再问微信读书要了。

列表接口每轮都把最近 N 篇原样返回，而 parse_mp_articles 解析出来的 dict 里
根本没有 content 字段 —— 所以「not a.get('content')」永远成立，
不查库的话每一轮都会把同样几篇正文重抓一遍。
20 个订阅号 × 每轮 10 篇 = 每小时 200 次重复请求，内容一个字都没变。
"""

import httpx
import pytest

from utils import rss_store
from utils import weread_client as wc
from utils.rss_poller import RSSPoller
from utils.weread_client import WereadClient

FAKEID = "MzI5NjM4MjExMg=="
BOOK = "MP_WXS_3296382112"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=1; wr_skey=k; wr_rt=rt")
    monkeypatch.setenv("WEREAD_CONTENT_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_PAGE_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_AUTO_ADD_SHELF", "false")
    monkeypatch.setenv("IMAGE_PREFETCH", "false")
    monkeypatch.setenv("IMAGE_CACHE_DIR", str(tmp_path / "img"))
    wc.reset_shelf_cache()
    rss_store.add_subscription(FAKEID, nickname="人民日报")
    yield
    rss_store.remove_subscription(FAKEID)
    wc.reset_shelf_cache()


class Server:
    def __init__(self, ids):
        self.ids = list(ids)
        self.content_calls = 0

    def __call__(self, request):
        path = request.url.path
        if path == "/web/mp/articles":
            off = int(request.url.params.get("offset", 0) or 0)
            items = [] if off else [{
                "createTime": 1757000000,
                "subReviews": [{
                    "reviewId": f"{BOOK}_{i}",
                    "review": {"reviewId": f"{BOOK}_{i}", "createTime": 1757000000,
                               "mpInfo": {"title": f"文章{i}", "time": 1757000000,
                                          "content": "摘要", "pic_url": ""}},
                }],
            } for i in self.ids]
            return httpx.Response(200, json={"reviews": items})
        if path == "/web/mp/content":
            self.content_calls += 1
            return httpx.Response(
                200, text='<div id="js_content"><p>这是正文，内容不变。</p></div>')
        return httpx.Response(200, json={})


@pytest.fixture
def install(monkeypatch):
    def _install(server):
        monkeypatch.setattr(
            WereadClient, "_new_client",
            lambda self: httpx.AsyncClient(transport=httpx.MockTransport(server),
                                           follow_redirects=True),
        )
        return server
    return _install


async def test_stored_content_is_not_refetched(install):
    server = install(Server(["s1", "s2", "s3"]))
    poller = RSSPoller()

    await poller._poll_one(FAKEID, "人民日报")
    assert server.content_calls == 3, "首轮要把 3 篇正文抓下来"

    for _ in range(3):
        await poller._poll_one(FAKEID, "人民日报")

    assert server.content_calls == 3, (
        f"正文已经落盘却又抓了 {server.content_calls - 3} 次，"
        "每轮重抓同样的文章正是风控的主要来源")


async def test_new_articles_still_get_fetched(install):
    server = install(Server(["n1", "n2"]))
    poller = RSSPoller()

    await poller._poll_one(FAKEID, "人民日报")
    assert server.content_calls == 2

    # 公众号发了一篇新的
    server.ids.append("n3")
    await poller._poll_one(FAKEID, "人民日报")

    assert server.content_calls == 3, "新文章必须照抓，不能被跳过逻辑误伤"


async def test_blanked_content_is_refetched(install):
    """被风控清空过正文的文章，下一轮要能补回来。"""
    server = install(Server(["b1"]))
    poller = RSSPoller()

    await poller._poll_one(FAKEID, "人民日报")
    assert server.content_calls == 1

    # 模拟「存成风控页后被启动清理清空」
    rss_store.clear_intercepted_content()
    conn = rss_store._get_conn()
    conn.execute("UPDATE articles SET content='', plain_content='' WHERE fakeid=?",
                 (FAKEID,))
    conn.commit()
    conn.close()

    await poller._poll_one(FAKEID, "人民日报")
    assert server.content_calls == 2, "正文被清空的文章应该重新抓"


def test_links_with_content_ignores_empty_bodies():
    rss_store.save_articles(FAKEID, [
        {"aid": "x1", "title": "有正文", "link": "https://mp.weixin.qq.com/s/x1",
         "digest": "", "cover": "", "author": "a", "content": "<p>正文</p>",
         "plain_content": "正文", "publish_time": 1},
        {"aid": "x2", "title": "没正文", "link": "https://mp.weixin.qq.com/s/x2",
         "digest": "", "cover": "", "author": "a", "content": "",
         "plain_content": "", "publish_time": 2},
    ])

    have = rss_store.links_with_content(
        FAKEID, ["https://mp.weixin.qq.com/s/x1", "https://mp.weixin.qq.com/s/x2"])

    assert have == {"https://mp.weixin.qq.com/s/x1"}
