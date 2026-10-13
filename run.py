"""Точка запуска приложения без Docker и npm.

Использование:
    python run.py
"""

import sys
import threading
import time
import webbrowser

import uvicorn

from app.config import HOST, PORT, LOG_FILE


def open_browser():
    time.sleep(1.2)
    url = f"http://{HOST}:{PORT}"
    print(f"\n[OK] Открываем браузер: {url}")
    webbrowser.open(url)


def main():
    print("=" * 60)
    print("  Выгрузка из Яндекс Диска (FastAPI + Asyncio)")
    print(f"  Адрес: http://{HOST}:{PORT}")
    print(f"  Логи:  {LOG_FILE}")
    print("  Нажмите Ctrl+C для остановки сервера")
    print("=" * 60)

    # Запускаем авто-открытие браузера в отдельном потоке
    threading.Thread(target=open_browser, daemon=True).start()

    try:
        uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
        sys.exit(0)


if __name__ == "__main__":
    main()
