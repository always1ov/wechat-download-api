#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 tmwgsicp
# Licensed under the GNU Affero General Public License v3.0
# See LICENSE file in the project root for full license text.
# SPDX-License-Identifier: AGPL-3.0-only
"""
微信读书通道管理路由

公众号后台凭证约 4 天过期、还有频率风控；配好微信读书 Cookie 后，后台失效时
轮询器和文章接口会自动切到微信读书继续采集。这里提供 Cookie 的配置/校验，
以及直连微信读书的文章列表/正文接口，便于排查「到底是哪条通道挂了」。
"""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from utils import rss_store, weread_client
from utils.weread_client import WereadClient, WereadError, weread_auth
from utils.weread_qr import weread_qr_login

logger = logging.getLogger(__name__)

router = APIRouter()


class WereadResponse(BaseModel):
    """通用响应"""
    success: bool = Field(..., description="是否成功")
    data: Optional[Dict] = Field(None, description="返回数据，失败时为 null")
    error: Optional[str] = Field(None, description="错误信息，成功时为 null")


class CookieRequest(BaseModel):
    """Cookie 配置请求"""
    cookie: str = Field(
        ...,
        description="微信读书 Cookie，需含 wr_skey / wr_vid / wr_rt。"
                    "取法：浏览器登录 weread.qq.com → F12 Network → 任意请求 → "
                    "复制 Request Headers 里的完整 Cookie",
    )


class ShelfRequest(BaseModel):
    """加入书架请求"""
    fakeid: str = Field(..., description="公众号 FakeID，或已换算好的 MP_WXS_* bookId")
    nickname: str = Field("", description="公众号名称（仅用于日志/提示）")


@router.get("/weread/status", response_model=WereadResponse, summary="微信读书通道状态")
async def weread_status():
    """
    查看微信读书通道的配置状态（不回传完整 Cookie）。

    **返回字段（均在 `data` 下）：**
    - `configured`: 是否已配置 Cookie
    - `enabled`: 通道是否启用（默认「配了 Cookie 就启用」，可用 WEREAD_ENABLED 覆盖）
    - `env_managed`: Cookie 是否由 WEREAD_COOKIE 环境变量托管（是则管理页写入不生效）
    - `vid`: Cookie 里的 wr_vid
    - `article_source`: 取数策略 auto / mp / weread
    - `auto_add_to_shelf`: 采集前是否自动把公众号加入书架
    """
    info = weread_auth.get_info()
    info.update({
        "article_source": weread_client.article_source(),
        "auto_add_to_shelf": weread_client.auto_add_to_shelf(),
        "content_interval": weread_client.content_interval(),
        "page_interval": weread_client.page_interval(),
        "max_pages": weread_client.max_pages(),
    })
    return WereadResponse(success=True, data=info)


@router.post("/weread/qrcode", response_model=WereadResponse, summary="获取微信读书登录二维码")
async def weread_qrcode():
    """
    开一次微信读书扫码登录，返回二维码（PNG data URI），用微信扫一下即可。

    比手动复制 Cookie 省事，而且拿到的 Cookie 带 wr_rt，之后 wr_skey 过期能自动续期。

    **返回字段（均在 `data` 下）：**
    - `qr_image`: 二维码图片，`data:image/png;base64,...`，可直接塞进 `<img src>`
    - `confirm_url`: 二维码里的链接（自己渲染二维码时用）
    - `state`: 初始状态，`waiting`
    - `expires_in`: 二维码有效期（秒）

    拿到后轮询 `GET /api/weread/qrcode/status` 看扫码结果。
    """
    try:
        data = await weread_qr_login.start()
    except WereadError as e:
        return WereadResponse(success=False, error=e.user_message)
    except Exception as e:
        logger.error("[WeReadQR] 获取二维码失败: %s", e)
        return WereadResponse(success=False, error=f"获取二维码失败: {e}")
    return WereadResponse(success=True, data=data)


@router.get("/weread/qrcode/status", response_model=WereadResponse,
            summary="查询微信读书扫码状态")
