"""Диспетчер выгрузки: управление параллельными воркерами, прогрессом и отменой."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .config import API_QPS, DOWNLOAD_CONCURRENCY
from .db import db
from .paths import PathError, safe_join, yandex_rel
from .ratelimit import RateLimiter
from .yandex import CancelledDownload, TokenInvalidError, YandexAPIError, YandexClient

logger = logging.getLogger(__name__)


def _format_bytes(num: int) -> str:
    n = float(num)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024.0:
            return f"{n:3.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} ПБ"


class TaskRunner:

    def __init__(
        self,
        task_id: str,
        dest: str,
        items: List[Dict[str, Any]],
        concurrency: int = DOWNLOAD_CONCURRENCY,
        keep_parts: bool = False,
    ):
        self.task_id = task_id
        self.dest = dest
        self.items = items
        self.concurrency = max(1, min(64, int(concurrency)))
        self.keep_parts = bool(keep_parts)
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
        self.speed: float = 0.0
        self._last_speed_bytes: int = 0
        self._last_speed_time: float = time.monotonic()
        self._report_header_written: bool = False

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()

    def get_speed(self) -> float:
        now = time.monotonic()
        dt = now - self._last_speed_time
        if dt >= 0.8:
            delta = self.done_bytes - self._last_speed_bytes
            self.speed = max(0.0, delta / dt)
            self._last_speed_bytes = self.done_bytes
            self._last_speed_time = now
        return self.speed



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
        keep_parts: Optional[bool] = False,
    ) -> None:
        conc = concurrency if concurrency and concurrency > 0 else self.concurrency
        runner = TaskRunner(task_id, dest, items, concurrency=conc, keep_parts=bool(keep_parts))
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
            t["speed"] = runner.get_speed()
            t["keep_parts"] = runner.keep_parts
            if runner.errors:
                t["errors"] = runner.errors
        else:
            t["speed"] = 0.0
        return t

    def _record_failure_to_file(self, runner: TaskRunner, disk_path: str, error: str) -> None:
        """Мгновенно фиксирует ошибку скачивания в _failed_downloads.txt прямо в процессе работы."""
        try:
            dest_root = Path(runner.dest)
            dest_root.mkdir(parents=True, exist_ok=True)
            report_file = dest_root / "_failed_downloads.txt"
            from datetime import datetime, timezone
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            if not getattr(runner, "_report_header_written", False):
                header = [
                    "",
                    "=" * 80,
                    f"ЗАДАЧА: {runner.task_id}",
                    f"Старт: {now_str}",
                    f"Папка назначения: {dest_root}",
                    "ИНФОРМАЦИЯ О ДОКАЧИВАНИИ: При повторном запуске успешно скачанные файлы",
                    "пропускаются автоматически, будут докачиваться только сбойные.",
                    "-" * 80,
                    "Список файлов, которые не удалось загрузить (запись в реальном времени):",
                    "",
                ]
                with open(report_file, "a", encoding="utf-8") as f:
                    f.write("\n".join(header) + "\n")
                runner._report_header_written = True

            entry = f"[{now_str}] Файл: {disk_path}\n   Ошибка: {error}\n\n"
            with open(report_file, "a", encoding="utf-8") as f:
                f.write(entry)
                f.flush()
            logger.info("Сбой скачивания %s мгновенно зафиксирован в %s", disk_path, report_file)
        except Exception as e:
            logger.warning("Не удалось записать сбой в отчёт: %s", e)

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

            file_credited = 0

            def update_file_bytes(current_file_bytes: int) -> None:
                nonlocal file_credited
                delta = current_file_bytes - file_credited
                if delta != 0:
                    file_credited = current_file_bytes
                    runner.done_bytes += delta

            logger.info(
                "[Таска %s] Начало загрузки: %s (%s)",
                runner.task_id[:8],
                disk_path,
                _format_bytes(size),
            )

            max_retries = 3
            last_err = ""
            for attempt in range(max_retries + 1):
                if runner.is_cancelled():
                    if file_credited > 0:
                        runner.done_bytes -= file_credited
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
                        on_progress=update_file_bytes,
                        keep_parts=runner.keep_parts,
                    )
                    if size is not None and size > 0:
                        update_file_bytes(size)
                    runner.done_files += 1
                    logger.info(
                        "[Таска %s] Завершено: %s (%s)",
                        runner.task_id[:8],
                        disk_path,
                        _format_bytes(size),
                    )
                    db.update_task_progress(runner.task_id, runner.done_files, runner.done_bytes, runner.failed_files)
                    return
                except CancelledDownload:
                    if file_credited > 0:
                        runner.done_bytes -= file_credited
                    return
                except YandexAPIError as e:
                    if e.status == 429:
                        last_err = "HTTP 429: Превышен лимит запросов Яндекса. Рекомендуется снизить количество воркеров (потоков) до 4–6."
                    else:
                        last_err = f"HTTP {e.status}: {e.message}"
                    if e.retryable and attempt < max_retries:
                        delay = min(60.0, 2.0 ** attempt)
                        logger.warning(
                            "[Таска %s] Сбой API при скачивании %s: %s (попытка %d/%d, повтор через %.1fс)...",
                            runner.task_id[:8],
                            disk_path,
                            last_err,
                            attempt + 1,
                            max_retries,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    break
                except httpx.TransportError as e:
                    last_err = f"Сетевая ошибка: {type(e).__name__}"
                    if attempt < max_retries:
                        delay = min(60.0, 2.0 ** (attempt + 1))
                        logger.warning(
                            "[Таска %s] Сетевой сбой при скачивании %s: %s (попытка %d/%d, повтор через %.1fс)...",
                            runner.task_id[:8],
                            disk_path,
                            last_err,
                            attempt + 1,
                            max_retries,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    break
                except Exception as e:
                    last_err = f"Ошибка: {type(e).__name__}: {e}"
                    logger.exception("Unexpected error downloading %s", disk_path)
                    break

            # Ошибка скачивания файла после всех попыток
            if not runner.is_cancelled():
                if file_credited > 0:
                    runner.done_bytes -= file_credited
                    file_credited = 0
                runner.done_files += 1
                runner.failed_files += 1
                logger.error(
                    "[Таска %s] Ошибка скачивания %s: %s (после %d попыток)",
                    runner.task_id[:8],
                    disk_path,
                    last_err,
                    max_retries + 1,
                )
                err_entry = {"path": disk_path, "error": last_err[:300]}
                runner.errors.append(err_entry)
                if len(runner.errors) > 50:
                    runner.errors = runner.errors[-50:]
                db.record_task_error(runner.task_id, disk_path, last_err)
                db.update_task_progress(runner.task_id, runner.done_files, runner.done_bytes, runner.failed_files)
                # Мгновенная фиксация ошибки в файле прямо в процессе
                self._record_failure_to_file(runner, disk_path, last_err)

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

                async def _progress_heartbeat():
                    try:
                        while not runner.is_cancelled() and runner.status == "running":
                            await asyncio.sleep(25.0)
                            pct = (runner.done_files / runner.total_files * 100) if runner.total_files else 0
                            spd = runner.get_speed()
                            logger.info(
                                "[Таска %s] В процессе: %d/%d файлов (%.1f%%), %s / %s, скорость: %s/с, ошибок: %d",
                                runner.task_id[:8],
                                runner.done_files,
                                runner.total_files,
                                pct,
                                _format_bytes(runner.done_bytes),
                                _format_bytes(runner.total_bytes),
                                _format_bytes(int(spd)),
                                runner.failed_files,
                            )
                    except asyncio.CancelledError:
                        pass

                hb_task = asyncio.create_task(_progress_heartbeat())
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                finally:
                    hb_task.cancel()

        except asyncio.CancelledError:
            runner.status = "cancelled"
            db.update_task_status(runner.task_id, "cancelled")
        except TokenInvalidError:
            runner.status = "error"
            runner.error = "Токен истёк во время выгрузки"
            db.update_task_status(runner.task_id, "error", runner.error)
        except (YandexAPIError, PathError) as e:
            if isinstance(e, YandexAPIError) and e.status == 429:
                msg = "Превышен лимит запросов Яндекса (HTTP 429). Рекомендуется снизить количество воркеров (потоков) в настройках выгрузки до 4–6."
            else:
                msg = e.message if isinstance(e, YandexAPIError) else str(e)
            runner.status = "error"
            runner.error = msg
            db.update_task_status(runner.task_id, "error", msg)
        except Exception as e:
            runner.status = "error"
            runner.error = f"Непредвиденная ошибка: {e}"
            logger.exception("Task %s failed unexpectedly", runner.task_id)
            db.update_task_status(runner.task_id, "error", runner.error)

        # 3. Финализация статуса
        if runner.is_cancelled():
            runner.status = "cancelled"
            db.update_task_status(runner.task_id, "cancelled")
        elif runner.status != "error":
            if runner.total_files > 0 and runner.failed_files == runner.total_files:
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

        # 4. Финализация отчёта об ошибках в целевой папке
        dest_root = Path(runner.dest)
        report_file = dest_root / "_failed_downloads.txt"
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        if getattr(runner, "_report_header_written", False):
            try:
                status_str = "ОТМЕНЕНА ПОЛЬЗОВАТЕЛЕМ" if runner.is_cancelled() else (
                    "ЗАВЕРШЕНА С ОШИБКАМИ" if runner.failed_files > 0 else "ЗАВЕРШЕНА"
                )
                footer = [
                    "-" * 80,
                    f"Итог задачи: {status_str}",
                    f"Время завершения: {now_str}",
                    f"Всего ошибок в задаче: {runner.failed_files} из {runner.total_files}",
                    "=" * 80,
                    "",
                ]
                with open(report_file, "a", encoding="utf-8") as f:
                    f.write("\n".join(footer) + "\n")
            except Exception as e:
                logger.warning("Не удалось финализировать отчёт: %s", e)
        elif runner.status == "done" and runner.failed_files == 0:
            if report_file.is_file():
                try:
                    lines = [
                        "",
                        "=" * 80,
                        f"ЗАДАЧА: {runner.task_id}",
                        f"Дата и время: {now_str}",
                        "Статус: УСПЕШНО ЗАВЕРШЕНА",
                        f"Все файлы ({runner.total_files}) успешно скачаны без ошибок.",
                        "=" * 80,
                        "",
                    ]
                    with open(report_file, "a", encoding="utf-8") as f:
                        f.write("\n".join(lines) + "\n")
                except Exception:
                    pass


download_manager = DownloadManager()
