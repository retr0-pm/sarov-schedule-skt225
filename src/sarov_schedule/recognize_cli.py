from __future__ import annotations

import argparse
import json
from pathlib import Path

from .profiles import PROFILES
from .recognizer import RecognitionError, write_schedule


def main() -> None:
    parser = argparse.ArgumentParser(description="Распознать расписание выбранной группы")
    parser.add_argument("--profile", choices=PROFILES, default="skt225")
    parser.add_argument("output", type=Path, nargs="?", default=Path("output"))
    parser.add_argument(
        "--previous-schedule",
        type=Path,
        help="Предыдущий проверенный schedule.json только как явно помеченная справочная версия",
    )
    args = parser.parse_args()
    try:
        schedule = write_schedule(args.output, previous_schedule_path=args.previous_schedule, profile=PROFILES[args.profile])
    except RecognitionError as exc:
        raise SystemExit(f"Ошибка распознавания расписания: {exc}") from exc
    print(json.dumps(schedule["recognition"], ensure_ascii=False, indent=2))
    if schedule["recognition"]["status"] != "verified":
        print(
            "::warning::Расписание опубликовано с неоднозначностями; "
            "safe_for_deletions=false, полный успех распознавания не подтверждён"
        )


if __name__ == "__main__":
    main()