async def weread_qrcode_status():
    """
    查询扫码登录进度。前端轮询这个接口（微信读书那边的长轮询由后台任务扛着）。

    **`state` 取值：**
    - `waiting`: 等待扫码
    - `scanned`: 已扫码，等手机上确认
    - `confirmed`: 登录成功，Cookie 已验证并保存
    - `expired`: 二维码过期，重新获取
    - `error`: 出错，看 `message`
    - `idle`: 还没开始
    """
    return WereadResponse(success=True, data=weread_qr_login.status())


@router.delete("/weread/qrcode", response_model=WereadResponse, summary="取消微信读书扫码登录")
async def cancel_weread_qrcode():
    """取消当前扫码会话，停掉后台轮询。"""
    await weread_qr_login.cancel()
    return WereadResponse(success=True, data=weread_qr_login.status())


@router.post("/weread/cookie", response_model=WereadResponse, summary="配置微信读书 Cookie")
async def save_weread_cookie(req: CookieRequest):
    """
    保存微信读书 Cookie 到 `data/.weread.json`，并立即校验一次有效性。

    **请求体参数：**
    - **cookie** (必填): 完整的 weread.qq.com Cookie

    保存成功即返回 success=true；校验结果在 `data.verified` / `data.verify_message` 里，
    校验失败不代表没保存（可能只是网络不通）。
    """
    ok, message = weread_auth.save_cookie(req.cookie)
    if not ok:
        return WereadResponse(success=False, error=message)

    verified, verify_message = False, ""
    try:
        async with WereadClient() as client:
            verified, verify_message = await client.verify()
    except Exception as e:  # 网络异常不该让保存动作失败
        verify_message = f"校验请求失败: {e}"
        logger.warning("[WeRead] Cookie 校验异常: %s", e)

    info = weread_auth.get_info()
    info.update({"message": message, "verified": verified,
                 "verify_message": verify_message})
    return WereadResponse(success=True, data=info)


@router.delete("/weread/cookie", response_model=WereadResponse, summary="清除微信读书 Cookie")
async def clear_weread_cookie():
    """删除已保存的微信读书 Cookie（不影响 WEREAD_COOKIE 环境变量）。"""
    ok = weread_auth.clear()
    if not ok:
        return WereadResponse(success=False, error="清除失败，请检查 data 目录权限")
    return WereadResponse(success=True, data=weread_auth.get_info())


@router.post("/weread/renew", response_model=WereadResponse, summary="手动续期微信读书登录态")
async def renew_weread_cookie():
    """
    用 Cookie 里的 `wr_rt` 换一个新的 `wr_skey`（POST `/web/login/renewal`）。

    `wr_skey` 是短效令牌，过期后接口返回 -2012；只要 `wr_rt` 还在就能一直换新的，
    不用重新扫码。正常情况下遇到 -2012 会自动续期，这个接口用于手动触发或排查。
    """
    if not weread_auth.is_configured():
        return WereadResponse(success=False, error=weread_client.COOKIE_MISSING_MSG)
    try:
        async with WereadClient() as client:
            await client.renew_cookie()
            valid, message = await client.verify()
    except WereadError as e:
        return WereadResponse(success=False, error=e.user_message)
    except Exception as e:
        return WereadResponse(success=False, error=f"续期失败: {e}")

    info = weread_auth.get_info()
    info.update({"valid": valid, "message": message})
    return WereadResponse(success=valid, data=info, error=None if valid else message)


@router.post("/weread/verify", response_model=WereadResponse, summary="校验微信读书 Cookie")
async def verify_weread_cookie():
    """打一次最轻的微信读书接口，确认当前 Cookie 是否还有效。"""
    if not weread_auth.is_configured():
        return WereadResponse(success=False, error=weread_client.COOKIE_MISSING_MSG)
    try:
        async with WereadClient() as client:
            valid, message = await client.verify()
    except Exception as e:
        return WereadResponse(success=False, error=f"校验请求失败: {e}")
    return WereadResponse(success=valid, data={"valid": valid, "message": message},
                          error=None if valid else message)


