"""
开源版（单用户 / SQLite）的 MCP 数据适配层。

把 6 个 MCP tool 需要的能力桥接到 utils.rss_store（sqlite3）+ 搜索/凭证。
与 SaaS 版的多租户 SQLAlchemy 适配层不同：单用户、无 user_id、无付费门禁。
"""
import os
import time
import logging


from utils import rss_store
from utils.image_proxy import proxy_image_url

logger = logging.getLogger(__name__)



def _base_url() -> str:
    return os.getenv("SITE_URL", "").rstrip("/")


class StoreAdapter:
    """单用户数据适配：全部同步 sqlite 读，MCP 调用稀疏，直接调用即可。"""

    async def list_subscriptions(self) -> list[dict]:
        subs = rss_store.list_subscriptions()
        base = _base_url()
        return [{
            "fakeid": s.get("fakeid", ""),
            "nickname": s.get("nickname", ""),
            "alias": s.get("alias", ""),
            "head_img": proxy_image_url(s.get("head_img", ""), base) if s.get("head_img") else "",
            "article_count": s.get("article_count", 0),
            "category": s.get("category_name", ""),
            "created_at": s.get("created_at", 0),
        } for s in subs]

    async def search_accounts(self, query: str) -> list[dict]:
        """搜公众号。走微信读书（App 域搜索 + 书架匹配），不依赖公众号后台。"""
        from routes.search import searchbiz_raw

        accounts, err, hint = await searchbiz_raw(query)
        if err:
            raise RuntimeError(err)
        if not accounts and hint:
            raise RuntimeError(hint)
        return [{
            "fakeid": a.get("fakeid", ""),
            "nickname": a.get("nickname", ""),
            "alias": a.get("alias", ""),
            "service_type": a.get("service_type", 0),
        } for a in accounts]

    async def subscribe_account(self, fakeid: str) -> dict:
        if rss_store.is_blacklisted(fakeid):
            raise ValueError("该公众号已被标记失效，无法订阅")
        if rss_store.get_subscription(fakeid):
            return {"success": True, "message": "已订阅，无需重复"}
        # 尽量取个昵称（失败不阻塞；采集会补全元数据）
        nickname = ""
        try:
            from routes.search import searchbiz_raw
            accounts, _, _ = await searchbiz_raw(fakeid)
            for a in accounts:
                if a.get("fakeid") == fakeid:
                    nickname = a.get("nickname", "")
                    break
        except Exception:
            pass
        rss_store.add_subscription(fakeid, nickname=nickname)
        # 立刻抓一次，别让调用方等下一轮轮询（默认 1 小时）才看到文章
        new_count = 0
        try:
            from utils.rss_poller import rss_poller
            new_count = await rss_poller.fetch_now(fakeid)
        except Exception as e:
            logger.warning("订阅后立即采集失败 %s: %s", fakeid[:8], e)
        return {"success": True,
                "message": f"已订阅 {nickname or fakeid}，抓到 {new_count} 篇新文章"}

    async def unsubscribe_account(self, fakeid: str) -> dict:
        ok = rss_store.remove_subscription(fakeid)
        return {"success": ok, "message": "已取消订阅" if ok else "未找到该订阅"}
