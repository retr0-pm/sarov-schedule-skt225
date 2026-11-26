"""Pure reconciliation for the dedicated SKT-225 calendar."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .rules import RULES_VERSION, normalize_subject

CALENDAR_ID = "f311eb844a8fcb81b18529a77e2ce85057a80d90d467bbe3b60b4202eb586da8@group.calendar.google.com"
MANAGED_BY = "sarov-schedule-skt225"
MOSCOW = ZoneInfo("Europe/Moscow")
HORIZON = timedelta(days=14)
FIELDS = ("managed_by", "source_id", "series_id", "week_id", "rules_version", "source_pdf_sha256")
SOURCE = re.compile(r"skt225-v3-[0-9a-f]{24}")
SERIES = re.compile(r"series-[0-9a-f]{24}")
SHA = re.compile(r"[0-9a-f]{64}")
WEEK = re.compile(r"\d{4}-\d\d-\d\d/\d{4}-\d\d-\d\d")
FORMAT_SUFFIX = re.compile(r"\s*(?:\((?:online|онлайн|семинар|лекция)\)|\b(?:online|онлайн))$")


class SyncError(RuntimeError):
    """An error with a fixed, safe-to-publish message."""


def week_bounds(week: str) -> tuple[date, date]:
    if not isinstance(week, str) or not WEEK.fullmatch(week):
        raise ValueError("Invalid week")
    start, end = map(date.fromisoformat, week.split("/"))
    if not timedelta(0) <= end - start <= timedelta(days=6):
        raise ValueError("Invalid week")
    return start, end


def moment(day: str, clock: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=MOSCOW)


def interval(raw: dict) -> tuple[datetime, datetime]:
    times = tuple(datetime.fromisoformat(raw[key]["dateTime"].replace("Z", "+00:00"))
                  for key in ("start", "end"))
    if any(value.utcoffset() is None for value in times) or times[0] >= times[1]:
        raise ValueError("Invalid event interval")
    return times


def window(schedule: dict) -> tuple[datetime, datetime]:
    bounds = [week_bounds(week["week_id"]) for week in schedule["weeks"]]
    return (datetime.combine(min(pair[0] for pair in bounds), time.min, MOSCOW),
            datetime.combine(max(pair[1] for pair in bounds), time.min, MOSCOW)
            + timedelta(days=1) + HORIZON)


def markers(raw: dict) -> dict[str, str] | None:
    values: dict[str, list[str]] = defaultdict(list)
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ValueError("Unreadable description")
    for line in description.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in FIELDS:
            values[key.strip()].append(value.strip())
    private = raw.get("extendedProperties", {}).get("private", {})
    if private.get("managed_by") == MANAGED_BY and MANAGED_BY not in values["managed_by"]:
        raise ValueError("Managed event lacks description marker")
    if MANAGED_BY not in values["managed_by"]:
        return None
    if any(len(values[key]) != 1 for key in FIELDS):
        raise ValueError("Missing or duplicate marker")
    result = {key: values[key][0] for key in FIELDS}
    week_bounds(result["week_id"])
    if not (SOURCE.fullmatch(result["source_id"]) and SERIES.fullmatch(result["series_id"])
            and SHA.fullmatch(result["source_pdf_sha256"]) and result["rules_version"] == RULES_VERSION):
        raise ValueError("Invalid marker")
    if any(key in private and private[key] != result[key] for key in FIELDS):
        raise ValueError("Conflicting private marker")
    return result


def payload(event: dict) -> dict:
    # Keep the six legacy markers so partially imported calendars need no migration.
    lines = ["Группа: СКТ-225", f"Преподаватель: {event['teacher'] or 'не указан'}",
             f"Тип занятия: {event['lesson_type'] or 'не указан'}",
             f"managed_by={MANAGED_BY}", f"rules_version={RULES_VERSION}"]
    lines.extend(f"{key}={event[key]}" for key in ("source_id", "series_id", "week_id", "source_pdf_sha256"))
    return {
        "summary": event["subject"], "location": event["room"] or "",
        "description": "\n".join(lines),
        **{key: {"dateTime": moment(event["date"], event[key]).isoformat(), "timeZone": MOSCOW.key}
           for key in ("start", "end")},
    }


def subject_at_slot(title: str) -> str:
    normalized = normalize_subject(title)
    while (shorter := FORMAT_SUFFIX.sub("", normalized)) != normalized:
        normalized = shorter
    return normalized


def human_field(raw: dict, label: str) -> str:
    prefix = f"{label}:"
    values = [line[len(prefix):].strip() for line in raw["description"].splitlines() if line.startswith(prefix)]
    if len(set(values)) > 1:
        raise ValueError("Conflicting event descriptions")
    return "" if not values or values[0] == "не указан" else values[0]


@dataclass(frozen=True)
class Existing:
    raw: dict
    markers: dict[str, str]
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Operation:
    kind: str
    desired: dict | None = None
    previous: Existing | None = None

    def public(self) -> dict:
        # Never publish any calendar-derived text, internal event IDs, or descriptions.
        change = {"kind": self.kind}
        if self.desired:
            change.update(date=self.desired["date"], start=self.desired["start"], subject=self.desired["subject"])
        elif self.previous:
            start = self.previous.start.astimezone(MOSCOW)
            change.update(date=start.date().isoformat(), start=start.strftime("%H:%M"))
        return change


@dataclass
class Plan:
    expected: int
    managed: int = 0
    unchanged: int = 0
    operations: list[Operation] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def matches(old: Existing, desired: dict) -> bool:
    target = payload(desired)
    return (
        (old.start, old.end) == interval(target)
        and old.raw["summary"] == target["summary"]
        and (old.raw.get("location") or "") == target["location"]
        and all(old.markers[key] == desired[key] for key in ("source_id", "series_id", "week_id"))
        and human_field(old.raw, "Преподаватель") == (desired["teacher"] or "")
        and human_field(old.raw, "Тип занятия") == (desired["lesson_type"] or "")
    )


def parse_existing(raw: dict) -> Existing | None:
    meta = markers(raw)
    if meta is None:
        return None
    start, end = interval(raw)
    first, last = week_bounds(meta["week_id"])
    if (not first <= start.astimezone(MOSCOW).date() <= last
            or raw.get("status") != "confirmed"
            or raw.get("recurrence") or raw.get("recurringEventId") or raw.get("locked")
            or raw.get("attendees") or raw.get("conferenceData") or raw.get("hangoutLink")
            or not raw.get("id") or not raw.get("etag") or not isinstance(raw.get("summary"), str)
            or any(raw[key].get("timeZone") not in (None, MOSCOW.key) for key in ("start", "end"))):
        raise ValueError("Unsupported managed event")
    human_field(raw, "Преподаватель")
    human_field(raw, "Тип занятия")
    return Existing(raw, meta, start, end)


def build_plan(schedule: dict, calendar: list[dict], now: datetime) -> Plan:
    if now.utcoffset() is None:
        raise ValueError("A timezone-aware cutoff is required")
    future = {e["source_id"]: e for e in schedule["events"] if moment(e["date"], e["start"]) >= now}
    all_desired = {e["source_id"]: e for e in schedule["events"]}
    safe_weeks = {w["week_id"] for w in schedule["weeks"]
                  if w["status"] == "verified" and w["safe_for_deletions"] is True}
    plan = Plan(expected=len(future))
    existing: dict[str, Existing] = {}
    seen_ids: set[str] = set()
    for raw in calendar:
        try:
            old = parse_existing(raw)
        except (KeyError, TypeError, ValueError, AttributeError):
            plan.conflicts.append("malformed_managed_event")
            continue
        if old is None:
            continue
        if old.start >= now:
            plan.managed += 1
        key = old.markers["source_id"]
        if key in existing or old.raw["id"] in seen_ids:
            plan.conflicts.append("duplicate_calendar_identity")
        existing[key] = old
        seen_ids.add(old.raw["id"])

    missing = {}
    for key, desired in future.items():
        old = existing.get(key)
        if old is None:
            missing[key] = desired
        elif old.start < now:
            plan.conflicts.append("source_id_already_used_by_started_event")
        elif old.markers["series_id"] != desired["series_id"]:
            plan.conflicts.append("source_id_series_mismatch")
        elif matches(old, desired):
            plan.unchanged += 1
        else:
            plan.operations.append(Operation("update", desired, old))

    stale = {}
    for key, old in existing.items():
        if old.start < now or key in future:
            continue
        if key in all_desired or old.markers["week_id"] not in safe_weeks:
            plan.conflicts.append("managed_event_outside_verified_scope")
        else:
            stale[key] = old

    match_changes(plan, missing, stale)
    plan.operations.extend(Operation("create", desired) for desired in missing.values())
    plan.operations.extend(Operation("delete", previous=old) for old in stale.values())
    plan.operations.sort(key=lambda op: (
        {"update": 0, "create": 1, "delete": 2}[op.kind],
        op.desired["source_id"] if op.desired else op.previous.markers["source_id"],
    ))
    return plan


def match_changes(plan: Plan, missing: dict[str, dict], stale: dict[str, Existing]) -> None:
    by_series: dict[str, list[tuple[str, Existing]]] = defaultdict(list)
    by_slot: dict[tuple, list[tuple[str, Existing]]] = defaultdict(list)
    for key, old in stale.items():
        by_series[old.markers["series_id"]].append((key, old))
        by_slot[old.markers["week_id"], subject_at_slot(old.raw["summary"]), old.start, old.end].append((key, old))

    candidates = {}
    for key, desired in missing.items():
        start, end = interval(payload(desired))
        title = normalize_subject(desired["subject"])
        similar = by_series[desired["series_id"]]
        nearby = [(ident, old) for ident, old in similar if abs(start - old.start) <= HORIZON]
        if any(normalize_subject(old.raw["summary"]) != title for _, old in nearby):
            plan.conflicts.append("series_title_mismatch")
        same_slot = by_slot[desired["week_id"], subject_at_slot(desired["subject"]), start, end]
        candidates[key] = dict(nearby + same_slot)

    degrees = Counter(ident for pairs in candidates.values() for ident in pairs)
    for key, pairs in candidates.items():
        if not pairs:
            continue
        if len(pairs) != 1 or any(degrees[ident] != 1 for ident in pairs):
            plan.conflicts.append("ambiguous_move_or_replacement")
            continue
        ident, old = next(iter(pairs.items()))
        plan.operations.append(Operation("update", missing.pop(key), old))
        del stale[ident]


def guard_deletions(plan: Plan, limit: int) -> None:
    deletes = sum(op.kind == "delete" for op in plan.operations)
    if deletes > limit or deletes * 5 > plan.managed or (deletes and plan.expected == 0):
        raise SyncError("deletion_budget_exceeded")
