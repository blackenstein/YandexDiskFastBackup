from pathlib import Path
import pytest
from app.paths import PathError, resolve_dest, safe_join, yandex_rel

ROOT = Path("/data").resolve()


def test_dest_inside_root():
    assert resolve_dest(str(ROOT / "backup"), ROOT) == ROOT / "backup"


def test_dest_root_itself():
    assert resolve_dest(str(ROOT), ROOT) == ROOT
    assert resolve_dest("", ROOT) == ROOT
    assert resolve_dest(str(ROOT) + "/", ROOT) == ROOT


def test_dest_outside_root():
    with pytest.raises(PathError):
        resolve_dest("/etc", ROOT, enforce_root=True)


def test_dest_traversal():
    with pytest.raises(PathError):
        resolve_dest(str(ROOT) + "/../etc", ROOT, enforce_root=True)


def test_dest_relative_to_root():
    assert resolve_dest("sub/dir", ROOT) == ROOT / "sub" / "dir"


def test_safe_join_basic():
    assert safe_join(ROOT, "a/b/file.txt") == ROOT / "a" / "b" / "file.txt"


def test_safe_join_traversal_blocked():
    with pytest.raises(PathError):
        safe_join(ROOT, "../../etc/passwd")


def test_yandex_rel():
    assert yandex_rel("/a/b/c.txt") == "a/b/c.txt"
    assert yandex_rel("disk:/a/b/c.txt") == "a/b/c.txt"
    assert yandex_rel("/single.txt") == "single.txt"
    assert yandex_rel("/a/b/") == "a/b"
    # Защита от переключения диска на Windows при облачных путях с двоеточием (например, D:/...)
    assert yandex_rel("/D:/folder/file.txt") == "D_/folder/file.txt"
    assert yandex_rel("disk:/D:/file.txt") == "D_/file.txt"


def test_yandex_rel_empty():
    with pytest.raises(PathError):
        yandex_rel("/")


def test_safe_join_with_windows_drive_rel():
    rel = yandex_rel("/D:/folder/file.txt")
    joined = safe_join(ROOT, rel)
    assert ROOT in joined.parents
    assert joined.name == "file.txt"