@router.post("/weread/shelf", response_model=WereadResponse, summary="把公众号加入微信读书书架")
async def add_to_shelf(req: ShelfRequest):
    """
    把公众号加入微信读书书架（= 在微信读书里关注它）。

    微信读书只对**书架上**的公众号返回文章列表，所以采集前必须先加入书架。
    正常采集流程会自动做这一步，这里用于手动补做或排查。

    **请求体参数：**
    - **fakeid** (必填): 公众号 FakeID，或 `MP_WXS_*` 形式的 bookId
    - **nickname** (可选): 公众号名称，仅用于提示文案
    """
    try:
        book_id = weread_client.fakeid_to_book_id(req.fakeid)
    except WereadError as e:
        return WereadResponse(success=False, error=e.user_message)

    try:
        async with WereadClient() as client:
            ok, detail = await client.ensure_on_shelf(book_id, req.nickname)
    except WereadError as e:
        return WereadResponse(success=False, error=e.user_message)
    except Exception as e:
        return WereadResponse(success=False, error=f"加入书架失败: {e}")

    data = {"book_id": book_id, "fakeid": req.fakeid, "detail": detail}
    return WereadResponse(success=ok, data=data, error=None if ok else detail)


@router.get("/weread/diagnose", response_model=WereadResponse, summary="诊断微信读书通道")
async def weread_diagnose(
    fakeid: str = Query("", description="要测的公众号 FakeID 或 MP_WXS_* bookId（可选，"
                                        "不填只测登录态和搜索）"),
):
    """
    把微信读书这条链路逐步跑一遍，指出到底卡在哪一步。

    「拉不到文章」可能断在任意环节：Cookie 缺字段、登录态过期、公众号不在书架、
    列表接口被风控、正文接口挂了……逐个报错才好定位，所以每一步都单独试、
    单独记结果，不会因为前面失败就中断后面的检查。

    把返回的 `steps` 整段贴出来即可定位问题。
    """
    steps = []

    def rec(name, ok, detail="", hint=""):
        steps.append({"step": name, "ok": bool(ok), "detail": str(detail)[:400],
                      "hint": hint})

    def err_detail(e):
        if isinstance(e, WereadError):
            return f"code={e.code} msg={e.message}"
        return f"{type(e).__name__}: {e}"

    # ── 1. Cookie 本身 ──
    cookie = weread_auth.get_cookie()
    pairs = weread_client.parse_cookie(cookie)
    rec("Cookie 已配置", bool(cookie),
        f"来源={'环境变量 WEREAD_COOKIE' if weread_auth.is_env_managed() else 'data/.weread.json'}"
        if cookie else "未配置",
        "" if cookie else "到管理页点「扫码登录微信读书」")
    rec("含 wr_skey", "wr_skey" in pairs, "", "缺了就是没登录成功，重新扫码")
    rec("含 wr_rt", "wr_rt" in pairs, "",
        "缺了无法自动续期，wr_skey 一过期就得重新扫码")
    rec("含 wr_vid", "wr_vid" in pairs, "", "缺了加入书架会失败")

    if not cookie:
        return WereadResponse(success=False, data={"steps": steps},
                              error=weread_client.COOKIE_MISSING_MSG)

    # ── 2. 登录态（先不自动续期，看真实状态）──
    login_ok = False
    try:
        async with WereadClient(allow_renew=False) as probe:
            login_ok, message = await probe.verify()
        rec("登录态有效 (/web/shelf/sync)", login_ok, message,
            "" if login_ok else "下一步的续期若成功即可自愈")
    except Exception as e:
        rec("登录态有效 (/web/shelf/sync)", False, err_detail(e))

    # ── 3. 续期（登录态不行时这是自愈手段）──
    if not login_ok:
        try:
            async with WereadClient(allow_renew=False) as probe:
                await probe.renew_cookie()
                login_ok, message = await probe.verify()
            rec("续期后登录态 (/web/login/renewal)", login_ok, message,
                "" if login_ok else "wr_rt 也失效了，必须重新扫码")
        except Exception as e:
            rec("续期 (/web/login/renewal)", False, err_detail(e),
                "wr_rt 失效或接口有变，需要重新扫码")

    async with WereadClient() as client:
        # ── 4. App 域（i.weread.qq.com）──
        try:
            found = await client.search_mp_accounts("人民日报", count=5)
            rec("App 域可用 (/store/search)", True, f"搜到 {len(found)} 个公众号",
                "" if found else "接口通但没结果，可能是关键词或响应结构变了")
        except Exception as e:
            rec("App 域可用 (/store/search)", False, err_detail(e),
                "App 域对登录态更严；不通不影响网页域采集，只是搜公众号得靠后台")

        if not fakeid:
            return WereadResponse(success=True, data={
                "steps": steps,
                "note": "带上 ?fakeid=xxx 可继续测该公众号的书架/列表/正文",
            })

        # ── 5. bookId 换算 ──
        try:
            book_id = weread_client.fakeid_to_book_id(fakeid)
            rec("fakeid → bookId", True, book_id)
        except WereadError as e:
            rec("fakeid → bookId", False, err_detail(e),
                "fakeid 格式不对，应是后台搜索返回的 base64 串")
            return WereadResponse(success=False, data={"steps": steps},
                                  error="fakeid 无法换算成 bookId")

        # ── 6. 书架（微信读书只对书架上的号返回文章）──
        try:
            on_shelf = await client.get_shelf_book_ids([book_id])
            rec("查询书架 (/web/shelf/bookIds)", True,
                f"在架={book_id in (on_shelf or [])} 原始={on_shelf}")
        except Exception as e:
            rec("查询书架 (/web/shelf/bookIds)", False, err_detail(e))

        try:
            await client.add_to_shelf(book_id)
            rec("加入书架 (/mp/shelf/addToShelf)", True, "已加入或本来就在")
        except Exception as e:
            rec("加入书架 (/mp/shelf/addToShelf)", False, err_detail(e),
                "不在书架微信读书就不返回文章，这一步失败基本注定拉不到")

        # ── 7. 三条取列表的路，逐个试 ──
        review_id = ""
        try:
            payload = await client.get_articles_page(book_id, offset=0)
            articles, groups = weread_client.parse_mp_articles(payload, book_id)
            review_id = articles[0]["review_id"] if articles else ""
            rec("网页域列表 (/web/mp/articles)", True,
                f"{len(articles)} 篇 / {groups} 组",
                "" if articles else "接口通但 0 篇：该号可能没在微信读书发过文章")
        except Exception as e:
            rec("网页域列表 (/web/mp/articles)", False, err_detail(e))

        try:
            payload = await client.get_app_articles_page(book_id, offset=0)
            app_articles, _ = weread_client.parse_app_articles(payload, book_id)
            review_id = review_id or (app_articles[0]["review_id"] if app_articles else "")
            rec("App 域列表 (/book/articles)", True, f"{len(app_articles)} 篇")
        except Exception as e:
            rec("App 域列表 (/book/articles)", False, err_detail(e))

        try:
            payload = await client.get_cover(book_id)
            latest = weread_client.parse_mp_cover(payload, book_id)
            review_id = review_id or (latest or {}).get("review_id", "")
            rec("最新一篇兜底 (/api/mp/cover)", True,
                f"《{(latest or {}).get('title', '')}》")
        except Exception as e:
            rec("最新一篇兜底 (/api/mp/cover)", False, err_detail(e))

        # ── 8. 正文 ──
        if review_id:
            try:
                result = await client.fetch_article_content(review_id)
                rec("取正文 (/web/mp/content)", True,
                    f"{len(result.get('content', ''))} 字符, "
                    f"{len(result.get('images', []))} 张图")
            except Exception as e:
                rec("取正文 (/web/mp/content)", False, err_detail(e))
        else:
            rec("取正文 (/web/mp/content)", False,
                "前面没拿到任何 reviewId，跳过", "先解决上面的列表问题")

    failed = [s["step"] for s in steps if not s["ok"]]
    return WereadResponse(
        success=not failed,
        data={"steps": steps, "failed": failed, "book_id": book_id},
        error=None if not failed else f"以下环节失败: {', '.join(failed)}",
    )


