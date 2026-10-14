import pytest
from app.yandex import YandexClient

c = YandexClient("test-token")


def test_norm_path():
    assert c._norm_path("disk:/a/b") == "/a/b"
    assert c._norm_path("disk:/") == "/"
    assert c._norm_path("/a/b") == "/a/b"
    assert c._norm_path(None) == ""
    assert c._norm_path("") == ""
    # Гарантия лидирующего слэша для предотвращения DiskPathFormatError в API Яндекса
    assert c._norm_path("disk:D") == "/D"
    assert c._norm_path("D") == "/D"
    assert c._norm_path("disk:/D") == "/D"
    assert c._norm_path("/D") == "/D"


def test_api_path():
    assert c._api_path("/") == "disk:/"
    assert c._api_path(None) == "disk:/"
    assert c._api_path("") == "disk:/"
    assert c._api_path("/a/b") == "disk:/a/b"
    assert c._api_path("disk:/a/b") == "disk:/a/b"
    # Защита от сбоя парсинга двоеточия в API Яндекса для папок вида 'D:'
    assert c._api_path("/D:") == "disk:/D:"
    assert c._api_path("disk:/D:") == "disk:/D:"
    assert c._api_path("D:") == "disk:/D:"
    assert c._api_path("/D:/file.txt") == "disk:/D:/file.txt"


def test_norm_type():
    assert c._norm_type("dir") == "folder"
    assert c._norm_type("folder") == "folder"
    assert c._norm_type("file") == "file"


def test_item_size_top_level():
    assert c._item_size({"size": 100}) == 100
    assert c._item_size({"size": None, "file": {"size": 55}}) == 55
    assert c._item_size({}) == 0


def test_list_of_new_format():
    data = {"_embedded": {"items": [{"name": "a"}], "total": 2, "limit": 1, "offset": 0}}
    items, total = c._list_of(data)
    assert total == 2
    assert len(items) == 1


def test_list_of_old_format():
    data = {"items": [{"name": "a"}, {"name": "b"}], "next_page_token": "x"}
    items, total = c._list_of(data)
    assert total == 0
    assert len(items) == 2


def test_norm_item_dir():
    item = {
        "path": "disk:/Education",
        "type": "dir",
        "name": "Education",
        "created": "2026-01-01T00:00:00+00:00",
        "modified": "2026-01-02T00:00:00+00:00",
    }
    n = c._norm_item(item)
    assert n["path"] == "/Education"
    assert n["type"] == "folder"
    assert n["size"] == 0
    assert n["modified"] == "2026-01-02T00:00:00+00:00"


def test_download_url_new_format():
    assert c.download_url({"file": "https://dl/x"}) == "https://dl/x"


def test_download_url_old_format():
    assert c.download_url({"file": {"download_url": "https://dl/y"}}) == "https://dl/y"
    assert c.download_url({"download_url": "https://dl/z"}) == "https://dl/z"


@pytest.mark.asyncio
async def test_disk_info_mapping():
    class FakeResp:
        def json(self):
            return {"total_space": 100, "used_space": 40}

    async def fake_request(*args, **kwargs):
        return FakeResp()

    c._request = fake_request
    info = await c.disk_info()
    assert info["total_bytes"] == 100
    assert info["used_bytes"] == 40


class MockStreamResponse:
    def __init__(self, status_code: int, chunks: list, exc_on_iter=None):
        self.status_code = status_code
        self.chunks = chunks
        self.exc_on_iter = exc_on_iter

    async def aiter_bytes(self, chunk_size=None):
        if self.exc_on_iter:
            raise self.exc_on_iter
        for chunk in self.chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.mark.asyncio
async def test_stream_to_file_fresh_download(tmp_path):
    import httpx
    client = YandexClient("tok")
    dest = tmp_path / "file.txt"
    recorded_calls = []

    def mock_stream(method, url, headers=None, **kwargs):
        recorded_calls.append((method, url, headers))
        return MockStreamResponse(200, [b"hello ", b"world"])

    client._http.stream = mock_stream
    written = await client.stream_to_file("http://cdn/file", dest, expected_size=11)
    assert written == 11
    assert dest.read_bytes() == b"hello world"
    assert not dest.with_name(dest.name + ".part").exists()
    assert recorded_calls[0][2] is None  # Без Range


