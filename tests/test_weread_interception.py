#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""风控验证页不能被当成文章正文。

用户在 RSS 阅读器里打开文章，看到的是：
    环境异常
    当前环境异常，完成验证后即可继续访问。
    去验证
这是微信的反爬拦截页被当正文入库了。而且因为 content 非空，
save_articles 的回填逻辑不会再覆盖它 —— 这篇文章会永久坏掉。
"""

import pytest

from utils import rss_store
from utils import weread_client as wc
from utils.weread_client import WereadError

# 用户截图里那页的实际内容
INTERCEPT_HTML = """
<div id="js_content">
  <h2>环境异常</h2>
  <p>当前环境异常，完成验证后即可继续访问。</p>
  <a href="https://weixin.qq.com/verify">去验证</a>
  <p>视频 小程序 赞，轻点两下取消赞 在看，轻点两下取消在看</p>
</div>
"""

REAL_HTML = """
<div id="js_content">
  <p>这是一篇真正的文章正文，讲了一些事情，篇幅也够长。</p>
  <p>第二段内容。</p>
</div>
"""


def test_interception_page_is_rejected():
    with pytest.raises(WereadError) as exc:
        wc.process_content_html(INTERCEPT_HTML, review_id="MP_WXS_1_tok")

    assert exc.value.code == "intercepted"
    assert "风控" in exc.value.message


def test_real_content_still_passes():
    result = wc.process_content_html(REAL_HTML, review_id="MP_WXS_1_tok")
    assert "真正的文章正文" in result["content"]


@pytest.mark.parametrize("text,expected", [
    ("当前环境异常，完成验证后即可继续访问。", True),
    ("请在微信客户端打开链接", True),
    ("这是一篇正常文章，聊了聊环境保护的异常天气", False),
    ("", False),
])
def test_signature_detection(text, expected):
    assert bool(wc.looks_like_interception(text)) is expected


def test_long_article_mentioning_verification_is_not_flagged():
    """正文很长时只扫头部，别让文中偶然出现的词把整篇判死。"""
    body = "正常内容。" * 2000 + "当前环境异常"
    assert wc.looks_like_interception(body) == ""


# ── 已经存坏的文章要能自愈 ──────────────────────────────────

FAKEID = "MzI5NjM4MjExMg=="


@pytest.fixture
def _sub():
    rss_store.add_subscription(FAKEID, nickname="人民日报")
    yield
    rss_store.remove_subscription(FAKEID)


def test_cleanup_blanks_intercepted_content_so_it_can_refetch(_sub):
    rss_store.save_articles(FAKEID, [{
        "aid": "bad1", "title": "标题", "link": "https://mp.weixin.qq.com/s/bad1",
        "digest": "", "cover": "", "author": "x",
        "content": "<p>当前环境异常，完成验证后即可继续访问。</p>",
        "plain_content": "当前环境异常，完成验证后即可继续访问。",
        "publish_time": 1757000000,
    }, {
        "aid": "good1", "title": "好文章", "link": "https://mp.weixin.qq.com/s/good1",
        "digest": "", "cover": "", "author": "x",
        "content": "<p>真正的正文</p>", "plain_content": "真正的正文",
        "publish_time": 1757000001,
    }])

    cleaned = rss_store.clear_intercepted_content()
    assert cleaned >= 1

    arts = {a["aid"]: a for a in rss_store.get_articles(FAKEID, limit=10)}
    assert arts["bad1"]["content"] == "", "拦截页正文没被清掉，就永远不会重抓"
    assert arts["good1"]["content"] == "<p>真正的正文</p>", "正常文章不能被误清"


# ── 撞上验证页时换一条正文路径再试 ─────────────────────────

import httpx
from utils.weread_client import WereadClient

INTERCEPT_BODY = ('<div id="js_content"><h2>环境异常</h2>'
                  '<p>当前环境异常，完成验证后即可继续访问。</p></div>')
REAL_BODY = '<div id="js_content"><p>这是真正的正文内容。</p></div>'


@pytest.fixture
def _cookie(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=1; wr_skey=k; wr_rt=rt")
    monkeypatch.setenv("WEREAD_CONTENT_INTERVAL", "0")
    wc.weread_auth._runtime_cookie = ""
    yield
    wc.weread_auth._runtime_cookie = ""


def _install(monkeypatch, handler):
    monkeypatch.setattr(
        WereadClient, "_new_client",
        lambda self: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                       follow_redirects=True),
    )


async def test_falls_back_to_alternate_content_path(monkeypatch, _cookie):
    """主路径被验证页挡住时，备选路径能拿到就算成功。"""
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/web/mp/content":
            return httpx.Response(200, text=INTERCEPT_BODY)
        if request.url.path == "/api/mp/content":
            return httpx.Response(200, text=REAL_BODY)
        return httpx.Response(200, json={})

    _install(monkeypatch, handler)
    async with WereadClient() as client:
        result = await client.fetch_article_content("MP_WXS_1_tok")

    assert "真正的正文内容" in result["content"]
    assert seen == ["/web/mp/content", "/api/mp/content"]


async def test_first_path_wins_when_it_works(monkeypatch, _cookie):
    """主路径正常时不该多打备选，白白多一次请求。"""
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, text=REAL_BODY)

    _install(monkeypatch, handler)
    async with WereadClient() as client:
        await client.fetch_article_content("MP_WXS_1_tok")

    assert seen == ["/web/mp/content"]


async def test_both_paths_intercepted_still_rejected(monkeypatch, _cookie):
    """两条路都是验证页 → 仍然按 intercepted 处理，绝不入库。"""
    def handler(request):
        return httpx.Response(200, text=INTERCEPT_BODY)

    _install(monkeypatch, handler)
    async with WereadClient() as client:
        with pytest.raises(WereadError) as exc:
            await client.fetch_article_content("MP_WXS_1_tok")

    assert exc.value.code == "intercepted"


async def test_auth_error_does_not_try_alternate_path(monkeypatch, _cookie):
    """登录态失效换路径也没用，别浪费请求。"""
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"errcode": -2012, "errmsg": "login timeout"})

    _install(monkeypatch, handler)
    async with WereadClient(allow_renew=False) as client:
        with pytest.raises(WereadError):
            await client.fetch_article_content("MP_WXS_1_tok")

    assert seen == ["/web/mp/content"], "登录态问题不该再试备选路径"


async def test_json_error_body_on_content_reports_real_reason(monkeypatch, _cookie):
    """正文接口回 JSON 错误体时，要报真实原因而不是「解析不出正文」。

    as_json=False 拿到的是纯文本，登录态失效的 -2012 会被当成 HTML 一路带下去，
    最后报成解析失败 —— 用户照着去查解析，方向就偏了。
    """
    def handler(request):
        return httpx.Response(200, json={"errcode": -2012, "errmsg": "login timeout"})

    _install(monkeypatch, handler)
    async with WereadClient(allow_renew=False) as client:
        with pytest.raises(WereadError) as exc:
            await client.fetch_article_content("MP_WXS_1_tok")

    assert exc.value.code == -2012
    assert exc.value.is_auth_error
