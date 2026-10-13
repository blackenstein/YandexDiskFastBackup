import os
import sys
import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# Добавляем корень проекта в путь
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

# Временная БД для тестов
temp_dir = tempfile.mkdtemp(prefix="ydl-test-")
os.environ["DATABASE_PATH"] = str(Path(temp_dir) / "test_db.sqlite3")
os.environ["DOWNLOAD_ROOT"] = str(Path(temp_dir) / "downloads")

from app.db import Database, db
from app.main import app

# Перенаправляем глобальный db на временную базу
db.db_path = str(Path(temp_dir) / "test_db.sqlite3")
db.init_db()


import pytest_asyncio

@pytest_asyncio.fixture
async def async_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
