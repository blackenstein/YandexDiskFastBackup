"""Асинхронный клиент API Яндекс Диска (https://yandex.ru/dev/disk-api)."""

import asyncio
import os
import socket
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import httpx

from .config import FORCE_IPV4

BASE_URL = "https://cloud-api.yandex.net/v1"
CHUNK_SIZE = 1024 * 1024

_orig_getaddrinfo = socket.getaddrinfo
_ipv4_resolver_installed = False


def install_ipv4_resolver() -> None:
    """Принудительно резолвим хосты только в IPv4 (идемпотентно).

    На хостах без IPv6-маршрута httpx может падать при получении AAAA-записи.
    Фильтруем результат getaddrinfo до AF_INET.
    """
    global _ipv4_resolver_installed
    if _ipv4_resolver_installed:
        return

    def getaddrinfo_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        try:
            return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
        except socket.gaierror:
            return _orig_getaddrinfo(host, port, family, type, proto, flags)

    socket.getaddrinfo = getaddrinfo_ipv4_only
    _ipv4_resolver_installed = True


class YandexAPIError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        retryable: bool = False,
        retry_after: Optional[float] = None,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


class TokenInvalidError(YandexAPIError):
    pass


class CancelledDownload(Exception):
    pass


class YandexClient:
    def __init__(
        self,
        token: str,
        rate_limiter: Optional[Any] = None,
        timeout: Optional[httpx.Timeout] = None,
        force_ipv4: Optional[bool] = None,
    ):
        self.token = token
        self.rate_limiter = rate_limiter
        if force_ipv4 is None:
            force_ipv4 = FORCE_IPV4
        if force_ipv4:
            install_ipv4_resolver()

        if timeout is None:
            timeout = httpx.Timeout(30.0, connect=30.0, write=30.0, pool=30.0)

        self._http = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # --- базовые запросы ----------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"OAuth {self.token}"}

    @staticmethod
    def _json_message(resp: httpx.Response) -> str:
        try:
            data = resp.json()
            msg = data.get("message") or data.get("description")
            if msg:
                return str(msg)[:300]
        except Exception:
            pass
        return (resp.text or resp.reason_phrase)[:300]

    @staticmethod
    def _retry_after(resp: httpx.Response, default: float = 2.0) -> float:
        try:
            return min(60.0, max(1.0, float(resp.headers.get("Retry-After", default))))
        except (TypeError, ValueError):
            return default

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        max_retries: int = 5,
    ) -> httpx.Response:
        url = BASE_URL + path
        attempt = 0
        while True:
            if self.rate_limiter:
                if asyncio.iscoroutinefunction(getattr(self.rate_limiter, "acquire", None)):
                    await self.rate_limiter.acquire()
                else:
                    self.rate_limiter.acquire()

            try:
                resp = await self._http.request(method, url, params=params, headers=self._headers())
            except httpx.TransportError as e:
                if attempt >= max_retries:
                    raise YandexAPIError(0, f"Сетевая ошибка: {e}", retryable=True) from e
                await asyncio.sleep(min(30.0, 2.0 ** attempt))
                attempt += 1
                continue

            if resp.status_code == 401:
                raise TokenInvalidError(401, "Токен недействителен или истёк")
            if resp.status_code == 403:
                raise YandexAPIError(403, f"Доступ запрещён: {self._json_message(resp)}")
            if resp.status_code == 404:
                raise YandexAPIError(404, "Не найдено на диске")
            if resp.status_code == 429:
                if attempt >= max_retries:
                    raise YandexAPIError(429, "Превышен лимит запросов (429)", retryable=True)
                await asyncio.sleep(self._retry_after(resp))
                attempt += 1
                continue
            if 500 <= resp.status_code < 600 and attempt < max_retries:
                await asyncio.sleep(min(30.0, 1.5 ** attempt))
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise YandexAPIError(
                    resp.status_code,
                    self._json_message(resp),
                    retryable=500 <= resp.status_code < 600,
                )
            return resp

    # --- нормализация ответов (актуальный и старый форматы API) ----------------

    @staticmethod
    def _norm_path(p: Optional[str]) -> str:
        """'disk:/a/b' → '/a/b'."""
        if not p:
            return ""
        if p.startswith("disk:"):
            return p[len("disk:") :]
        return p

    @staticmethod
    def _norm_type(t: str) -> str:
        return "folder" if t in ("dir", "folder") else t

    @staticmethod
    def _item_size(item: Dict[str, Any]) -> int:
        if item.get("size") is not None:
            return int(item["size"] or 0)
        f = item.get("file")
        if isinstance(f, dict):
            return int(f.get("size", 0) or 0)
        return 0

    @staticmethod
    def _list_of(data: Any) -> Tuple[List[Dict[str, Any]], int]:
        """(items, total) из ответа-списка. Актуальный формат: '_embedded.items'; старый: 'items'."""
        if isinstance(data, dict):
            emb = data.get("_embedded")
            if isinstance(emb, dict) and "items" in emb:
                return emb.get("items", []), int(emb.get("total", 0) or 0)
            if "items" in data:
                return data.get("items", []), int(data.get("total", 0) or 0)
        return [], 0

    def _norm_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "path": self._norm_path(item.get("path") or ""),
            "type": self._norm_type(item.get("type") or "file"),
            "name": item.get("name") or "",
            "size": self._item_size(item),
            "modified": item.get("modified") or item.get("created") or "",
        }

    # --- операции над диском --------------------------------------------------

    async def disk_info(self) -> Dict[str, int]:
        resp = await self._request("GET", "/disk")
        data = resp.json()
        return {
            "total_bytes": int(data.get("total_space", data.get("total_bytes", 0)) or 0),
            "used_bytes": int(data.get("used_space", data.get("used_bytes", 0)) or 0),
        }

    async def user_login(self) -> str:
        try:
            resp = await self._request("GET", "/users/me")
            data = resp.json()
            return data.get("login") or data.get("email") or ""
        except YandexAPIError:
            return ""

    async def list_folder(self, path: str = "/") -> AsyncIterator[Dict[str, Any]]:
        """Итератор по элементам папки с offset-пагинацией (1000 на страницу)."""
        params = {"path": self._norm_path(path or "/"), "limit": 1000, "offset": 0}
        while True:
            resp = await self._request("GET", "/disk/resources", params=params)
            data = resp.json()
            items, total = self._list_of(data)
            for item in items:
                yield self._norm_item(item)
            params["offset"] = int(params["offset"]) + len(items)
            if not items or (total and params["offset"] >= total):
                break

    async def iter_files(self, items: List[Dict[str, Any]]) -> AsyncIterator[Tuple[str, int]]:
        """Рекурсивно раскрывает выбранные элементы и выдаёт (путь_файла, размер)."""
        for item in items:
            if item.get("type") in ("folder", "dir"):
                async for sub in self.list_folder(item["path"]):
                    if sub["type"] == "folder":
                        async for f_path, f_sz in self.iter_files([sub]):
                            yield f_path, f_sz
                    else:
                        yield sub["path"], sub["size"]
            else:
                if "size" in item and item["size"] is not None:
                    size = self._item_size(item)
                else:
                    try:
                        res = await self.get_resource(item["path"])
                        size = self._item_size(res)
                    except YandexAPIError:
                        size = 0
                yield item["path"], size

    async def get_resource(self, path: str) -> Dict[str, Any]:
        """Объект ресурса. Актуальный формат возвращает его без обёртки, старый — списком."""
        resp = await self._request("GET", "/disk/resources", params={"path": self._norm_path(path)})
        data = resp.json()
        if isinstance(data, dict) and ("type" in data or "file" in data):
            return data
        items, _ = self._list_of(data)
        if not items:
            raise YandexAPIError(404, f"Не найдено: {path}")
        return items[0]

    def download_url(self, resource: Dict[str, Any]) -> Optional[str]:
        """URL скачивания: в актуальном формате — поле 'file' (строка), в старом — 'download_url'."""
        f = resource.get("file")
        if isinstance(f, str):
            return f
        if isinstance(f, dict):
            return f.get("download_url") or f.get("url")
        return resource.get("download_url")

    async def stream_to_file(
        self,
        url: str,
        dest: Path,
        cancel_check: Optional[Callable[[], bool]] = None,
        chunk_size: int = CHUNK_SIZE,
        expected_size: Optional[int] = None,
    ) -> int:
        """Стримит файл с download_url в dest (через .part-файл). Возвращает число байт."""
        dest = Path(dest)
        tmp = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0

        async with self._http.stream("GET", url) as resp:
            if resp.status_code == 429:
                raise YandexAPIError(
                    429,
                    "Превышен лимит скачиваний (429)",
                    retryable=True,
                    retry_after=self._retry_after(resp, default=5.0),
                )
            if resp.status_code >= 400:
                raise YandexAPIError(resp.status_code, f"Ошибка загрузки файла: HTTP {resp.status_code}")

            with open(tmp, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size):
                    if cancel_check and cancel_check():
                        f.close()
                        tmp.unlink(missing_ok=True)
                        raise CancelledDownload()
                    f.write(chunk)
                    written += len(chunk)

        if expected_size is not None and written != int(expected_size):
            tmp.unlink(missing_ok=True)
            retryable = written > 0
            raise YandexAPIError(
                502,
                f"CDN отправил {written:,} из {int(expected_size):,} байт (повреждённая копия файла на стороне Яндекса)",
                retryable=retryable,
            )

        # Атомарная замена
        os.replace(tmp, dest)
        return written
