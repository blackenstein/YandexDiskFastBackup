"""Маршруты FastAPI, полностью воспроизводящие API оригинального проекта."""

import posixpath
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from . import config
from .db import db
from .downloader import download_manager
from .paths import PathError, resolve_dest, safe_join, yandex_rel
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
    keep_parts: Optional[bool] = False


class VerifyRequest(BaseModel):
    dest: str
    items: Optional[List[Dict[str, Any]]] = None


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


@router.get("/config/")
async def get_config():
    """Текущая конфигурация сервера для веб-интерфейса."""
    return {
        "download_root": str(config.DOWNLOAD_ROOT),
        "concurrency": config.DOWNLOAD_CONCURRENCY,
        "last_dest": db.get_last_dest(),
    }


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
    db.create_task(task_id, str(dest_path), clean, keep_parts=bool(req.keep_parts))
    download_manager.start_task(
        task_id,
        str(dest_path),
        clean,
        concurrency=req.concurrency,
        keep_parts=bool(req.keep_parts),
    )
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


@router.post("/verify/")
async def verify_files(req: VerifyRequest):
    """Сверяет файлы в локальной папке с файлами на Яндекс Диске.

    Выявляет:
    - полностью загруженные и совпавшие файлы (ok);
    - отсутствующие локально файлы (missing);
    - недокачанные файлы (.part);
    - файлы с несовпадающим размером (mismatch).
    """
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

    try:
        dest_path = resolve_dest(dest, config.DOWNLOAD_ROOT, enforce_root=config.ENFORCE_DOWNLOAD_ROOT)
    except PathError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    raw_items = req.items or []
    clean: List[Dict[str, Any]] = []
    for it in raw_items[:1000]:
        if isinstance(it, dict):
            p = (it.get("path") or "").strip()
            if p:
                clean.append({
                    "path": p,
                    "type": it.get("type") if it.get("type") in ("file", "folder") else "folder",
                })

    if not clean:
        clean = [{"path": "/", "type": "folder"}]

    client = YandexClient(acc["token"])
    cloud_files: List[Tuple[str, int]] = []
    try:
        async with client:
            for it in clean:
                async for f_path, f_sz in client.iter_files([it]):
                    cloud_files.append((f_path, f_sz))
    except TokenInvalidError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Токен истёк — подключите Яндекс Диск снова",
        )
    except YandexAPIError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ошибка Яндекс Диска: {e.message}",
        )

    total_files = len(cloud_files)
    total_bytes = sum(sz for _, sz in cloud_files)
    matched_files = 0
    matched_bytes = 0
    missing_files = 0
    missing_bytes = 0
    mismatched_files = 0
    mismatched_bytes = 0
    part_files = 0
    part_bytes = 0

    missing_items: List[Dict[str, Any]] = []
    details: List[Dict[str, Any]] = []

    for disk_path, cloud_sz in cloud_files:
        try:
            rel = yandex_rel(disk_path)
            target = safe_join(dest_path, rel)
            part_target = target.with_name(target.name + ".part")
        except Exception:
            missing_files += 1
            missing_bytes += (cloud_sz or 0)
            missing_items.append({"path": disk_path, "type": "file"})
            continue

        status_str = "missing"
        local_sz = None

        if target.is_file():
            try:
                local_sz = target.stat().st_size
                if cloud_sz is None or cloud_sz == 0 or local_sz == int(cloud_sz):
                    status_str = "ok"
                    matched_files += 1
                    matched_bytes += (cloud_sz or local_sz or 0)
                else:
                    status_str = "mismatch"
                    mismatched_files += 1
                    mismatched_bytes += (cloud_sz or 0)
                    missing_items.append({"path": disk_path, "type": "file"})
            except Exception:
                status_str = "missing"
                missing_files += 1
                missing_bytes += (cloud_sz or 0)
                missing_items.append({"path": disk_path, "type": "file"})
        elif part_target.is_file():
            try:
                part_sz = part_target.stat().st_size
                status_str = "part"
                part_files += 1
                part_bytes += (cloud_sz or 0)
                local_sz = part_sz
                missing_items.append({"path": disk_path, "type": "file"})
            except Exception:
                status_str = "missing"
                missing_files += 1
                missing_bytes += (cloud_sz or 0)
                missing_items.append({"path": disk_path, "type": "file"})
        else:
            status_str = "missing"
            missing_files += 1
            missing_bytes += (cloud_sz or 0)
            missing_items.append({"path": disk_path, "type": "file"})

        if len(details) < 50000:
            details.append({
                "path": disk_path,
                "local_rel": rel,
                "status": status_str,
                "cloud_size": cloud_sz,
                "local_size": local_sz,
            })

    return {
        "dest": str(dest_path),
        "total_files": total_files,
        "total_bytes": total_bytes,
        "matched_files": matched_files,
        "matched_bytes": matched_bytes,
        "missing_files": missing_files,
        "missing_bytes": missing_bytes,
        "mismatched_files": mismatched_files,
        "mismatched_bytes": mismatched_bytes,
        "part_files": part_files,
        "part_bytes": part_bytes,
        "missing_items": missing_items,
        "details": details,
    }
