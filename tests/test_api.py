import asyncio
from pathlib import Path
import pytest

from app import config
from app.db import db
from app.downloader import download_manager
from app.yandex import YandexClient


@pytest.mark.asyncio
async def test_oauth_url_requires_client_id(async_client, monkeypatch):
    monkeypatch.setattr(config, "YANDEX_CLIENT_ID", "")
    resp = await async_client.get("/api/oauth-url/")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_oauth_url(async_client, monkeypatch):
    monkeypatch.setattr(config, "YANDEX_CLIENT_ID", "12345abcde")
    resp = await async_client.get("/api/oauth-url/")
    assert resp.status_code == 200
    assert "client_id=12345abcde" in resp.json()["url"]


@pytest.mark.asyncio
async def test_token_flow(async_client, monkeypatch):
    # Очищаем аккаунт
    db.delete_account()

    async def fake_disk_info(self):
        return {"total_bytes": 1000, "used_bytes": 100}

    async def fake_user_login(self):
        return "tester"

    monkeypatch.setattr(YandexClient, "disk_info", fake_disk_info)
    monkeypatch.setattr(YandexClient, "user_login", fake_user_login)

    # 1. До подключения
    resp = await async_client.get("/api/token/")
    assert resp.status_code == 200
    assert resp.json()["connected"] is False

    # 2. Подключение
    resp = await async_client.post("/api/token/", json={"token": "tok-12345"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["connected"] is True
    assert data["login"] == "tester"

    # 3. Проверка статуса (токен маскирован)
    resp = await async_client.get("/api/token/")
    data = resp.json()
    assert data["connected"] is True
    assert data["token_masked"] == "••••2345"
    assert "tok-12345" not in str(data)

    # 4. Отключение
    resp = await async_client.delete("/api/token/")
    assert resp.status_code == 200
    assert resp.json()["connected"] is False


@pytest.mark.asyncio
async def test_browse(async_client, monkeypatch):
    db.save_account(token="dummy-tok", login="test", total_bytes=1000, used_bytes=100)

    async def fake_list(self, path="/"):
        yield {"name": "file.txt", "path": "/file.txt", "type": "file", "size": 42, "modified": ""}
        yield {"name": "folder1", "path": "/folder1", "type": "folder", "size": 0, "modified": ""}

    monkeypatch.setattr(YandexClient, "list_folder", fake_list)

    resp = await async_client.get("/api/disk/browse/?path=/")
    assert resp.status_code == 200
    data = resp.json()
    names = [i["name"] for i in data["items"]]
    # Папки должны идти первыми
    assert names[0] == "folder1"
    assert names[1] == "file.txt"
    assert data["parent"] == "/"


@pytest.mark.asyncio
async def test_task_download_end_to_end(async_client, monkeypatch, tmp_path):
    db.save_account(token="dummy-tok", login="test", total_bytes=1000, used_bytes=100)
    monkeypatch.setattr(config, "DOWNLOAD_ROOT", tmp_path)

    async def fake_iter_files(self, items):
        yield "/dir/file.txt", 10

    async def fake_get_resource(self, path):
        return {"file": "http://fake-cdn/download", "size": 10}

    async def fake_stream(self, url, dest, cancel_check=None, chunk_size=1, expected_size=None, *args, **kwargs):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"0123456789")
        return 10


    monkeypatch.setattr(YandexClient, "iter_files", fake_iter_files)
    monkeypatch.setattr(YandexClient, "get_resource", fake_get_resource)
    monkeypatch.setattr(YandexClient, "stream_to_file", fake_stream)

    resp = await async_client.post(
        "/api/tasks/",
        json={"dest": str(tmp_path), "items": [{"path": "/dir", "type": "folder"}]},
    )
    assert resp.status_code == 201
    tid = resp.json()["id"]

    # Ждём завершения задачи
    for _ in range(50):
        resp = await async_client.get(f"/api/tasks/{tid}/")
        data = resp.json()
        if data["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)

    assert data["status"] == "done"
    assert data["total_files"] == 1
    assert data["done_files"] == 1
    assert data["failed_files"] == 0
    assert (tmp_path / "dir" / "file.txt").read_bytes() == b"0123456789"


@pytest.mark.asyncio
async def test_task_dest_outside_root_rejected(async_client, monkeypatch, tmp_path):
    db.save_account(token="dummy-tok", login="test", total_bytes=1000, used_bytes=100)
    monkeypatch.setattr(config, "DOWNLOAD_ROOT", tmp_path)
    monkeypatch.setattr(config, "ENFORCE_DOWNLOAD_ROOT", True)

    resp = await async_client.post(
        "/api/tasks/",
        json={"dest": "/etc", "items": [{"path": "/a.txt", "type": "file"}]},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_task_requires_items(async_client, monkeypatch, tmp_path):
    db.save_account(token="dummy-tok", login="test", total_bytes=1000, used_bytes=100)
    monkeypatch.setattr(config, "DOWNLOAD_ROOT", tmp_path)

    resp = await async_client.post(
        "/api/tasks/",
        json={"dest": str(tmp_path), "items": []},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_task_delete(async_client, monkeypatch, tmp_path):
    db.save_account(token="dummy-tok", login="test", total_bytes=1000, used_bytes=100)
    monkeypatch.setattr(config, "DOWNLOAD_ROOT", tmp_path)

    async def fake_iter_files(self, items):
        if False:
            yield

    monkeypatch.setattr(YandexClient, "iter_files", fake_iter_files)

    resp = await async_client.post(
        "/api/tasks/",
        json={"dest": str(tmp_path), "items": [{"path": "/empty-dir", "type": "folder"}]},
    )
    assert resp.status_code == 201
    tid = resp.json()["id"]

    resp = await async_client.delete(f"/api/tasks/{tid}/")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    resp = await async_client.get(f"/api/tasks/{tid}/")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_static_files(async_client):
    resp = await async_client.get("/")
    assert resp.status_code == 200
    assert "Выгрузка — Яндекс Диск" in resp.text

    resp = await async_client.get("/styles.css")
    assert resp.status_code == 200

    resp = await async_client.get("/app.js")
    assert resp.status_code == 200
    assert "formatBytes" in resp.text


@pytest.mark.asyncio
async def test_browse_folder_endpoint(async_client, monkeypatch):
    from app import routes
    monkeypatch.setattr(routes, "_open_folder_dialog", lambda *a, **k: "C:/Selected/Folder")
    resp = await async_client.post("/api/browse-folder/")
    assert resp.status_code == 200
    assert resp.json()["path"] == "C:/Selected/Folder"


@pytest.mark.asyncio
async def test_smart_resume_skips_existing_files(async_client, monkeypatch, tmp_path):
    db.save_account(token="dummy-tok", login="test", total_bytes=1000, used_bytes=100)
    monkeypatch.setattr(config, "DOWNLOAD_ROOT", tmp_path)

    # Предварительно создаём файл с тем же размером
    existing_file = tmp_path / "resume_dir" / "already.txt"
    existing_file.parent.mkdir(parents=True, exist_ok=True)
    existing_file.write_bytes(b"12345")

    async def fake_iter_files(self, items):
        yield "/resume_dir/already.txt", 5

    stream_called = False

    async def fake_stream(*args, **kwargs):
        nonlocal stream_called
        stream_called = True
        return 5

    monkeypatch.setattr(YandexClient, "iter_files", fake_iter_files)
    monkeypatch.setattr(YandexClient, "stream_to_file", fake_stream)

    resp = await async_client.post(
        "/api/tasks/",
        json={"dest": str(tmp_path), "items": [{"path": "/resume_dir", "type": "folder"}]},
    )
    assert resp.status_code == 201
    tid = resp.json()["id"]

    for _ in range(50):
        resp = await async_client.get(f"/api/tasks/{tid}/")
        data = resp.json()
        if data["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)

    assert data["status"] == "done"
    assert data["done_files"] == 1
    # stream_to_file НЕ должен был вызываться, т.к. файл уже существовал
    assert stream_called is False


@pytest.mark.asyncio
async def test_failed_downloads_report_created(async_client, monkeypatch, tmp_path):
    db.save_account(token="dummy-tok", login="test", total_bytes=1000, used_bytes=100)
    monkeypatch.setattr(config, "DOWNLOAD_ROOT", tmp_path)

    async def fake_iter_files(self, items):
        yield "/error_dir/bad.txt", 10

    async def fake_get_resource(self, path):
        from app.yandex import YandexAPIError
        raise YandexAPIError(502, "CDN failure")

    monkeypatch.setattr(YandexClient, "iter_files", fake_iter_files)
    monkeypatch.setattr(YandexClient, "get_resource", fake_get_resource)

    resp = await async_client.post(
        "/api/tasks/",
        json={"dest": str(tmp_path), "items": [{"path": "/error_dir", "type": "folder"}]},
    )
    assert resp.status_code == 201
    tid = resp.json()["id"]

    for _ in range(50):
        resp = await async_client.get(f"/api/tasks/{tid}/")
        data = resp.json()
        if data["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)

    assert data["status"] == "error"
    # Проверяем, что файл отчёта создан в целевой папке
    report = tmp_path / "_failed_downloads.txt"
    assert report.is_file()
    content = report.read_text(encoding="utf-8")
    assert "/error_dir/bad.txt" in content
    assert "CDN failure" in content
    assert "ИНФОРМАЦИЯ О ДОКАЧИВАНИИ" in content


@pytest.mark.asyncio
async def test_failed_downloads_report_appends_across_tasks(async_client, monkeypatch, tmp_path):
    db.save_account(token="dummy-tok", login="test", total_bytes=1000, used_bytes=100)
    monkeypatch.setattr(config, "DOWNLOAD_ROOT", tmp_path)

    async def fake_iter1(self, items):
        yield "/dir/bad1.txt", 10

    async def fake_res1(self, path):
        from app.yandex import YandexAPIError
        raise YandexAPIError(500, "Error 1")

    monkeypatch.setattr(YandexClient, "iter_files", fake_iter1)
    monkeypatch.setattr(YandexClient, "get_resource", fake_res1)

    resp = await async_client.post(
        "/api/tasks/",
        json={"dest": str(tmp_path), "items": [{"path": "/dir", "type": "folder"}]},
    )
    tid1 = resp.json()["id"]
    for _ in range(50):
        r = await async_client.get(f"/api/tasks/{tid1}/")
        if r.json()["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)

    async def fake_iter2(self, items):
        yield "/dir/bad2.txt", 20

    async def fake_res2(self, path):
        from app.yandex import YandexAPIError
        raise YandexAPIError(502, "Error 2")

    monkeypatch.setattr(YandexClient, "iter_files", fake_iter2)
    monkeypatch.setattr(YandexClient, "get_resource", fake_res2)

    resp = await async_client.post(
        "/api/tasks/",
        json={"dest": str(tmp_path), "items": [{"path": "/dir", "type": "folder"}]},
    )
    tid2 = resp.json()["id"]
    for _ in range(50):
        r = await async_client.get(f"/api/tasks/{tid2}/")
        if r.json()["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)

    report = tmp_path / "_failed_downloads.txt"
    assert report.is_file()
    content = report.read_text(encoding="utf-8")
    assert tid1 in content
    assert "bad1.txt" in content
    assert tid2 in content
    assert "bad2.txt" in content



@pytest.mark.asyncio
async def test_create_task_with_custom_concurrency(async_client, monkeypatch, tmp_path):
    db.save_account(token="dummy-tok", login="test", total_bytes=1000, used_bytes=100)
    monkeypatch.setattr(config, "DOWNLOAD_ROOT", tmp_path)

    async def fake_iter_files(self, items):
        if False:
            yield

    monkeypatch.setattr(YandexClient, "iter_files", fake_iter_files)

    resp = await async_client.post(
        "/api/tasks/",
        json={"dest": str(tmp_path), "items": [{"path": "/dir", "type": "folder"}], "concurrency": 16},
    )
    assert resp.status_code == 201
    tid = resp.json()["id"]

    runner = download_manager.runners.get(tid)
    assert runner is not None
    assert runner.concurrency == 16


@pytest.mark.asyncio
async def test_create_task_with_keep_parts(async_client, monkeypatch, tmp_path):
    db.save_account(token="dummy-tok", login="test", total_bytes=1000, used_bytes=100)
    monkeypatch.setattr(config, "DOWNLOAD_ROOT", tmp_path)

    async def fake_iter_files(self, items):
        if False:
            yield

    monkeypatch.setattr(YandexClient, "iter_files", fake_iter_files)

    resp = await async_client.post(
        "/api/tasks/",
        json={"dest": str(tmp_path), "items": [{"path": "/dir", "type": "folder"}], "keep_parts": True},
    )
    assert resp.status_code == 201
    tid = resp.json()["id"]

    runner = download_manager.runners.get(tid)
    assert runner is not None
    assert runner.keep_parts is True

    task = db.get_task(tid)
    assert task["keep_parts"] is True

