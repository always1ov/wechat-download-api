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
