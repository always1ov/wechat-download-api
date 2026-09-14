#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信读书通道：标识换算、响应解析、翻页与兜底逻辑。"""

import pytest

from utils import weread_client as wc
from utils.weread_client import WereadClient, WereadError


# ── 标识换算 ──────────────────────────────────────────────

def test_fakeid_to_book_id_decodes_base64():
    # 实测值：base64("3296382112") == "MzI5NjM4MjExMg=="
    assert wc.fakeid_to_book_id("MzI5NjM4MjExMg==") == "MP_WXS_3296382112"


def test_fakeid_to_book_id_accepts_book_id_and_plain_biz():
    assert wc.fakeid_to_book_id("MP_WXS_3296382112") == "MP_WXS_3296382112"
    assert wc.fakeid_to_book_id("3296382112") == "MP_WXS_3296382112"


def test_fakeid_to_book_id_rejects_garbage():
    # b64decode 对非法输入不一定报错，只会解出乱码 —— 必须挡在这一步，
    # 否则会拿着乱码 bookId 去打微信读书，错误信息完全对不上。
    with pytest.raises(WereadError):
        wc.fakeid_to_book_id("这不是fakeid")
    with pytest.raises(WereadError):
        wc.fakeid_to_book_id("")


def test_book_id_to_fakeid_roundtrip():
    assert wc.book_id_to_fakeid(wc.fakeid_to_book_id("MzI5NjM4MjExMg==")) == "MzI5NjM4MjExMg=="


def test_build_mp_url_keeps_tilde():
    # '~' 是短链 token 的合法字符，转义成 %7E 微信会 302 跳转
    assert wc.build_mp_url("4OcS7~rrtk2Lwe4P0YPiGg") == \
        "https://mp.weixin.qq.com/s/4OcS7~rrtk2Lwe4P0YPiGg"
    assert wc.build_mp_url("") == ""


def test_review_id_to_mp_url():
    assert wc.review_id_to_mp_url("MP_WXS_1_abc~def", "MP_WXS_1") == \
        "https://mp.weixin.qq.com/s/abc~def"
    # 不传 bookId 时按最后一段切
    assert wc.review_id_to_mp_url("MP_WXS_1_abc") == "https://mp.weixin.qq.com/s/abc"
    assert wc.review_id_to_mp_url("") == ""


def test_extract_article_token():
    assert wc.extract_article_token("https://mp.weixin.qq.com/s/tok~en?chksm=1") == "tok~en"
    # 长链没有短链 token，推不出 reviewId
    assert wc.extract_article_token("https://mp.weixin.qq.com/s?__biz=abc&mid=1") == ""
    assert wc.extract_article_token("") == ""


def test_review_id_from_article_url():
    assert wc.review_id_from_article_url(
        "https://mp.weixin.qq.com/s/tok", "MzI5NjM4MjExMg=="
    ) == "MP_WXS_3296382112_tok"
    assert wc.review_id_from_article_url("https://mp.weixin.qq.com/s?__biz=x", "3296382112") == ""
    assert wc.review_id_from_article_url("https://mp.weixin.qq.com/s/tok", "乱码") == ""


# ── 响应解析 ──────────────────────────────────────────────

GROUPED_PAYLOAD = {
    "reviews": [
        {
            "createTime": 1778580000,
            "subReviews": [
                {
                    "reviewId": "MP_WXS_1_review-1",
                    "review": {
                        "reviewId": "MP_WXS_1_review-1",
                        "createTime": 1778580001,
                        "mpInfo": {
                            "title": "标题一",
                            "content": "摘要一",
                            "time": 1778580002,
                            "originalId": "abc~def",
                            "pic_url": "https://example.test/cover.jpg",
                            "readNum": 12,
                            "likeNum": 3,
                        },
                    },
                }
            ],
        }
    ]
}


def test_parse_mp_articles_maps_fields():
    articles, group_count = wc.parse_mp_articles(GROUPED_PAYLOAD, "MP_WXS_1")
    assert group_count == 1
    assert len(articles) == 1
    a = articles[0]
    assert a["aid"] == "MP_WXS_1_review-1"
    assert a["review_id"] == "MP_WXS_1_review-1"
    assert a["title"] == "标题一"
    assert a["digest"] == "摘要一"
    assert a["link"] == "https://mp.weixin.qq.com/s/abc~def"
    assert a["cover"] == "https://example.test/cover.jpg"
    assert a["publish_time"] == 1778580002
    assert a["source"] == "weread"


