#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""搜公众号：后台优先，后台不可用时改用微信读书搜索。

searchbiz 是本项目最后一个硬依赖公众号后台的环节，这组用例盯的就是它。
"""

import httpx
import pytest

import routes.search as search_mod
from utils import rss_store
from utils import weread_client as wc
from utils.auth_manager import auth_manager
from utils.weread_client import WereadClient

BOOK_ID = "MP_WXS_3296382112"
FAKEID = "MzI5NjM4MjExMg=="

# install_mp 会替换 httpx.AsyncClient（httpx 是共享模块对象），这里先抓住真的那个
REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=42; wr_skey=k; wr_rt=web%40rt")
    monkeypatch.setenv("WEREAD_PAGE_INTERVAL", "0")
    monkeypatch.delenv("ARTICLE_SOURCE", raising=False)
    monkeypatch.delenv("WEREAD_APP_API", raising=False)
    monkeypatch.setenv("WEREAD_AUTO_RENEW", "false")
    wc.reset_shelf_cache()
    yield
    wc.reset_shelf_cache()


def install_weread(monkeypatch, *, accounts=("某公众号",), fail=False):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if fail:
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(200, json={"books": [
            {"bookInfo": {"bookId": BOOK_ID, "title": t, "cover": "https://x/c.jpg"}}
            for t in accounts
        ]})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        WereadClient, "_new_client",
        lambda self: REAL_ASYNC_CLIENT(transport=transport, follow_redirects=True),
    )
    return calls


def install_mp(monkeypatch, payload):
    """替换 routes.search 里直接用的 httpx.AsyncClient。"""
    calls = []

    class FakeResponse:
        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kwargs):
            calls.append(url)
            return FakeResponse()

    monkeypatch.setattr(search_mod.httpx, "AsyncClient", FakeClient)
    return calls


MP_OK = {"base_resp": {"ret": 0}, "list": [
    {"fakeid": "MzAxMjcyNjI2MQ==", "nickname": "后台搜到的", "alias": "a",
     "round_head_img": "https://y/h.jpg", "service_type": 1}]}
MP_EXPIRED = {"base_resp": {"ret": 200003, "err_msg": "invalid session"}}


async def test_uses_weread_when_backend_not_logged_in(monkeypatch):
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    install_weread(monkeypatch)

    accounts, err = await search_mod.searchbiz_raw("测试")

    assert err is None
    assert accounts[0]["fakeid"] == FAKEID
    assert accounts[0]["nickname"] == "某公众号"
    assert accounts[0]["source"] == "weread"


async def test_backend_success_does_not_touch_weread(monkeypatch):
    monkeypatch.setattr(auth_manager, "get_credentials",
                        lambda: {"token": "t", "cookie": "c"})
    install_mp(monkeypatch, MP_OK)
    weread_calls = install_weread(monkeypatch)

    accounts, err = await search_mod.searchbiz_raw("测试")

    assert err is None
    assert accounts[0]["nickname"] == "后台搜到的"
    assert weread_calls == [], "后台好使就不该多打一次微信读书"


async def test_expired_backend_falls_back_to_weread(monkeypatch):
    monkeypatch.setattr(auth_manager, "get_credentials",
                        lambda: {"token": "t", "cookie": "c"})
    install_mp(monkeypatch, MP_EXPIRED)
    weread_calls = install_weread(monkeypatch)

    accounts, err = await search_mod.searchbiz_raw("测试")

    assert err is None
    assert accounts[0]["nickname"] == "某公众号"
    assert len(weread_calls) == 1


async def test_both_channels_down_reports_backend_error(monkeypatch):
    """两边都不行时，报后台的错（那才是用户能动手解决的）。"""
    monkeypatch.setattr(auth_manager, "get_credentials",
                        lambda: {"token": "t", "cookie": "c"})
    install_mp(monkeypatch, MP_EXPIRED)
    install_weread(monkeypatch, fail=True)

    accounts, err = await search_mod.searchbiz_raw("测试")

    assert accounts == []
    assert "登录" in err


async def test_source_mp_disables_weread_search(monkeypatch):
    monkeypatch.setenv("ARTICLE_SOURCE", "mp")
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    weread_calls = install_weread(monkeypatch)

    accounts, err = await search_mod.searchbiz_raw("测试")

    assert accounts == []
    assert "扫码登录" in err
    assert weread_calls == []


async def test_blacklist_applies_to_weread_results(monkeypatch):
    """黑名单是防「搜到已失效号」的，微信读书这条路也得过一遍。"""
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    install_weread(monkeypatch)
    rss_store.add_to_blacklist(FAKEID, nickname="某公众号", reason="test")
    try:
        accounts, err = await search_mod.searchbiz_raw("测试")
        assert accounts == []
        assert err is None
    finally:
        rss_store.remove_from_blacklist(FAKEID)


async def test_mmbiz_covers_go_through_image_proxy(monkeypatch):
    """mmbiz 封面有防盗链，必须走代理 —— 和后台那条路一样的待遇。"""
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})

    def handler(request):
        return httpx.Response(200, json={"books": [{"bookInfo": {
            "bookId": BOOK_ID, "title": "某公众号",
            "cover": "https://mmbiz.qpic.cn/abc.jpg"}}]})

    monkeypatch.setattr(
        WereadClient, "_new_client",
        lambda self: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler),
                                       follow_redirects=True),
    )

    accounts, _ = await search_mod.searchbiz_raw("测试", base_url="https://example.test")

    assert accounts[0]["round_head_img"].startswith("https://example.test/api/image?url=")


async def test_non_wechat_covers_pass_through(monkeypatch):
    """微信读书自家 CDN 的封面没有防盗链，/api/image 也不放行这个域名，原样返回。"""
    monkeypatch.setattr(auth_manager, "get_credentials", lambda: {})
    install_weread(monkeypatch)

    accounts, _ = await search_mod.searchbiz_raw("测试", base_url="https://example.test")

    assert accounts[0]["round_head_img"] == "https://x/c.jpg"
