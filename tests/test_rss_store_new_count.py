#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""save_articles 的「新增篇数」必须只数真正新插入的。

ON CONFLICT DO UPDATE 命中时 SQLite 的 cursor.rowcount 同样是 1，
靠它计数会把「回填正文的旧文章」算成新增 —— 轮询日志和
「获取历史文章」的「新增 N 篇」都会虚高。
"""

import pytest

from utils import rss_store

FAKEID = "MzI5NjM4MjExMg=="


@pytest.fixture(autouse=True)
def _subscription():
    """articles 有指向 subscriptions 的外键，没订阅的话插入会被静默丢掉。"""
    rss_store.add_subscription(FAKEID, nickname="人民日报")
    yield
    rss_store.remove_subscription(FAKEID)


def _art(aid, content=""):
    return {"aid": aid, "title": f"文章 {aid}", "link": f"https://mp.weixin.qq.com/s/{aid}",
            "digest": "", "cover": "", "author": "某号",
            "content": content, "plain_content": content, "publish_time": 1757000000}


def test_new_count_excludes_backfilled_articles():
    assert rss_store.save_articles(FAKEID, [_art("a1"), _art("a2")]) == 2

    # 同样两篇再来一次，这次带正文 —— 会走 DO UPDATE 把正文补上，但不是新增
    assert rss_store.save_articles(FAKEID, [_art("a1", "正文"), _art("a2", "正文")]) == 0, \
        "回填正文的旧文章被当成了新增"

    # 混合：一篇旧的 + 一篇新的，只能算 1 篇新增
    assert rss_store.save_articles(FAKEID, [_art("a1", "正文"), _art("a3")]) == 1


def test_deep_fetch_of_already_polled_articles_reports_zero_new():
    """「获取历史文章」把轮询器已经拿过的又拉一遍时，不该报「新增」。"""
    rss_store.save_articles(FAKEID, [_art("b1"), _art("b2")], source="poll")

    assert rss_store.save_articles(FAKEID, [_art("b1"), _art("b2")],
                                   source="deep_fetch") == 0