@router.get("/weread/search", response_model=WereadResponse, summary="通过微信读书搜索公众号")
async def weread_search(
    query: str = Query(..., description="公众号名称或关键词"),
    count: int = Query(15, description="返回数量", ge=1, le=50),
):
    """
    用微信读书搜公众号（i 域 `/store/search`），完全不经过公众号后台。

    这是本项目摆脱「搜索必须依赖公众号后台」的路子：搜到的 `MP_WXS_*` 会换算回
    `fakeid`，可直接喂给 `/api/public/articles`、`/api/rss/subscribe` 等既有接口。

    i 域只认微信读书 App 的 UA 和**已续期**的 `wr_skey`；不可用时本接口报错，
    而 `/api/public/searchbiz` 会自动退回公众号后台。
    """
    if not weread_auth.is_configured():
        return WereadResponse(success=False, error=weread_client.COOKIE_MISSING_MSG)
    try:
        async with WereadClient() as client:
            accounts = await client.search_mp_accounts(query, count=count)
    except WereadError as e:
        return WereadResponse(success=False, error=e.user_message)
    except Exception as e:
        logger.error("[WeRead] 搜索异常: %s", e)
        return WereadResponse(success=False, error=f"搜索失败: {e}")

    return WereadResponse(success=True, data={
        "list": accounts,
        "total": len(accounts),
        "source": "weread",
    })


