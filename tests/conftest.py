#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pytest 公共配置：把项目根加入 sys.path，并把测试用 SQLite 指到临时目录。

RSS_DB_PATH 必须在 import utils.rss_store 之前设置（模块导入时就读它），
conftest 在收集测试模块之前执行，正好满足这个顺序。
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["RSS_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="wda-test-")) / "rss.db")
# TestClient 会跑 lifespan，别在测试里真的起轮询线程
os.environ.setdefault("SKIP_BACKGROUND_TASKS", "1")


import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    """建表。风控分支会写 verification_count，缺表会直接抛 OperationalError。"""
    from utils import rss_store

    rss_store.init_db()


@pytest.fixture(autouse=True)
def _reset_weread_singletons():
    """把微信读书那几个进程级单例状态清干净再跑下一个用例。

    weread_auth / 书架缓存 / 续期冷却 / i 域探测结果都是模块级的，
    上一个用例（比如续期成功后写了 _runtime_cookie）会串到下一个用例，
    表现为「单独跑通过、全量跑失败」。
    """
    from utils import weread_client as wc

    def clear():
        wc.weread_auth._runtime_cookie = ""
        wc.weread_auth._cache = {}
        wc.weread_auth._last_loaded_at = 0.0
        wc.reset_shelf_cache()

    clear()
    yield
    clear()
