import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Загружаем .env
load_dotenv(BASE_DIR / ".env")

YANDEX_CLIENT_ID = os.environ.get("YANDEX_CLIENT_ID", "").strip()
YANDEX_TOKEN = os.environ.get("YANDEX_TOKEN", "").strip()

# Папка назначения по умолчанию
_raw_download_root = os.environ.get("DOWNLOAD_ROOT", "").strip()
if not _raw_download_root or _raw_download_root == "/data":
    # Если в .env остался старый докеровский /data, на Windows используем папку downloads в корне проекта
    DOWNLOAD_ROOT = (BASE_DIR / "downloads").resolve()
else:
    DOWNLOAD_ROOT = Path(_raw_download_root).expanduser().resolve()

DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

# Ограничивать ли папку назначения строго внутри DOWNLOAD_ROOT (по умолчанию False для Windows)
ENFORCE_DOWNLOAD_ROOT = os.environ.get("ENFORCE_DOWNLOAD_ROOT", "0") == "1"

DOWNLOAD_CONCURRENCY = int(os.environ.get("DOWNLOAD_CONCURRENCY", "8"))
API_QPS = int(os.environ.get("API_QPS", "4"))
FORCE_IPV4 = os.environ.get("FORCE_IPV4", "1") == "1"

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))

DB_DIR = BASE_DIR / "var"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = os.environ.get("DATABASE_PATH", str(DB_DIR / "db.sqlite3"))

# Папка и настройка логирования
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "app.log"


def setup_logging():
    log_format = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Очищаем существующие обработчики, чтобы не дублировать логи
    if not root_logger.handlers:
        # Лог в консоль
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(log_format, date_format))
        root_logger.addHandler(console_handler)

        # Лог в файл с ротацией (до 10 МБ, 5 файлов ротации), UTF-8
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(log_format, date_format))
        root_logger.addHandler(file_handler)


setup_logging()
