#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信读书客户端的请求级行为：书架、翻页、cover 兜底、正文解析。

统一 patch WereadClient._request，不发真实网络请求。
"""

import pytest

from utils import weread_client as wc
from utils.weread_client import WereadClient, WereadError

BOOK_ID = "MP_WXS_3296382112"
FAKEID = "MzI5NjM4MjExMg=="


def _article_group(review_id, title):
    return {
        "createTime": 1778580000,
        "subReviews": [{
            "review": {
                "reviewId": review_id,
                "createTime": 1778580000,
                "mpInfo": {"title": title, "time": 1778580000, "originalId": review_id.split("_")[-1]},
            }
        }],
    }


class FakeWeread:
    """按 path 分发的假接口。记录调用便于断言翻页参数。"""

    def __init__(self, pages=None, shelf=None, cover=None, content=None, errors=None):
        self.pages = pages or {}          # offset -> payload
        self.shelf = shelf                # None = 接口不可识别
        self.cover = cover
        self.content = content
        self.errors = errors or {}        # path -> WereadError
        self.calls = []

    def install(self, monkeypatch):
        fake = self

        async def _request(self, method, path, *, params=None, json_data=None,
                           as_json=True, interval=0.0):
            fake.calls.append((method, path, dict(params or {}), json_data))
            if path in fake.errors:
                raise fake.errors[path]
            if path == "/web/shelf/bookIds":
                return {"bookIds": fake.shelf if fake.shelf is not None else []}
            if path == "/web/shelf/sync":
                return {"synckey": 1, "books": []}
            if path in ("/mp/shelf/addToShelf", "/web/shelf/add"):
                return {"errCode": 0}
            if path == "/web/mp/articles":
                offset = int(params.get("offset", 0))
                return fake.pages.get(offset, {"reviews": []})
            if path == "/api/mp/cover":
                return fake.cover or {}
            if path == "/web/mp/content":
                return fake.content or ""
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(WereadClient, "_request", _request)
        return self


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=1; wr_skey=test")
    monkeypatch.delenv("WEREAD_ENABLED", raising=False)
    monkeypatch.delenv("ARTICLE_SOURCE", raising=False)
    # 关掉限速，测试不用真等
    monkeypatch.setenv("WEREAD_CONTENT_INTERVAL", "0")
    monkeypatch.setenv("WEREAD_PAGE_INTERVAL", "0")
    wc.reset_shelf_cache()
    yield
    wc.reset_shelf_cache()


async def test_list_articles_adds_to_shelf_when_missing(monkeypatch):
    """微信读书只对书架上的公众号返回文章，不在书架就得先加。"""
    fake = FakeWeread(pages={0: {"reviews": [_article_group("MP_WXS_1_a", "A")]}},
                      shelf=[]).install(monkeypatch)

    async with WereadClient() as client:
        articles = await client.list_articles(FAKEID, limit=10)

    paths = [c[1] for c in fake.calls]
    assert "/web/shelf/bookIds" in paths
    assert "/mp/shelf/addToShelf" in paths
    assert [a["title"] for a in articles] == ["A"]


async def test_list_articles_skips_add_when_already_on_shelf(monkeypatch):
    fake = FakeWeread(pages={0: {"reviews": [_article_group("MP_WXS_1_a", "A")]}},
                      shelf=[BOOK_ID]).install(monkeypatch)

    async with WereadClient() as client:
        await client.list_articles(FAKEID, limit=10)

    assert "/mp/shelf/addToShelf" not in [c[1] for c in fake.calls]


async def test_shelf_state_is_cached_across_calls(monkeypatch):
    fake = FakeWeread(pages={0: {"reviews": [_article_group("MP_WXS_1_a", "A")]}},
                      shelf=[BOOK_ID]).install(monkeypatch)

    async with WereadClient() as client:
        await client.list_articles(FAKEID, limit=10)
        await client.list_articles(FAKEID, limit=10)

    # 第二轮不再查书架，避免每次轮询都多打两个请求
    assert [c[1] for c in fake.calls].count("/web/shelf/bookIds") == 1


async def test_auto_add_to_shelf_can_be_disabled(monkeypatch):
    monkeypatch.setenv("WEREAD_AUTO_ADD_SHELF", "false")
    fake = FakeWeread(pages={0: {"reviews": [_article_group("MP_WXS_1_a", "A")]}},
                      shelf=[]).install(monkeypatch)

    async with WereadClient() as client:
        await client.list_articles(FAKEID, limit=10)

    paths = [c[1] for c in fake.calls]
    assert "/web/shelf/bookIds" not in paths
    assert "/mp/shelf/addToShelf" not in paths


async def test_list_articles_paginates_by_group_count(monkeypatch):
    """offset 按顶层分组数累加 —— 按文章条数累加会漏文章。"""
    fake = FakeWeread(
        pages={
            0: {"reviews": [_article_group("MP_WXS_1_a", "A"),
                            _article_group("MP_WXS_1_b", "B")]},
            2: {"reviews": [_article_group("MP_WXS_1_c", "C")]},
            3: {"reviews": []},
        },
        shelf=[BOOK_ID],
    ).install(monkeypatch)

    async with WereadClient() as client:
        articles = await client.list_articles(FAKEID, limit=10)

    assert [a["title"] for a in articles] == ["A", "B", "C"]
    offsets = [c[2]["offset"] for c in fake.calls if c[1] == "/web/mp/articles"]
    assert offsets == [0, 2, 3]


async def test_list_articles_stops_at_limit(monkeypatch):
    fake = FakeWeread(
        pages={0: {"reviews": [_article_group("MP_WXS_1_a", "A"),
                               _article_group("MP_WXS_1_b", "B")]}},
        shelf=[BOOK_ID],
    ).install(monkeypatch)

    async with WereadClient() as client:
        articles = await client.list_articles(FAKEID, limit=1)

    assert len(articles) == 1
    assert [c[2]["offset"] for c in fake.calls if c[1] == "/web/mp/articles"] == [0]


async def test_list_articles_respects_max_pages(monkeypatch):
    monkeypatch.setenv("WEREAD_MAX_PAGES", "2")
    # 每页 1 组，永远不为空 —— 只有页数上限能停住它
    pages = {i: {"reviews": [_article_group(f"MP_WXS_1_{i}", str(i))]} for i in range(10)}
    fake = FakeWeread(pages=pages, shelf=[BOOK_ID]).install(monkeypatch)

    async with WereadClient() as client:
        articles = await client.list_articles(FAKEID, limit=100)

    assert len(articles) == 2


async def test_list_articles_falls_back_to_cover_when_list_blocked(monkeypatch):
    """列表接口被风控（-2041）且一篇都没拿到时，退回 cover 只取最新一篇。"""
    fake = FakeWeread(
        shelf=[BOOK_ID],
        cover={"reviewId": f"{BOOK_ID}_tok", "title": "最新", "pic": "c.jpg"},
        errors={"/web/mp/articles": WereadError(-2041, "blocked", retriable=False)},
    ).install(monkeypatch)

    async with WereadClient() as client:
        articles = await client.list_articles(FAKEID, limit=10)

    assert [a["title"] for a in articles] == ["最新"]
    assert "/api/mp/cover" in [c[1] for c in fake.calls]


async def test_list_articles_keeps_partial_page_on_mid_pagination_error(monkeypatch):
    """翻到一半报错时保留已拿到的文章，不该整轮丢掉。"""
    class Flaky(FakeWeread):
        def install(self, monkeypatch):
            outer = self

            async def _request(self, method, path, *, params=None, json_data=None,
                               as_json=True, interval=0.0):
                outer.calls.append((method, path, dict(params or {}), json_data))
                if path == "/web/shelf/bookIds":
                    return {"bookIds": [BOOK_ID]}
                if path == "/web/mp/articles":
                    if int(params.get("offset", 0)) == 0:
                        return {"reviews": [_article_group("MP_WXS_1_a", "A")]}
                    raise WereadError(-1, "boom")
                raise AssertionError(path)

            monkeypatch.setattr(WereadClient, "_request", _request)
            return self

    Flaky().install(monkeypatch)
    async with WereadClient() as client:
        articles = await client.list_articles(FAKEID, limit=10)

    assert [a["title"] for a in articles] == ["A"]


async def test_fetch_article_content_extracts_body(monkeypatch):
    FakeWeread(content='<html><body><div id="js_content"><p>正文</p>'
                       '<script>bad()</script></div></body></html>').install(monkeypatch)

    async with WereadClient() as client:
        result = await client.fetch_article_content("MP_WXS_1_tok")

    assert "正文" in result["content"]
    assert "正文" in result["plain_content"]


async def test_fetch_article_content_handles_bare_fragment(monkeypatch):
    """/web/mp/content 有时只返回正文片段，没有 js_content 容器。"""
    FakeWeread(content="<p>裸片段正文</p>").install(monkeypatch)

    async with WereadClient() as client:
        result = await client.fetch_article_content("MP_WXS_1_tok")

    assert "裸片段正文" in result["content"]


async def test_fetch_article_content_raises_on_empty(monkeypatch):
    FakeWeread(content="   ").install(monkeypatch)

    async with WereadClient() as client:
        with pytest.raises(WereadError):
            await client.fetch_article_content("MP_WXS_1_tok")


async def test_verify_uses_shelf_sync_with_empty_user_vid(monkeypatch):
    """userVid 必须传空字符串 —— 传真实 vid 反而会被判 -2012。"""
    fake = FakeWeread(shelf=[]).install(monkeypatch)

    async with WereadClient() as client:
        ok, _ = await client.verify()

    assert ok is True
    call = next(c for c in fake.calls if c[1] == "/web/shelf/sync")
    assert call[2] == {"userVid": "", "synckey": 0}


async def test_verify_reports_expired_cookie(monkeypatch):
    FakeWeread(errors={"/web/shelf/sync": WereadError(-2012, "login timeout",
                                                      retriable=False)}).install(monkeypatch)

    async with WereadClient() as client:
        ok, message = await client.verify()

    assert ok is False
    assert message == wc.COOKIE_EXPIRED_MSG


async def test_request_requires_cookie(monkeypatch):
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    client = WereadClient()
    with pytest.raises(WereadError) as exc:
        await client.get_articles_page(BOOK_ID)
    assert exc.value.code == "missing_cookie"
    assert exc.value.user_message == wc.COOKIE_MISSING_MSG