def test_parse_mp_articles_handles_flat_shape():
    """有的响应不带 subReviews，review 直接挂在顶层分组上。"""
    flat = {"reviews": [{"createTime": 5, "review": {
        "reviewId": "MP_WXS_1_r2", "mpInfo": {"title": "扁平"}}}]}
    articles, group_count = wc.parse_mp_articles(flat, "MP_WXS_1")
    assert group_count == 1
    assert articles[0]["title"] == "扁平"
    # mpInfo 没有 originalId 时，链接由 reviewId 推导
    assert articles[0]["link"] == "https://mp.weixin.qq.com/s/r2"


def test_parse_mp_articles_skips_entries_without_review_id():
    payload = {"reviews": [{"subReviews": [{"review": {"mpInfo": {"title": "无 id"}}}]}]}
    articles, _ = wc.parse_mp_articles(payload, "MP_WXS_1")
    assert articles == []


@pytest.mark.parametrize("code", [-2010, -2012, -2041])
def test_parse_mp_articles_flags_auth_errors_as_non_retriable(code):
    with pytest.raises(WereadError) as exc:
        wc.parse_mp_articles({"errCode": code, "errMsg": "blocked"}, "MP_WXS_1")
    assert exc.value.code == code
    assert exc.value.retriable is False
    assert exc.value.is_auth_error
    assert exc.value.user_message == wc.COOKIE_EXPIRED_MSG


def test_parse_mp_articles_other_errors_stay_retriable():
    with pytest.raises(WereadError) as exc:
        wc.parse_mp_articles({"errCode": -1, "errMsg": "boom"}, "MP_WXS_1")
    assert exc.value.retriable is True


def test_parse_mp_cover():
    article = wc.parse_mp_cover(
        {"reviewId": "MP_WXS_9_tok", "title": "最新一篇", "pic": "p.jpg"}, "MP_WXS_9"
    )
    assert article["review_id"] == "MP_WXS_9_tok"
    assert article["link"] == "https://mp.weixin.qq.com/s/tok"
    assert article["cover"] == "p.jpg"


def test_parse_mp_cover_without_review_id_raises():
    with pytest.raises(WereadError):
        wc.parse_mp_cover({}, "MP_WXS_9")


def test_parse_shelf_book_ids_shapes():
    assert wc.parse_shelf_book_ids({"bookIds": ["a", "b"]}) == ["a", "b"]
    assert wc.parse_shelf_book_ids(["a"]) == ["a"]
    assert wc.parse_shelf_book_ids({"books": [{"bookId": "c"}]}) == ["c"]
    assert wc.parse_shelf_book_ids({"unexpected": 1}) is None


# ── Cookie 存储 ───────────────────────────────────────────

def test_normalize_cookie_collapses_whitespace():
    assert WereadClient  # 引用一下，保持导入可见
    assert wc.WereadAuth.normalize_cookie("  wr_vid=1;\n  wr_skey=k ; ") == "wr_vid=1; wr_skey=k"


def test_extract_vid():
    assert wc.WereadAuth.extract_vid("a=1; wr_vid=123456; b=2") == "123456"
    assert wc.WereadAuth.extract_vid("a=1") == ""


def test_save_cookie_rejects_non_weread_cookie(tmp_path, monkeypatch):
    auth = wc.weread_auth
    monkeypatch.setattr(auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)

    ok, message = auth.save_cookie("slave_sid=xxx; slave_user=yyy")
    assert ok is False
    assert "wr_skey" in message

    ok, _ = auth.save_cookie("wr_vid=42; wr_skey=abc")
    assert ok is True
    assert auth.get_cookie() == "wr_vid=42; wr_skey=abc"
    assert auth.get_info()["vid"] == "42"

    assert auth.clear() is True
    assert auth.get_cookie() == ""


def test_env_cookie_wins_over_stored(tmp_path, monkeypatch):
    auth = wc.weread_auth
    monkeypatch.setattr(auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    auth.save_cookie("wr_vid=1; wr_skey=stored")
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=2; wr_skey=fromenv")
    assert auth.get_cookie() == "wr_vid=2; wr_skey=fromenv"
    assert auth.is_env_managed() is True
    auth.clear()


def test_is_enabled_follows_cookie_and_override(tmp_path, monkeypatch):
    monkeypatch.setattr(wc.weread_auth, "credentials_file", tmp_path / ".weread.json")
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    monkeypatch.delenv("WEREAD_ENABLED", raising=False)
    assert wc.is_enabled() is False

    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=1; wr_skey=k")
    assert wc.is_enabled() is True

    monkeypatch.setenv("WEREAD_ENABLED", "false")
    assert wc.is_enabled() is False


def test_article_source_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("ARTICLE_SOURCE", raising=False)
    assert wc.article_source() == "auto"
    monkeypatch.setenv("ARTICLE_SOURCE", "WeRead")
    assert wc.article_source() == "weread"
    monkeypatch.setenv("ARTICLE_SOURCE", "nonsense")
    assert wc.article_source() == "auto"
