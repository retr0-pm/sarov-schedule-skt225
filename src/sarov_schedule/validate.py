from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .fetcher import FetchError


def validate_artifact(output_dir: Path) -> dict[str, Any]:
    manifest_path = output_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FetchError(f"Невозможно прочитать {manifest_path}: {exc}") from exc

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise FetchError("Манифест не содержит PDF")
    if manifest.get("pdf_count") != len(files):
        raise FetchError("pdf_count не совпадает с количеством записей")

    expected_names = {"manifest.json"}
    for item in files:
        filename = item.get("file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise FetchError(f"Небезопасное имя файла: {filename!r}")
        path = output_dir / filename
        expected_names.add(filename)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise FetchError(f"Нет файла {filename}: {exc}") from exc
        if not data.startswith(b"%PDF-"):
            raise FetchError(f"Неверная сигнатура {filename}")
        if len(data) != item.get("size_bytes"):
            raise FetchError(f"Неверный размер {filename}")
        if hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise FetchError(f"Неверная SHA-256 {filename}")

    actual_names = {path.name for path in output_dir.iterdir() if path.is_file()}
    extras = actual_names - expected_names
    missing = expected_names - actual_names
    if extras or missing:
        raise FetchError(f"Состав каталога не совпадает с манифестом: лишние={sorted(extras)}, нет={sorted(missing)}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверить самодостаточный артефакт расписания")
    parser.add_argument("output", type=Path, nargs="?", default=Path("output"))
    args = parser.parse_args()
    try:
        manifest = validate_artifact(args.output)
    except FetchError as exc:
        raise SystemExit(f"Артефакт некорректен: {exc}") from exc
    print(f"Проверено PDF: {manifest['pdf_count']}")


if __name__ == "__main__":
    main()
