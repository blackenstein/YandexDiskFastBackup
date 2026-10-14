"""Главное приложение FastAPI: подключение маршрутов, обработка ошибок и статика."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, YANDEX_TOKEN
from .db import db
from .routes import router
from .yandex import TokenInvalidError, YandexAPIError, YandexClient

logger = logging.getLogger("ydl")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Автоподключение токена из .env, если аккаунт ещё не сохранён в БД
    if YANDEX_TOKEN:
        acc = db.get_account()
        if not acc:
            logger.info("Обнаружен YANDEX_TOKEN в окружении, выполняем автоподключение...")
            client = YandexClient(YANDEX_TOKEN)
            try:
                async with client:
                    info = await client.disk_info()
                    login = await client.user_login()
                db.save_account(
                    token=YANDEX_TOKEN,
                    login=login,
                    total_bytes=int(info.get("total_bytes", 0)),
                    used_bytes=int(info.get("used_bytes", 0)),
                )
                logger.info("Успешно подключён диск для пользователя %s", login or "default")
            except (TokenInvalidError, YandexAPIError) as e:
                logger.warning("Не удалось автоматически подключить токен из .env: %s", e)
    yield


app = FastAPI(title="Yandex Disk Downloader", lifespan=lifespan)

# CORS для локальной разработки
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/index.html", "/app.js", "/styles.css") or path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# Форматируем ошибки как {"error": "..."}, чтобы полностью совпадать с контрактом DRF
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
    )


# Подключаем API маршруты
app.include_router(router)

# Раздача статического фронтенда
static_dir = BASE_DIR / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
