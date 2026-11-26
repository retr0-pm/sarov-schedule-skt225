from __future__ import annotations

import argparse
import json
from pathlib import Path

from .fetcher import COURSE, DEFAULT_MAX_PDF_BYTES, FetchError, ScheduleDownloader, fetch_to_output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Загрузить расписание МГУ в Сарове")
    parser.add_argument("--course", type=int, choices=(1, 2), default=COURSE)
    parser.add_argument("--output", type=Path, default=Path("output"), help="Каталог полного результата")
    parser.add_argument(
        "--previous-manifest",
        type=Path,
        help="Предыдущий успешно проверенный manifest.json для сравнения SHA-256",
    )
    parser.add_argument("--max-pdf-bytes", type=int, default=DEFAULT_MAX_PDF_BYTES)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        manifest = fetch_to_output(
            args.output,
            previous_manifest_path=args.previous_manifest,
            downloader=ScheduleDownloader(max_pdf_bytes=args.max_pdf_bytes),
            course=args.course,
        )
    except FetchError as exc:
        raise SystemExit(f"Ошибка загрузки расписания: {exc}") from exc
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
