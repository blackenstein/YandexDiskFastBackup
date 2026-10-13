"""Диспетчер выгрузки: управление параллельными воркерами, прогрессом и отменой."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .config import API_QPS, DOWNLOAD_CONCURRENCY
from .db import db
from .paths import PathError, safe_join, yandex_rel
from .ratelimit import RateLimiter
from .yandex import CancelledDownload, TokenInvalidError, YandexAPIError, YandexClient

logger = logging.getLogger(__name__)


class TaskRunner:
    def __init__(
        self,
        task_id: str,
        dest: str,
        items: List[Dict[str, Any]],
        concurrency: int = DOWNLOAD_CONCURRENCY,
    ):
        self.task_id = task_id
        self.dest = dest
        self.items = items
        self.concurrency = max(1, min(64, int(concurrency)))
        self.semaphore = asyncio.Semaphore(self.concurrency)
        self.cancel_event = asyncio.Event()
        self.total_files = 0
        self.total_bytes = 0
        self.done_files = 0
        self.done_bytes = 0
        self.failed_files = 0
        self.status = "pending"
        self.error = ""
        self.errors: List[Dict[str, str]] = []
        self._main_task: Optional[asyncio.Task] = None

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()


class DownloadManager:
    def __init__(
        self,
        concurrency: int = DOWNLOAD_CONCURRENCY,
        qps: int = API_QPS,
    ):
        self.concurrency = concurrency
        self.rate_limiter = RateLimiter(qps=qps)
        self.runners: Dict[str, TaskRunner] = {}

    def _get_client(self) -> YandexClient:
        acc = db.get_account()
        if not acc or not acc.get("token"):
            raise YandexAPIError(0, "Яндекс Диск не подключён")
        return YandexClient(acc["token"], rate_limiter=self.rate_limiter)

    def start_task(
        self,
        task_id: str,
        dest: str,
        items: List[Dict[str, Any]],
        concurrency: Optional[int] = None,
    ) -> None:
        conc = concurrency if concurrency and concurrency > 0 else self.concurrency
        runner = TaskRunner(task_id, dest, items, concurrency=conc)
        self.runners[task_id] = runner
        runner._main_task = asyncio.create_task(self._run_task(runner))

    def cancel_task(self, task_id: str) -> bool:
        runner = self.runners.get(task_id)
        if runner:
            runner.cancel()
        task = db.get_task(task_id)
        if not task:
            return False
        if task["status"] in ("done", "error", "cancelled"):
            return False
        db.update_task_status(task_id, "cancelled")
        if runner:
            runner.status = "cancelled"
        return True

    def delete_task(self, task_id: str) -> bool:
        self.cancel_task(task_id)
        self.runners.pop(task_id, None)
        return db.delete_task(task_id)

    def get_task_payload(self, task_id: str) -> Optional[Dict[str, Any]]:
        t = db.get_task(task_id)
        if not t:
            return None
        runner = self.runners.get(task_id)
        if runner and runner.status == "running":
            t["total_files"] = runner.total_files or t["total_files"]
            t["total_bytes"] = runner.total_bytes or t["total_bytes"]
            t["done_files"] = runner.done_files
            t["done_bytes"] = runner.done_bytes
            t["failed_files"] = runner.failed_files
            t["status"] = runner.status
            if runner.errors:
                t["errors"] = runner.errors
        return t

    async def _download_file(
        self,
        runner: TaskRunner,
        client: YandexClient,
        disk_path: str,
        size: int,
        dest_root: Path,
    ) -> None:
        if runner.is_cancelled():
            return

        target = safe_join(dest_root, yandex_rel(disk_path))

        # Умное докачивание: если файл уже существует и его размер совпадает,
        # пропускаем сразу без лишних сетевых запросов к API Яндекса
        if target.is_file():
            try:
                local_size = target.stat().st_size
                if size is not None and local_size == int(size):
                    logger.info("Файл уже существует и полон (пропуск): %s (%d байт)", disk_path, local_size)
                    runner.done_files += 1
                    runner.done_bytes += size
                    db.update_task_progress(runner.task_id, runner.done_files, runner.done_bytes, runner.failed_files)
                    return
            except Exception:
                pass

        async with runner.semaphore:
            if runner.is_cancelled():
                return

            max_retries = 3
            last_err = ""
            for attempt in range(max_retries + 1):
                if runner.is_cancelled():
                    return
                try:
                    res = await client.get_resource(disk_path)
                    url = client.download_url(res)
                    if not url:
                        raise YandexAPIError(500, "Не получен URL для скачивания", retryable=True)

                    await client.stream_to_file(
                        url=url,
                        dest=target,
                        cancel_check=runner.is_cancelled,
                        expected_size=size,
                    )
                    runner.done_files += 1
                    runner.done_bytes += size
                    db.update_task_progress(runner.task_id, runner.done_files, runner.done_bytes, runner.failed_files)
                    return
                except CancelledDownload:
                    return
                except YandexAPIError as e:
                    last_err = f"HTTP {e.status}: {e.message}"
                    if e.retryable and attempt < max_retries:
                        await asyncio.sleep(min(60.0, 2.0 ** attempt))
                        continue
                    break
                except httpx.TransportError as e:
                    last_err = f"Сетевая ошибка: {type(e).__name__}"
                    if attempt < max_retries:
                        await asyncio.sleep(min(60.0, 2.0 ** (attempt + 1)))
                        continue
                    break
                except Exception as e:
                    last_err = f"Ошибка: {type(e).__name__}: {e}"
                    logger.exception("Unexpected error downloading %s", disk_path)
                    break

            # Ошибка скачивания файла
            if not runner.is_cancelled():
                runner.done_files += 1
                runner.failed_files += 1
                err_entry = {"path": disk_path, "error": last_err[:300]}
                runner.errors.append(err_entry)
                if len(runner.errors) > 50:
                    runner.errors = runner.errors[-50:]
                db.record_task_error(runner.task_id, disk_path, last_err)
                db.update_task_progress(runner.task_id, runner.done_files, runner.done_bytes, runner.failed_files)

    async def _run_task(self, runner: TaskRunner) -> None:
        try:
            client = self._get_client()
        except YandexAPIError as e:
            runner.status = "error"
            runner.error = e.message
            db.update_task_status(runner.task_id, "error", e.message)
            return

        runner.status = "running"
        db.update_task_status(runner.task_id, "running")

        files: List[Tuple[str, int]] = []
        try:
            async with client:
                # 1. Сканирование дерева
                for item in runner.items:
                    async for f_path, f_sz in client.iter_files([item]):
                        if runner.is_cancelled():
                            runner.status = "cancelled"
                            db.update_task_status(runner.task_id, "cancelled")
                            return
                        files.append((f_path, f_sz))

                runner.total_files = len(files)
                runner.total_bytes = sum(sz for _, sz in files)
                db.update_task_scan(runner.task_id, runner.total_files, runner.total_bytes, "running")

                if not files:
                    runner.status = "done"
                    db.finalize_task(runner.task_id, "done", 0, 0, 0)
                    return

                # 2. Параллельное скачивание
                dest_root = Path(runner.dest)
                tasks = [
                    self._download_file(runner, client, disk_path, size, dest_root)
                    for disk_path, size in files
                ]
                await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            runner.status = "cancelled"
            db.update_task_status(runner.task_id, "cancelled")
            return
        except TokenInvalidError:
            runner.status = "error"
            runner.error = "Токен истёк во время выгрузки"
            db.update_task_status(runner.task_id, "error", runner.error)
            return
        except (YandexAPIError, PathError) as e:
            msg = e.message if isinstance(e, YandexAPIError) else str(e)
            runner.status = "error"
            runner.error = msg
            db.update_task_status(runner.task_id, "error", msg)
            return
        except Exception as e:
            runner.status = "error"
            runner.error = f"Непредвиденная ошибка: {e}"
            logger.exception("Task %s failed unexpectedly", runner.task_id)
            db.update_task_status(runner.task_id, "error", runner.error)
            return

        # 3. Финализация
        if runner.is_cancelled():
            runner.status = "cancelled"
            db.update_task_status(runner.task_id, "cancelled")
        elif runner.total_files > 0 and runner.failed_files == runner.total_files:
            runner.status = "error"
            runner.error = "Не удалось выгрузить ни один файл"
            db.finalize_task(
                runner.task_id,
                "error",
                runner.done_files,
                runner.done_bytes,
                runner.failed_files,
                runner.error,
            )
        else:
            runner.status = "done"
            db.finalize_task(
                runner.task_id,
                "done",
                runner.done_files,
                runner.done_bytes,
                runner.failed_files,
            )

        # 4. Фиксация отчёта об ошибках в целевой папке
        dest_root = Path(runner.dest)
        report_file = dest_root / "_failed_downloads.txt"
        if runner.errors and not runner.is_cancelled():
            try:
                dest_root.mkdir(parents=True, exist_ok=True)
                from datetime import datetime, timezone
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                lines = [
                    "=" * 80,
                    "ОШИБКИ ВЫГРУЗКИ ИЗ ЯНДЕКС ДИСКА",
                    f"Дата и время: {now_str}",
                    f"Папка назначения: {dest_root}",
                    f"Всего файлов с ошибками: {len(runner.errors)} из {runner.total_files}",
                    "=" * 80,
                    "",
                    "Список файлов, которые не удалось загрузить:",
                    "",
                ]
                for idx, err in enumerate(runner.errors, 1):
                    lines.append(f"{idx}. {err.get('path', '')}")
                    lines.append(f"   Ошибка: {err.get('error', '')}")
                    lines.append("")
                lines.extend([
                    "-" * 80,
                    "ИНФОРМАЦИЯ О ДОКАЧИВАНИИ:",
                    "При повторном запуске выгрузки этой же папки уже успешно скачанные файлы",
                    "будут автоматически пропущены (умное докачивание). Программа попытается",
                    "загрузить только отсутствующие/неудавшиеся файлы.",
                    "=" * 80,
                ])
                report_file.write_text("\n".join(lines), encoding="utf-8")
                logger.info("Отчёт об ошибках сохранён в %s", report_file)
            except Exception as e:
                logger.warning("Не удалось записать отчёт об ошибках: %s", e)
        elif runner.status == "done" and runner.failed_files == 0:
            try:
                if report_file.is_file():
                    report_file.unlink(missing_ok=True)
            except Exception:
                pass


download_manager = DownloadManager()
