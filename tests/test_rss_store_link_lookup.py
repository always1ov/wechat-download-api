#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按原文链接反查文章 —— /api/article 走微信读书兜底时靠它找出 fakeid。"""

import pytest

from utils import rss_store

FAKEID = "MzI5NjM4MjExMg=="


@pytest.fixture
def seeded():
    rss_store.add_subscription(FAKEID, nickname="测试号")
    rss_store.save_articles(FAKEID, [
        {"aid": "1", "title": "带参数入库",
         "link": "https://mp.weixin.qq.com/s/tok1?chksm=abc&scene=21", "publish_time": 100},
        {"aid": "2", "title": "下划线 token",
         "link": "https://mp.weixin.qq.com/s/a_b", "publish_time": 200},
        {"aid": "3", "title": "易混淆 token",
         "link": "https://mp.weixin.qq.com/s/aXb", "publish_time": 300},
    ])
    yield
    rss_store.remove_subscription(FAKEID)


def test_exact_match(seeded):
    row = rss_store.get_article_by_link("https://mp.weixin.qq.com/s/a_b")
    assert row["title"] == "下划线 token"
    assert row["fakeid"] == FAKEID


def test_matches_stored_link_that_has_extra_query_params(seeded):
    """调用方给干净短链，库里存的却带 chksm/scene。"""
    row = rss_store.get_article_by_link("https://mp.weixin.qq.com/s/tok1")
    assert row["title"] == "带参数入库"


def test_underscore_in_token_is_not_a_wildcard(seeded):
    """'_' 是 LIKE 的单字符通配符，不转义会把 /s/aXb 也匹配上。"""
    row = rss_store.get_article_by_link("https://mp.weixin.qq.com/s/a_b")
    assert row["title"] == "下划线 token"


def test_missing_link_returns_none(seeded):
    assert rss_store.get_article_by_link("https://mp.weixin.qq.com/s/nope") is None
    assert rss_store.get_article_by_link("") is None