@router.get("/weread/articles", response_model=WereadResponse, summary="通过微信读书获取文章列表")
async def weread_articles(
    fakeid: str = Query(..., description="公众号 FakeID，或 MP_WXS_* 形式的 bookId"),
    count: int = Query(10, description="获取数量，最大 100", ge=1, le=100),
):
    """
    直连微信读书取文章列表，完全不经过公众号后台。

    翻页上限由 `WEREAD_MAX_PAGES` 控制；列表接口被风控时自动退回
    `/api/mp/cover`（只能取到最新一篇）。
    """
    if not weread_auth.is_configured():
        return WereadResponse(success=False, error=weread_client.COOKIE_MISSING_MSG)

    sub = rss_store.get_subscription(fakeid)
    nickname = (sub or {}).get("nickname", "")

    try:
        async with WereadClient() as client:
            articles: List[Dict] = await client.list_articles(
                fakeid, limit=count, nickname=nickname
            )
    except WereadError as e:
        return WereadResponse(success=False, error=e.user_message)
    except Exception as e:
        logger.error("[WeRead] 文章列表请求异常: %s", e)
        return WereadResponse(success=False, error=f"请求失败: {e}")

    return WereadResponse(success=True, data={
        "articles": articles,
        "total": len(articles),
        "count": len(articles),
        "book_id": weread_client.fakeid_to_book_id(fakeid),
        "source": "weread",
    })


@router.get("/weread/content", response_model=WereadResponse, summary="通过微信读书获取文章正文")
async def weread_content(
    review_id: str = Query("", description="微信读书 reviewId，形如 MP_WXS_123_<token>",
                           alias="review_id"),
    url: str = Query("", description="文章短链 https://mp.weixin.qq.com/s/<token>"),
    fakeid: str = Query("", description="公众号 FakeID，配合 url 使用"),
):
    """
    直连微信读书取文章正文，绕开 mp.weixin.qq.com 的验证码风控。

    二选一：直接给 `review_id`，或给 `url` + `fakeid`（由二者推导 reviewId）。
    """
    if not weread_auth.is_configured():
        return WereadResponse(success=False, error=weread_client.COOKIE_MISSING_MSG)

    target = review_id.strip()
    if not target:
        if not url or not fakeid:
            return WereadResponse(
                success=False,
                error="请提供 review_id，或同时提供 url 与 fakeid"
            )
        target = weread_client.review_id_from_article_url(url, fakeid)
        if not target:
            return WereadResponse(
                success=False,
                error="无法从该链接推导 reviewId（只支持 https://mp.weixin.qq.com/s/<token> 短链）"
            )

    try:
        async with WereadClient() as client:
            result = await client.fetch_article_content(target)
    except WereadError as e:
        return WereadResponse(success=False, error=e.user_message)
    except Exception as e:
        logger.error("[WeRead] 正文请求异常: %s", e)
        return WereadResponse(success=False, error=f"请求失败: {e}")

    return WereadResponse(success=True, data={
        "review_id": target,
        "link": weread_client.review_id_to_mp_url(target),
        "content": result.get("content", ""),
        "plain_content": result.get("plain_content", ""),
        "images": result.get("images", []),
    })
