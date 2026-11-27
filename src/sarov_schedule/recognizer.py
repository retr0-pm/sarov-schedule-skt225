from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .profiles import SKT225, Profile
from .rules import apply_skt225_rules, apply_vm126_rules, normalize_subject

GROUP = SKT225.group
TIMEZONE = "Europe/Moscow"
SCHEDULE_SCHEMA_VERSION = 1
GROUP_RE = re.compile(r"^[А-ЯЁA-Z]{1,12}-\d{3}$")
DATE_RE = re.compile(r"^(\d{2})\.(\d{2})$")
TIME_RE = re.compile(r"^(\d{1,2})[.:](\d{2})$")
TEACHER_RE = re.compile(r"^(?:[А-ЯЁ]\.){1,2}\s*[А-ЯЁ][А-Яа-яЁё-]+")
ROOM_RE = re.compile(r"\bаудитори[яи]\b", re.IGNORECASE)
LESSON_TYPES = ("лекция", "семинар", "практика", "лабораторная", "зачет", "зачёт", "экзамен")


class RecognitionError(RuntimeError):
    """Ошибка, при которой schedule.json нельзя считать структурно корректным."""


@dataclass(frozen=True)
class WordBox:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class VerticalSegment:
    x: float
    top: float
    bottom: float

    def crosses(self, y: float, tolerance: float = 1.0) -> bool:
        return self.top - tolerance <= y <= self.bottom + tolerance


@dataclass(frozen=True)
class HorizontalSegment:
    y: float
    x0: float
    x1: float

    def crosses(self, x: float, tolerance: float = 1.0) -> bool:
        return self.x0 - tolerance <= x <= self.x1 + tolerance


