"""Маршруты FastAPI, полностью воспроизводящие API оригинального проекта."""

import posixpath
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from . import config
from .db import db
from .downloader import download_manager
from .paths import PathError, resolve_dest
from .yandex import TokenInvalidError, YandexAPIError, YandexClient

router = APIRouter(prefix="/api")


# --- Модели запросов ---

class TokenRequest(BaseModel):
    token: str


class TaskItem(BaseModel):
    path: str
    type: Optional[str] = "folder"


class CreateTaskRequest(BaseModel):
    dest: str
    items: List[Dict[str, Any]]
    concurrency: Optional[int] = None


# --- Эндпоинты ---

@router.get("/oauth-url/")
async def get_oauth_url():
    """Ссылка OAuth-авторизации (implicit-флоу) с ClientID из .env."""
    if not config.YANDEX_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="YANDEX_CLIENT_ID не задан — добавьте его в .env",
        )
    return {"url": f"https://oauth.yandex.ru/authorize?response_type=token&client_id={config.YANDEX_CLIENT_ID}"}


@router.get("/token/")
async def get_token_status():
    acc = db.get_account()
    if not acc:
        return {"connected": False}
    token = acc["token"]
    masked = "••••" + token[-4:] if len(token) >= 4 else "••••"
    return {
        "connected": True,
        "login": acc.get("login", ""),
        "used_bytes": acc.get("used_bytes", 0),
        "total_bytes": acc.get("total_bytes", 0),
        "token_masked": masked,
    }


@router.post("/token/")
async def save_token(req: TokenRequest):
    token = (req.token or "").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Вставьте токен")

    client = YandexClient(token)
    try:
        async with client:
            info = await client.disk_info()
            login = await client.user_login()
    except TokenInvalidError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Токен недействителен или истёк. Получите новый.",
        )
    except YandexAPIError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ошибка Яндекс Диска: {e.message}",
        )

    db.save_account(
        token=token,
        login=login,
        total_bytes=int(info.get("total_bytes", 0)),
        used_bytes=int(info.get("used_bytes", 0)),
    )

    return {
        "connected": True,
        "login": login,
        "used_bytes": int(info.get("used_bytes", 0)),
        "total_bytes": int(info.get("total_bytes", 0)),
    }


@router.delete("/token/")
async def delete_token():
    db.delete_account()
    return {"connected": False}


@router.get("/disk/browse/")
async def browse_disk(path: str = Query("/", alias="path")):
    acc = db.get_account()
    if not acc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Подключите Яндекс Диск",
        )

    client = YandexClient(acc["token"])
    try:
        items = []
        async with client:
            async for it in client.list_folder(path):
                items.append(it)
        items.sort(key=lambda x: (x["type"] != "folder", x["name"].lower()))
    except TokenInvalidError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Токен истёк — подключите Яндекс Диск снова",
        )
    except YandexAPIError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=e.message,
        )

    parent = posixpath.dirname(path.rstrip("/")) or "/"
    return {"path": path, "parent": parent, "items": items}


@router.get("/tasks/")
async def list_tasks():
    tasks = db.list_tasks(limit=50)
    payloads = [download_manager.get_task_payload(t["id"]) or t for t in tasks]
    return {"tasks": payloads}


@router.post("/tasks/", status_code=status.HTTP_201_CREATED)
async def create_task(req: CreateTaskRequest):
    acc = db.get_account()
    if not acc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Подключите Яндекс Диск",
        )

    dest = (req.dest or "").strip()
    if not dest:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Укажите папку назначения",
        )

    if not req.items or not isinstance(req.items, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Выберите хотя бы один файл или папку",
        )

    clean: List[Dict[str, Any]] = []
    for it in req.items[:1000]:
        if not isinstance(it, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Некорректный запрос",
            )
        p = (it.get("path") or "").strip()
        if not p.startswith("/"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Некорректный путь на диске: {p}",
            )
        clean.append({
            "path": p,
            "type": it.get("type") if it.get("type") in ("file", "folder") else "folder",
        })

    try:
        dest_path = resolve_dest(dest, config.DOWNLOAD_ROOT, enforce_root=config.ENFORCE_DOWNLOAD_ROOT)
    except PathError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    task_id = str(uuid.uuid4())
    db.create_task(task_id, str(dest_path), clean)
    download_manager.start_task(task_id, str(dest_path), clean, concurrency=req.concurrency)
    return {"id": task_id}


def _open_folder_dialog(initial_dir: Optional[str] = None) -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(
            title="Выберите папку для выгрузки из Яндекс Диска",
            initialdir=initial_dir or None,
        )
        root.destroy()
        return folder or ""
    except Exception:
        return ""


@router.post("/browse-folder/")
async def browse_folder_endpoint():
    """Открывает нативное диалоговое окно Windows для выбора папки на диске."""
    import asyncio
    folder = await asyncio.to_thread(_open_folder_dialog)
    return {"path": folder}


@router.get("/tasks/{task_id}/")
async def get_task(task_id: str):
    payload = download_manager.get_task_payload(task_id)
    if not payload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Не найдено")
    return payload


@router.delete("/tasks/{task_id}/")
async def delete_task_endpoint(task_id: str):
    deleted = download_manager.delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Не найдено")
    return {"deleted": True}


@router.post("/tasks/{task_id}/cancel/")
async def cancel_task_endpoint(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Не найдено")

    if task["status"] in ("done", "error", "cancelled"):
        return {"cancelled": False, "status": task["status"]}

    download_manager.cancel_task(task_id)
    return {"cancelled": True}
