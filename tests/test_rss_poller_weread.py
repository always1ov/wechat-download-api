#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轮询器在公众号后台失效时切到微信读书的行为。"""

import pytest

import utils.article_fetcher as article_fetcher
from utils import weread_client as wc
from utils.rss_poller import MpChannelError, RSSPoller, WechatInvalidFakeidError
from utils.weread_client import WereadClient, WereadError

FAKEID = "MzI5NjM4MjExMg=="
BOOK_ID = "MP_WXS_3296382112"
LINK = "https://mp.weixin.qq.com/s/tok1"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=1; wr_skey=test")
    monkeypatch.setenv("WEREAD_CONTENT_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_PAGE_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_AUTO_ADD_SHELF", "false")  # 书架逻辑另有测试覆盖
    monkeypatch.delenv("WEREAD_ENABLED", raising=False)
    monkeypatch.delenv("ARTICLE_SOURCE", raising=False)
    wc.reset_shelf_cache()
    yield
    wc.reset_shelf_cache()


@pytest.fixture
def poller():
    return RSSPoller()


def _install_weread(monkeypatch, *, articles=None, content=None, error=None):
    """把微信读书接口换成固定返回。"""
    async def _request(self, method, path, *, params=None, json_data=None,
                       as_json=True, interval=0.0):
        if error is not None:
            raise error
        if path == "/web/mp/articles":
            if int(params.get("offset", 0)) != 0:
                return {"reviews": []}
            return {"reviews": [
                {"createTime": 1778580000,
                 "subReviews": [{"review": {
                     "reviewId": f"{BOOK_ID}_{a}",
                     "createTime": 1778580000,
                     "mpInfo": {"title": a, "time": 1778580000, "originalId": a},
                 }}]}
                for a in (articles or [])
            ]}
        if path == "/web/mp/content":
            return content or ""
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(WereadClient, "_request", _request)


# ── 列表通道切换 ──────────────────────────────────────────

async def test_falls_back_to_weread_when_backend_errors(poller, monkeypatch):
    async def _mp_fails(self, fakeid, creds):
        raise MpChannelError("ret=200013 freq control")

    monkeypatch.setattr(RSSPoller, "_fetch_from_mp", _mp_fails)
    _install_weread(monkeypatch, articles=["tok1"])

    articles = await poller._fetch_article_list(
        FAKEID, {"token": "t", "cookie": "c"}, use_mp=True, use_weread=True
    )
    assert [a["title"] for a in articles] == ["tok1"]
    assert articles[0]["review_id"] == f"{BOOK_ID}_tok1"


async def test_backend_error_without_weread_keeps_old_behaviour(poller, monkeypatch):
    """没配微信读书时，后台软失败仍然只是返回空列表，不打断整轮轮询。"""
    async def _mp_fails(self, fakeid, creds):
        raise MpChannelError("ret=200013 freq control")

    monkeypatch.setattr(RSSPoller, "_fetch_from_mp", _mp_fails)

    articles = await poller._fetch_article_list(
        FAKEID, {"token": "t", "cookie": "c"}, use_mp=True, use_weread=False
    )
    assert articles == []


async def test_invalid_fakeid_not_blacklisted_when_weread_still_has_articles(poller, monkeypatch):
    """后台说号没了，但微信读书还读得到 —— 不该拉黑。"""
    async def _mp_fails(self, fakeid, creds):
        raise WechatInvalidFakeidError("invalid args")

    monkeypatch.setattr(RSSPoller, "_fetch_from_mp", _mp_fails)
    _install_weread(monkeypatch, articles=["tok1"])

    articles = await poller._fetch_article_list(
        FAKEID, {"token": "t", "cookie": "c"}, use_mp=True, use_weread=True
    )
    assert len(articles) == 1


async def test_invalid_fakeid_still_raises_when_weread_empty(poller, monkeypatch):
    """两条通道都拿不到才确认是死号，保留原有拉黑逻辑。"""
    async def _mp_fails(self, fakeid, creds):
        raise WechatInvalidFakeidError("invalid args")

    monkeypatch.setattr(RSSPoller, "_fetch_from_mp", _mp_fails)
    _install_weread(monkeypatch, articles=[])

    with pytest.raises(WechatInvalidFakeidError):
        await poller._fetch_article_list(
            FAKEID, {"token": "t", "cookie": "c"}, use_mp=True, use_weread=True
        )