@dataclass(frozen=True)
class PageGeometry:
    page_number: int
    width: float
    height: float
    words: tuple[WordBox, ...]
    verticals: tuple[VerticalSegment, ...]
    horizontals: tuple[HorizontalSegment, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "width": self.width,
            "height": self.height,
            "words": [asdict(item) for item in self.words],
            "verticals": [asdict(item) for item in self.verticals],
            "horizontals": [asdict(item) for item in self.horizontals],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PageGeometry":
        return cls(
            page_number=int(data["page_number"]),
            width=float(data["width"]),
            height=float(data["height"]),
            words=tuple(WordBox(**item) for item in data["words"]),
            verticals=tuple(VerticalSegment(**item) for item in data["verticals"]),
            horizontals=tuple(HorizontalSegment(**item) for item in data["horizontals"]),
        )


@dataclass(frozen=True)
class LessonRow:
    lesson_number: int
    top: float
    bottom: float
    start: str
    end: str


@dataclass(frozen=True)
class TextLine:
    text: str
    top: float
    bottom: float


@dataclass
class PageResult:
    page_number: int
    page_date: date | None
    raw_events: list[dict[str, Any]]
    ambiguities: list[dict[str, Any]]


def _round(value: float) -> float:
    return round(float(value), 3)


def geometry_from_pdf_page(page: Any, page_number: int) -> PageGeometry:
    words = tuple(
        WordBox(
            text=str(item["text"]),
            x0=_round(item["x0"]),
            x1=_round(item["x1"]),
            top=_round(item["top"]),
            bottom=_round(item["bottom"]),
        )
        for item in page.extract_words(x_tolerance=1.5, y_tolerance=2.0, keep_blank_chars=False, use_text_flow=False)
    )
    verticals: list[VerticalSegment] = []
    horizontals: list[HorizontalSegment] = []

    for rect in page.rects:
        width = float(rect["width"])
        height = float(rect["height"])
        if width <= 1.25 and height >= 4:
            verticals.append(
                VerticalSegment(_round((rect["x0"] + rect["x1"]) / 2), _round(rect["top"]), _round(rect["bottom"]))
            )
        elif height <= 1.25 and width >= 4:
            horizontals.append(
                HorizontalSegment(_round((rect["top"] + rect["bottom"]) / 2), _round(rect["x0"]), _round(rect["x1"]))
            )

    for line in page.lines:
        x0, x1 = float(line["x0"]), float(line["x1"])
        top, bottom = float(line["top"]), float(line["bottom"])
        if abs(x1 - x0) <= 1.25 and abs(bottom - top) >= 4:
            verticals.append(VerticalSegment(_round((x0 + x1) / 2), _round(min(top, bottom)), _round(max(top, bottom))))
        elif abs(bottom - top) <= 1.25 and abs(x1 - x0) >= 4:
            horizontals.append(HorizontalSegment(_round((top + bottom) / 2), _round(min(x0, x1)), _round(max(x0, x1))))

    return PageGeometry(
        page_number=page_number,
        width=_round(page.width),
        height=_round(page.height),
        words=words,
        verticals=tuple(verticals),
        horizontals=tuple(horizontals),
    )


def extract_pdf_geometry(path: Path) -> list[PageGeometry]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - диагностируется установкой пакета
        raise RecognitionError("Для распознавания требуется pdfplumber") from exc

    try:
        with pdfplumber.open(path) as pdf:
            return [geometry_from_pdf_page(page, index) for index, page in enumerate(pdf.pages, start=1)]
    except Exception as exc:
        raise RecognitionError(f"Не удалось прочитать геометрию PDF {path.name}: {exc}") from exc


def _academic_year(semester: str) -> tuple[int, int]:
    years = re.findall(r"\d{4}", semester)
    if len(years) < 2:
        raise RecognitionError(f"Не удалось определить учебный год из {semester!r}")
    return int(years[0]), int(years[1])


def _date_from_page(geometry: PageGeometry, header: WordBox, years: tuple[int, int]) -> date | None:
    candidates: list[tuple[float, WordBox, re.Match[str]]] = []
    for word in geometry.words:
        match = DATE_RE.fullmatch(word.text.strip())
        if not match or word.cx >= header.cx:
            continue
        distance = abs(word.cy - header.cy)
        if distance <= max(45.0, geometry.height * 0.08):
            candidates.append((distance, word, match))
    if not candidates:
        return None
    _, _, match = min(candidates, key=lambda item: (item[0], -item[1].top))
    day, month = int(match.group(1)), int(match.group(2))
    year = years[0] if month >= 8 else years[1]
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _nearest_vertical_bounds(geometry: PageGeometry, x: float, y: float) -> tuple[float, float] | None:
    positions = sorted({segment.x for segment in geometry.verticals if segment.crosses(y)})
    left = [value for value in positions if value < x - 0.5]
    right = [value for value in positions if value > x + 0.5]
    if not left or not right:
        return None
    return max(left), min(right)


def _row_bounds(geometry: PageGeometry, word: WordBox) -> tuple[float, float] | None:
    ys = sorted({segment.y for segment in geometry.horizontals if segment.crosses(word.cx)})
    above = [value for value in ys if value < word.cy]
    below = [value for value in ys if value > word.cy]
    if not above or not below:
        return None
    return max(above), min(below)


def _normalize_clock(value: str) -> str | None:
    match = TIME_RE.fullmatch(value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    try:
        return time(hour, minute).strftime("%H:%M")
    except ValueError:
        return None


def _lesson_rows(geometry: PageGeometry, header_bottom: float, first_group_left: float) -> list[LessonRow]:
    rows: list[LessonRow] = []
    for word in geometry.words:
        if word.top <= header_bottom or word.cx >= first_group_left or not re.fullmatch(r"[1-6]", word.text.strip()):
            continue
        bounds = _row_bounds(geometry, word)
        if bounds is None:
            continue
        top, bottom = bounds
        times = []
        for candidate in geometry.words:
            if top <= candidate.cy <= bottom and word.x1 < candidate.cx < first_group_left:
                clock = _normalize_clock(candidate.text)
                if clock:
                    times.append((candidate.x0, clock))
        times.sort()
        unique_times: list[str] = []
        for _, clock in times:
            if clock not in unique_times:
                unique_times.append(clock)
        if len(unique_times) < 2:
            continue
        rows.append(LessonRow(int(word.text), top, bottom, unique_times[0], unique_times[1]))
    unique: dict[int, LessonRow] = {}
    for row in sorted(rows, key=lambda item: (item.lesson_number, item.top)):
        unique.setdefault(row.lesson_number, row)
    return list(unique.values())


def _has_horizontal_boundary(geometry: PageGeometry, y: float, x: float) -> bool:
    return any(abs(segment.y - y) <= 1.2 and segment.crosses(x, tolerance=1.5) for segment in geometry.horizontals)


def _cell_lines(geometry: PageGeometry, x0: float, x1: float, top: float, bottom: float) -> list[TextLine]:
    words = [
        word
        for word in geometry.words
        if x0 + 0.5 < word.cx < x1 - 0.5 and top + 0.5 < word.cy < bottom - 0.5
    ]
    words.sort(key=lambda item: (item.top, item.x0))
    groups: list[list[WordBox]] = []
    for word in words:
        if not groups or abs(word.top - sum(item.top for item in groups[-1]) / len(groups[-1])) > 2.5:
            groups.append([word])
        else:
            groups[-1].append(word)
    lines: list[TextLine] = []
    for group in groups:
        group.sort(key=lambda item: item.x0)
        text = " ".join(item.text for item in group)
        text = re.sub(r"(?<=\w)-\s+(?=\w)", "-", text)
        text = " ".join(text.split())
        if text:
            lines.append(TextLine(text, min(item.top for item in group), max(item.bottom for item in group)))
    return lines


def _is_room_line(text: str) -> bool:
    normalized = normalize_subject(text)
    return bool(ROOM_RE.search(text)) or normalized == "внииэф"


def _is_teacher_line(text: str) -> bool:
    return bool(TEACHER_RE.search(text))


def _split_entries(lines: Sequence[TextLine]) -> list[list[TextLine]]:
    entries: list[list[TextLine]] = []
    current: list[TextLine] = []
    current_has_room = False
    for line in lines:
        starts_seminar = normalize_subject(line.text).startswith("спецсеминар")
        if current and (current_has_room or (
            starts_seminar and any(_normalize_clock(item.text) is None for item in current)
        )):
            entries.append(current)
            current = []
            current_has_room = False
        current.append(line)
        if _is_room_line(line.text):
            current_has_room = True
    if current:
        entries.append(current)
    return entries


def _parse_entry(lines: Sequence[TextLine]) -> tuple[dict[str, Any] | None, str | None]:
    texts = [line.text for line in lines if line.text.strip()]
    if not texts:
        return None, None

    explicit_start: str | None = None
    while texts and _normalize_clock(texts[0]):
        explicit_start = _normalize_clock(texts.pop(0))

    teacher_index = next((index for index, text in enumerate(texts) if _is_teacher_line(text)), None)
    room_index = next((index for index, text in enumerate(texts) if _is_room_line(text)), None)
    subject_end_candidates = [index for index in (teacher_index, room_index) if index is not None]
    subject_end = min(subject_end_candidates) if subject_end_candidates else len(texts)
    subject_lines = texts[:subject_end]
    if not subject_lines:
        return None, "cell_has_no_subject"

    teacher = None
    if teacher_index is not None:
        end = room_index if room_index is not None and room_index > teacher_index else len(texts)
        teacher = " ".join(texts[teacher_index:end]).strip() or None
    room = None
    if room_index is not None:
        room = " ".join(texts[room_index:]).strip() or None

    raw_subject = " ".join(subject_lines)
    raw_subject = re.sub(r"(?<=\w)-\s+(?=\w)", "-", raw_subject)
    raw_subject = " ".join(raw_subject.split()).strip()
    lesson_type = None
    if normalize_subject(raw_subject).startswith("спецсеминар"):
        lesson_type = "спецсеминар"
    else:
        normalized = normalize_subject(raw_subject)
        for candidate in LESSON_TYPES:
            if re.search(rf"\b{re.escape(candidate)}\b", normalized):
                lesson_type = candidate.replace("ё", "е")
                break

    return {
        "subject": raw_subject,
        "teacher": teacher,
        "room": room,
        "lesson_type": lesson_type,
        "explicit_start": explicit_start,
        "raw_text": " | ".join(texts),
    }, None


def recognize_page_geometry(
    geometry: PageGeometry, *, semester: str, source_pdf: str, group: str = GROUP,
) -> PageResult:
    ambiguities: list[dict[str, Any]] = []
    headers = [word for word in geometry.words if word.text.strip().upper() == group]
    if len(headers) != 1:
        ambiguities.append(
            {
                "code": "target_group_header_count",
                "source_pdf": source_pdf,
                "page": geometry.page_number,
                "detail": f"Ожидался один заголовок {group}, найдено {len(headers)}",
            }
        )
        return PageResult(geometry.page_number, None, [], ambiguities)
    header = headers[0]
    years = _academic_year(semester)
    page_date = _date_from_page(geometry, header, years)
    if page_date is None:
        ambiguities.append(
            {
                "code": "page_date_not_found",
                "source_pdf": source_pdf,
                "page": geometry.page_number,
                "detail": "Не удалось однозначно определить дату страницы",
            }
        )

    header_bounds = _nearest_vertical_bounds(geometry, header.cx, header.cy)
    group_headers = [word for word in geometry.words if GROUP_RE.fullmatch(word.text.strip().upper()) and abs(word.cy - header.cy) <= 3]
    if header_bounds is None or not group_headers:
        ambiguities.append(
            {
                "code": "group_columns_not_found",
                "source_pdf": source_pdf,
                "page": geometry.page_number,
                "detail": "Не удалось восстановить границы колонок групп",
            }
        )
        return PageResult(geometry.page_number, page_date, [], ambiguities)

    first_group_left = min(
        bounds[0]
        for word in group_headers
        if (bounds := _nearest_vertical_bounds(geometry, word.cx, word.cy)) is not None
    )
    rows = _lesson_rows(geometry, header.bottom, first_group_left)
    if len(rows) != 6:
        ambiguities.append(
            {
                "code": "lesson_rows_incomplete",
                "source_pdf": source_pdf,
                "page": geometry.page_number,
                "detail": f"Ожидалось 6 строк пар, найдено {len(rows)}",
            }
        )
    rows.sort(key=lambda item: item.lesson_number)

    spans: list[tuple[list[LessonRow], float, float, float, float]] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        midpoint = (row.top + row.bottom) / 2
        bounds = _nearest_vertical_bounds(geometry, header.cx, midpoint)
        if bounds is None:
            ambiguities.append(
                {
                    "code": "target_cell_bounds_missing",
                    "source_pdf": source_pdf,
                    "page": geometry.page_number,
                    "lesson_number": row.lesson_number,
                    "detail": f"Нет границ ячейки {group}",
                }
            )
            index += 1
            continue
        x0, x1 = bounds
        span_rows = [row]
        bottom = row.bottom
        while index + len(span_rows) < len(rows):
            next_row = rows[index + len(span_rows)]
            if next_row.lesson_number != span_rows[-1].lesson_number + 1:
                break
            if _has_horizontal_boundary(geometry, bottom, header.cx):
                break
            next_bounds = _nearest_vertical_bounds(geometry, header.cx, (next_row.top + next_row.bottom) / 2)
            if next_bounds is None or any(abs(a - b) > 2 for a, b in zip(bounds, next_bounds)):
                break
            span_rows.append(next_row)
            bottom = next_row.bottom
        spans.append((span_rows, x0, x1, row.top, bottom))
        index += len(span_rows)

    raw_events: list[dict[str, Any]] = []
    for span_rows, x0, x1, top, bottom in spans:
        lines = _cell_lines(geometry, x0, x1, top, bottom)
        if not lines:
            continue
        entries = _split_entries(lines)
        for entry_index, entry_lines in enumerate(entries, start=1):
            parsed, error = _parse_entry(entry_lines)
            if error or parsed is None:
                ambiguities.append(
                    {
                        "code": error or "unparsed_cell",
                        "source_pdf": source_pdf,
                        "page": geometry.page_number,
                        "lesson_number": [row.lesson_number for row in span_rows],
                        "detail": " | ".join(line.text for line in entry_lines),
                    }
                )
                continue
            if page_date is None:
                continue
            lesson_numbers = [row.lesson_number for row in span_rows]
            start = parsed.pop("explicit_start") or span_rows[0].start
            raw_events.append(
                {
                    "group": group,
                    "date": page_date.isoformat(),
                    "lesson_number": lesson_numbers[0] if len(lesson_numbers) == 1 else lesson_numbers,
                    "start": start,
                    "end": span_rows[-1].end,
                    "timezone": TIMEZONE,
                    **parsed,
                    "source_pdf": source_pdf,
                    "page": geometry.page_number,
                    "source_locator": (
                        f"{source_pdf}:page-{geometry.page_number}:lessons-"
                        f"{'-'.join(str(item) for item in lesson_numbers)}:entry-{entry_index}"
                    ),
                    "recognition_status": "verified",
                }
            )
    return PageResult(geometry.page_number, page_date, raw_events, ambiguities)


def _manifest_fingerprint(manifest: dict[str, Any]) -> str:
    payload = [
        {"file": item.get("file"), "sha256": item.get("sha256"), "week_number": item.get("week_number")}
        for item in manifest.get("files", [])
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _series_id(event: dict[str, Any]) -> str:
    payload = "|".join(
        [
            event["group"],
            normalize_subject(event["subject"]),
            normalize_subject(event.get("teacher") or ""),
            normalize_subject(event.get("lesson_type") or ""),
        ]
    )
    return "series-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _source_id(
    *, academic_year: str, semester: str, week_number: int | None, series_id: str, occurrence: int,
    profile: Profile = SKT225,
) -> str:
    payload = "|".join(
        [academic_year, normalize_subject(semester), profile.group, str(week_number), series_id, str(occurrence)]
    )
    return profile.rules_version + "-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _week_id(dates: Iterable[date]) -> str:
    values = sorted(set(dates))
    if not values:
        raise RecognitionError("Нельзя построить week_id без дат")
    return f"{values[0].isoformat()}/{values[-1].isoformat()}"


def _load_previous_schedule(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecognitionError(f"Не удалось прочитать предыдущий schedule.json: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("weeks"), list):
        raise RecognitionError("Предыдущий schedule.json имеет неподдерживаемую структуру")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return data, digest


def build_schedule(
    output_dir: Path,
    manifest: dict[str, Any],
    *,
    generated_at: datetime | None = None,
    previous_schedule_path: Path | None = None,
    geometry_loader: Any = extract_pdf_geometry,
    profile: Profile = SKT225,
) -> dict[str, Any]:
    semester = manifest.get("semester")
    if not isinstance(semester, str):
        raise RecognitionError("В manifest.json отсутствует semester")
    start_year, end_year = _academic_year(semester)
    academic_year = f"{start_year}/{end_year}"
    previous_schedule, previous_digest = _load_previous_schedule(previous_schedule_path)
    if previous_schedule is not None and previous_schedule.get("group") != profile.group:
        raise RecognitionError("Предыдущее расписание относится к другой группе")
    previous_verified_weeks = {
        item.get("week_id") for item in (previous_schedule or {}).get("weeks", []) if item.get("status") == "verified"
    }

    all_events: list[dict[str, Any]] = []
    all_ambiguities: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    week_records: list[dict[str, Any]] = []

    for item in manifest.get("files", []):
        filename = item.get("file")
        sha256 = item.get("sha256")
        if not isinstance(filename, str) or not isinstance(sha256, str):
            raise RecognitionError("В manifest.json есть неполная запись PDF")
        path = output_dir / filename
        page_results = [
            recognize_page_geometry(geometry, semester=semester, source_pdf=filename, group=profile.group)
            for geometry in geometry_loader(path)
        ]
        dates = [result.page_date for result in page_results if result.page_date is not None]
        if not dates:
            all_ambiguities.extend(
                ambiguity for result in page_results for ambiguity in result.ambiguities
            )
            raise RecognitionError(f"Ни на одной странице {filename} не определена дата")
        flat_ambiguities = [ambiguity for result in page_results for ambiguity in result.ambiguities]
        week_id = _week_id(dates)
        included_for_week: list[dict[str, Any]] = []
        excluded_for_week: list[dict[str, Any]] = []
        for result in page_results:
            for event in result.raw_events:
                event["source_pdf_sha256"] = sha256
                event["week_id"] = week_id
                subject, lesson_type = event["subject"], event.get("lesson_type")
                if profile == SKT225:
                    decision = apply_skt225_rules(subject, lesson_type)
                else:
                    decision = apply_vm126_rules(subject, lesson_type, teacher=event.get("teacher"))
                if decision.include:
                    included_for_week.append(event)
                else:
                    excluded_for_week.append(
                        {
                            "week_id": week_id,
                            "date": event["date"],
                            "lesson_number": event["lesson_number"],
                            "subject": event["subject"],
                            "reason": decision.reason,
                            "source_pdf": filename,
                            "source_locator": event["source_locator"],
                        }
                    )
        status = "ambiguous" if flat_ambiguities else "verified"
        for event in included_for_week:
            event["recognition_status"] = status if status == "ambiguous" else "verified"
        week_records.append(
            {
                "week_id": week_id,
                "week_number": item.get("week_number"),
                "source_pdf": filename,
                "source_pdf_sha256": sha256,
                "status": status,
                "safe_for_deletions": status == "verified",
                "included_count": len(included_for_week),
                "excluded_count": len(excluded_for_week),
                "ambiguity_count": len(flat_ambiguities),
                "previous_verified_data_available": week_id in previous_verified_weeks,
            }
        )
        all_events.extend(included_for_week)
        excluded.extend(excluded_for_week)
        all_ambiguities.extend(flat_ambiguities)

    all_events.sort(
        key=lambda event: (
            event["week_id"],
            event["date"],
            event["lesson_number"] if isinstance(event["lesson_number"], int) else event["lesson_number"][0],
            normalize_subject(event["subject"]),
            event["source_locator"],
        )
    )
    occurrences: defaultdict[tuple[str, str], int] = defaultdict(int)
    week_by_id = {item["week_id"]: item for item in week_records}
    for event in all_events:
        series_id = _series_id(event)
        key = (event["week_id"], series_id)
        occurrences[key] += 1
        event["series_id"] = series_id
        event["source_id"] = _source_id(
            academic_year=academic_year,
            semester=semester,
            week_number=week_by_id[event["week_id"]]["week_number"],
            series_id=series_id,
            occurrence=occurrences[key],
            profile=profile,
        )

    week_records.sort(key=lambda item: (item["week_number"] is None, item["week_number"] or 10_000))
    overall_status = "ambiguous" if all_ambiguities or any(item["status"] != "verified" for item in week_records) else "verified"
    timestamp = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    schedule = {
        "schema_version": SCHEDULE_SCHEMA_VERSION,
        "rules_version": profile.rules_version,
        "generated_at": timestamp,
        "source": manifest.get("source"),
        "manifest_fingerprint": _manifest_fingerprint(manifest),
        "academic_year": academic_year,
        "semester": semester,
        "group": profile.group,
        "timezone": TIMEZONE,
        "recognition": {
            "status": overall_status,
            "safe_for_deletions": overall_status == "verified",
            "processed_week_count": len(week_records),
            "verified_week_count": sum(item["status"] == "verified" for item in week_records),
            "ambiguous_week_count": sum(item["status"] == "ambiguous" for item in week_records),
            "included_count": len(all_events),
            "excluded_count": len(excluded),
            "ambiguity_count": len(all_ambiguities),
        },
        "weeks": week_records,
        "events": all_events,
        "exclusions": excluded,
        "ambiguities": all_ambiguities,
        "previous_schedule": (
            {
                "sha256": previous_digest,
                "generated_at": previous_schedule.get("generated_at"),
                "used_as_current": False,
            }
            if previous_schedule is not None
            else None
        ),
    }
    validate_schedule(schedule, manifest, profile=profile)
    return schedule


def validate_schedule(schedule: dict[str, Any], manifest: dict[str, Any], *, profile: Profile = SKT225) -> None:
    required_top = {
        "schema_version",
        "rules_version",
        "generated_at",
        "source",
        "manifest_fingerprint",
        "academic_year",
        "semester",
        "group",
        "timezone",
        "recognition",
        "weeks",
        "events",
        "exclusions",
        "ambiguities",
    }
    missing = sorted(required_top - schedule.keys())
    if missing:
        raise RecognitionError(f"schedule.json не содержит поля: {missing}")
    if schedule["schema_version"] != SCHEDULE_SCHEMA_VERSION or schedule["rules_version"] != profile.rules_version:
        raise RecognitionError("Неподдерживаемая версия schedule.json или правил")
    if schedule["group"] != profile.group or schedule["timezone"] != TIMEZONE:
        raise RecognitionError("Некорректная группа или часовой пояс")
    if manifest.get("course", profile.course) != profile.course:
        raise RecognitionError("Манифест относится к другому курсу")
    if schedule["manifest_fingerprint"] != _manifest_fingerprint(manifest):
        raise RecognitionError("schedule.json относится к другому manifest.json")
    if not isinstance(schedule["events"], list) or not isinstance(schedule["weeks"], list):
        raise RecognitionError("events и weeks должны быть массивами")

    manifest_files = {item["file"]: item for item in manifest.get("files", [])}
    week_ids = {item.get("week_id") for item in schedule["weeks"]}
    source_ids: set[str] = set()
    for event in schedule["events"]:
        required_event = {
            "source_id",
            "group",
            "date",
            "lesson_number",
            "start",
            "end",
            "timezone",
            "subject",
            "teacher",
            "room",
            "lesson_type",
            "source_pdf",
            "source_pdf_sha256",
            "week_id",
            "recognition_status",
        }
        event_missing = required_event - event.keys()
        if event_missing:
            raise RecognitionError(f"Событие не содержит поля: {sorted(event_missing)}")
        if event["source_id"] in source_ids:
            raise RecognitionError(f"Повторяющийся source_id: {event['source_id']}")
        source_ids.add(event["source_id"])
        try:
            date.fromisoformat(event["date"])
            start = time.fromisoformat(event["start"])
            end = time.fromisoformat(event["end"])
        except (TypeError, ValueError) as exc:
            raise RecognitionError(f"Некорректная дата или время события {event['source_id']}") from exc
        if start >= end:
            raise RecognitionError(f"Начало не раньше конца у {event['source_id']}")
        lesson_number = event["lesson_number"]
        valid_lesson = isinstance(lesson_number, int) and 1 <= lesson_number <= 6
        valid_span = (
            isinstance(lesson_number, list)
            and lesson_number
            and all(isinstance(item, int) and 1 <= item <= 6 for item in lesson_number)
            and lesson_number == list(range(lesson_number[0], lesson_number[-1] + 1))
        )
        if not (valid_lesson or valid_span):
            raise RecognitionError(f"Некорректный lesson_number у {event['source_id']}")
        source = manifest_files.get(event["source_pdf"])
        if source is None or source.get("sha256") != event["source_pdf_sha256"]:
            raise RecognitionError(f"Событие ссылается на неизвестный PDF: {event['source_id']}")
        if event["week_id"] not in week_ids:
            raise RecognitionError(f"Событие ссылается на неизвестную неделю: {event['source_id']}")
        if event["recognition_status"] not in {"verified", "ambiguous"}:
            raise RecognitionError(f"Некорректный recognition_status: {event['source_id']}")

    recognition = schedule["recognition"]
    if recognition.get("included_count") != len(schedule["events"]):
        raise RecognitionError("included_count не совпадает с количеством событий")
    if recognition.get("excluded_count") != len(schedule["exclusions"]):
        raise RecognitionError("excluded_count не совпадает со списком исключений")
    if recognition.get("ambiguity_count") != len(schedule["ambiguities"]):
        raise RecognitionError("ambiguity_count не совпадает со списком неоднозначностей")
    has_ambiguities = bool(schedule["ambiguities"]) or any(item.get("status") != "verified" for item in schedule["weeks"])
    if has_ambiguities and (recognition.get("status") == "verified" or recognition.get("safe_for_deletions") is True):
        raise RecognitionError("Неоднозначный результат ошибочно помечен как полностью проверенный")


def write_schedule(
    output_dir: Path,
    *,
    previous_schedule_path: Path | None = None,
    profile: Profile = SKT225,
) -> dict[str, Any]:
    try:
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecognitionError(f"Не удалось прочитать manifest.json: {exc}") from exc
    schedule = build_schedule(output_dir, manifest, previous_schedule_path=previous_schedule_path, profile=profile)
    schedule_path = output_dir / "schedule.json"
    ambiguities_path = output_dir / "ambiguities.json"
    schedule_tmp = schedule_path.with_suffix(".json.tmp")
    ambiguities_tmp = ambiguities_path.with_suffix(".json.tmp")
    schedule_tmp.write_text(json.dumps(schedule, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ambiguities_tmp.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules_version": profile.rules_version,
                "status": schedule["recognition"]["status"],
                "safe_for_deletions": schedule["recognition"]["safe_for_deletions"],
                "items": schedule["ambiguities"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    schedule_tmp.replace(schedule_path)
    ambiguities_tmp.replace(ambiguities_path)
    return schedule
