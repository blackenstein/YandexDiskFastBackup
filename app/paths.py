"""Безопасная работа с путями: валидация папки назначения и маппинг пути Яндекса на локальный."""

from pathlib import Path


class PathError(ValueError):
    pass


def resolve_dest(requested, root, enforce_root=False):
    """Разрешает папку назначения, указанную пользователем.

    Принимает абсолютный путь (должен быть внутри root, если enforce_root=True)
    либо относительный (относительно root). Возвращает нормализованный Path.
    """
    root = Path(root).expanduser().resolve()
    req = (requested or "").strip()
    if not req:
        return root

    p = Path(req)
    if p.is_absolute() or req.startswith(("/", "\\")):
        p = p.resolve()
    else:
        p = (root / req).resolve()

    if p == root or root in p.parents:
        return p

    if enforce_root:
        raise PathError(f"Папка назначения должна быть внутри {root}")

    return p


def yandex_rel(disk_path):
    """Возвращает путь файла/папки Яндекса относительно корня диска.

    '/a/b/c.txt' → 'a/b/c.txt'.
    'disk:/a/b/c.txt' → 'a/b/c.txt'.
    На Windows пути вроде 'D:/file.txt' (например, бэкап диска D в облаке)
    трактуются Path как переключение диска на D:. Чтобы избежать смены диска
    и ошибок файловой системы, двоеточия (недопустимые в именах файлов Windows)
    заменяются на '_'.
    """
    rel = (disk_path or "").strip()
    if rel.startswith("disk:"):
        rel = rel[len("disk:") :]
    rel = rel.replace("\\", "/").strip("/")
    if not rel:
        raise PathError("Путь на диске пуст")
    rel = rel.replace(":", "_")
    return rel


def safe_join(root, rel):
    """Безопасный root / rel с гарантией, что результат не выходит за пределы root."""
    root = Path(root).expanduser().resolve()
    # Нормализуем rel, чтобы избежать проблем с относительными путями
    clean_rel = str(rel).replace("\\", "/").lstrip("/")
    p = (root / clean_rel).resolve()
    if p == root or root not in p.parents:
        raise PathError(f"Некорректный путь: {rel}")
    return p