async def test_invalid_fakeid_raises_when_weread_also_errors(poller, monkeypatch):
    async def _mp_fails(self, fakeid, creds):
        raise WechatInvalidFakeidError("invalid args")

    monkeypatch.setattr(RSSPoller, "_fetch_from_mp", _mp_fails)
    _install_weread(monkeypatch, error=WereadError(-2012, "login timeout", retriable=False))

    with pytest.raises(WechatInvalidFakeidError):
        await poller._fetch_article_list(
            FAKEID, {"token": "t", "cookie": "c"}, use_mp=True, use_weread=True
        )


async def test_weread_only_mode_skips_backend(poller, monkeypatch):
    async def _mp_should_not_run(self, fakeid, creds):
        raise AssertionError("公众号后台不该被调用")

    monkeypatch.setattr(RSSPoller, "_fetch_from_mp", _mp_should_not_run)
    _install_weread(monkeypatch, articles=["tok1"])

    articles = await poller._fetch_article_list(
        FAKEID, {}, use_mp=False, use_weread=True
    )
    assert len(articles) == 1


async def test_weread_failure_returns_empty_not_exception(poller, monkeypatch):
    async def _mp_fails(self, fakeid, creds):
        raise MpChannelError("boom")

    monkeypatch.setattr(RSSPoller, "_fetch_from_mp", _mp_fails)
    _install_weread(monkeypatch, error=WereadError(-2012, "login timeout", retriable=False))

    assert await poller._fetch_article_list(
        FAKEID, {"token": "t", "cookie": "c"}, use_mp=True, use_weread=True
    ) == []


# ── 正文通道切换 ──────────────────────────────────────────

async def test_weread_sourced_articles_skip_direct_scraping(poller, monkeypatch):
    """微信读书列表来的文章，正文也走读书接口，完全不碰 mp.weixin.qq.com。"""
    async def _must_not_fetch(*args, **kwargs):
        raise AssertionError("不该直抓 mp.weixin.qq.com")

    monkeypatch.setattr(article_fetcher, "fetch_articles_batch", _must_not_fetch)
    _install_weread(monkeypatch, content='<div id="js_content"><p>读书正文</p></div>')

    articles = [{"link": LINK, "title": "A", "review_id": f"{BOOK_ID}_tok1"}]
    result = await poller._enrich_articles_content(FAKEID, articles)

    assert "读书正文" in result[0]["content"]
    assert "读书正文" in result[0]["plain_content"]


async def test_weread_fills_content_when_direct_scrape_blocked(poller, monkeypatch):
    """直抓被微信风控挡下（拿不到正文）时，用微信读书兜底补齐。"""
    async def _blocked(urls, **kwargs):
        # 微信风控页：有响应但不是正文
        return {u: "<html><body>请输入图片中的字符 verifycode</body></html>" for u in urls}

    monkeypatch.setattr(article_fetcher, "fetch_articles_batch", _blocked)
    _install_weread(monkeypatch, content='<div id="js_content"><p>兜底正文</p></div>')

    articles = [{"link": LINK, "title": "A"}]   # 后台来源，没有 review_id
    result = await poller._enrich_articles_content(FAKEID, articles)

    # reviewId 由 fakeid + 短链 token 现推
    assert "兜底正文" in result[0]["content"]


async def test_no_weread_cookie_leaves_direct_result_untouched(poller, monkeypatch):
    async def _blocked(urls, **kwargs):
        return {u: "<html><body>nothing</body></html>" for u in urls}

    monkeypatch.setattr(article_fetcher, "fetch_articles_batch", _blocked)
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)

    articles = [{"link": LINK, "title": "A"}]
    result = await poller._enrich_articles_content(FAKEID, articles)
    assert result[0].get("content", "") == ""


async def test_enrich_caps_at_20_articles(poller, monkeypatch):
    async def _empty(urls, **kwargs):
        return {u: "" for u in urls}

    monkeypatch.setattr(article_fetcher, "fetch_articles_batch", _empty)
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)

    articles = [{"link": f"https://mp.weixin.qq.com/s/t{i}"} for i in range(30)]
    assert len(await poller._enrich_articles_content(FAKEID, articles)) == 20
