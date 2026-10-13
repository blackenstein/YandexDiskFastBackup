import pytest
from app.yandex import YandexClient

c = YandexClient("test-token")


def test_norm_path():
    assert c._norm_path("disk:/a/b") == "/a/b"
    assert c._norm_path("disk:/") == "/"
    assert c._norm_path("/a/b") == "/a/b"
    assert c._norm_path(None) == ""


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
