from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .fetcher import FetchError
from .profiles import PROFILES, SKT225, Profile
from .recognizer import RecognitionError, validate_schedule


def validate_artifact(
    output_dir: Path, *, require_schedule: bool = False, profile: Profile = SKT225,
) -> dict[str, Any]:
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

    schedule_path = output_dir / "schedule.json"
    ambiguities_path = output_dir / "ambiguities.json"
    schedule = None
    if schedule_path.exists():
        try:
            schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
            validate_schedule(schedule, manifest, profile=profile)
        except (OSError, json.JSONDecodeError, RecognitionError) as exc:
            raise FetchError(f"Некорректный schedule.json: {exc}") from exc
        expected_names.update({"schedule.json", "ambiguities.json"})
        try:
            ambiguity_report = json.loads(ambiguities_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FetchError(f"Некорректный ambiguities.json: {exc}") from exc
        if ambiguity_report.get("items") != schedule.get("ambiguities"):
            raise FetchError("ambiguities.json не соответствует schedule.json")
        if ambiguity_report.get("status") != schedule.get("recognition", {}).get("status"):
            raise FetchError("Статус ambiguities.json не соответствует schedule.json")
    elif require_schedule:
        raise FetchError("В артефакте нет обязательного schedule.json")

    actual_names = {path.name for path in output_dir.iterdir() if path.is_file()}
    extras = actual_names - expected_names
    missing = expected_names - actual_names
    if extras or missing:
        raise FetchError(f"Состав каталога не совпадает с манифестом: лишние={sorted(extras)}, нет={sorted(missing)}")
    if schedule is not None:
        manifest["schedule"] = schedule["recognition"]
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверить самодостаточный артефакт расписания")
    parser.add_argument("output", type=Path, nargs="?", default=Path("output"))
    parser.add_argument("--profile", choices=PROFILES, default="skt225")
    parser.add_argument(
        "--require-schedule",
        action="store_true",
        help="Требовать и проверять schedule.json и ambiguities.json",
    )
    args = parser.parse_args()
    try:
        manifest = validate_artifact(args.output, require_schedule=args.require_schedule, profile=PROFILES[args.profile])
    except FetchError as exc:
        raise SystemExit(f"Артефакт некорректен: {exc}") from exc
    print(f"Проверено PDF: {manifest['pdf_count']}")
    if "schedule" in manifest:
        summary = manifest["schedule"]
        print(
            "Распознавание: "
            f"{summary['status']}; включено={summary['included_count']}; "
            f"исключено={summary['excluded_count']}; неоднозначностей={summary['ambiguity_count']}"
        )


if __name__ == "__main__":
    main()