@pytest.mark.asyncio
async def test_stream_to_file_resume_206(tmp_path):
    client = YandexClient("tok")
    dest = tmp_path / "file.txt"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"hello ")  # 6 байт уже скачано

    recorded_headers = []

    def mock_stream(method, url, headers=None, **kwargs):
        recorded_headers.append(headers)
        if headers and "Range" in headers:
            return MockStreamResponse(206, [b"world"])
        return MockStreamResponse(200, [b"hello world"])

    client._http.stream = mock_stream
    written = await client.stream_to_file("http://cdn/file", dest, expected_size=11)
    assert written == 11
    assert dest.read_bytes() == b"hello world"
    assert recorded_headers[0] == {"Range": "bytes=6-"}


@pytest.mark.asyncio
async def test_stream_to_file_server_ignored_range_200(tmp_path):
    client = YandexClient("tok")
    dest = tmp_path / "file.txt"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"stale data")

    def mock_stream(method, url, headers=None, **kwargs):
        # Сервер вернул 200 вместо 206 (проигнорировал Range)
        return MockStreamResponse(200, [b"clean full file"])

    client._http.stream = mock_stream
    written = await client.stream_to_file("http://cdn/file", dest, expected_size=15)
    assert written == 15
    assert dest.read_bytes() == b"clean full file"


@pytest.mark.asyncio
async def test_stream_to_file_range_fallback_on_drop(tmp_path):
    import httpx
    client = YandexClient("tok")
    dest = tmp_path / "file.txt"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"half")

    call_count = 0

    def mock_stream(method, url, headers=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Первый вызов (Range) падает с ошибкой протокола
            return MockStreamResponse(
                206,
                [],
                exc_on_iter=httpx.RemoteProtocolError("peer closed connection"),
            )
        # Второй вызов (фоллбэк без Range) успешен
        return MockStreamResponse(200, [b"full content"])

    client._http.stream = mock_stream
    written = await client.stream_to_file("http://cdn/file", dest, expected_size=12)
    assert written == 12
    assert dest.read_bytes() == b"full content"
    assert call_count == 2


@pytest.mark.asyncio
async def test_stream_to_file_cancelled_preserves_part(tmp_path):
    from app.yandex import CancelledDownload
    client = YandexClient("tok")
    dest = tmp_path / "file.txt"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"first ")

    # Имитируем отмену после получения чанка
    def mock_stream(method, url, headers=None, **kwargs):
        return MockStreamResponse(206, [b"chunk1", b"chunk2"])

    client._http.stream = mock_stream

    call_count = 0

    def cancel_checker():
        nonlocal call_count
        call_count += 1
        return call_count >= 2

    with pytest.raises(CancelledDownload):
        await client.stream_to_file(
            "http://cdn/file",
            dest,
            cancel_check=cancel_checker,
            chunk_size=6,
        )

    # .part файл сохранился для последующего возобновления!
    assert part.is_file()
    assert part.stat().st_size >= 12


@pytest.mark.asyncio
async def test_stream_to_file_keep_parts_preserves_on_drop(tmp_path):
    import httpx
    from app.yandex import YandexAPIError
    client = YandexClient("tok")
    dest = tmp_path / "file.txt"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"existing_partial_data")

    def mock_stream(method, url, headers=None, **kwargs):
        return MockStreamResponse(
            206,
            [],
            exc_on_iter=httpx.RemoteProtocolError("peer closed connection"),
        )

    client._http.stream = mock_stream
    with pytest.raises(YandexAPIError) as exc_info:
        await client.stream_to_file("http://cdn/file", dest, expected_size=100, keep_parts=True)

    assert exc_info.value.status == 503
    assert part.is_file()
    assert part.read_bytes() == b"existing_partial_data"


@pytest.mark.asyncio
async def test_stream_to_file_keep_parts_preserves_on_size_mismatch(tmp_path):
    from app.yandex import YandexAPIError
    client = YandexClient("tok")
    dest = tmp_path / "file.txt"
    part = dest.with_name(dest.name + ".part")

    def mock_stream(method, url, headers=None, **kwargs):
        return MockStreamResponse(200, [b"incomplete"])

    client._http.stream = mock_stream
    with pytest.raises(YandexAPIError) as exc_info:
        await client.stream_to_file("http://cdn/file", dest, expected_size=50, keep_parts=True)

    assert exc_info.value.status == 502
    assert "файл .part сохранён" in exc_info.value.message
    assert part.is_file()
    assert part.read_bytes() == b"incomplete"

