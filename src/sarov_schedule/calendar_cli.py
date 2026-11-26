"""Validate, reconcile and publish a calendar synchronization report."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .calendar_api import Calendar, HTTPError, event_id
from .calendar_sync import (
    CALENDAR_ID, Operation, SyncError, build_plan, guard_deletions, interval, markers, matches, moment, parse_existing,
    payload, week_bounds, window,
)
from .fetcher import SCHEDULE_URL, FetchError
from .profiles import PROFILES, SKT225, Profile
from .validate import validate_artifact


def load_schedule(directory: Path, schema_path: Path, profile: Profile = SKT225) -> dict:
    try:
        manifest = validate_artifact(directory, require_schedule=True, profile=profile)
        if manifest.get("source") != SCHEDULE_URL or manifest.get("course") != profile.course:
            raise SyncError("unexpected_schedule_source")
        schedule = json.loads((directory / "schedule.json").read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if profile != SKT225:
            schema["properties"]["group"]["const"] = profile.group
            schema["properties"]["rules_version"]["const"] = profile.rules_version
            event_schema = schema["$defs"]["event"]["properties"]
            event_schema["group"]["const"] = profile.group
            event_schema["source_id"]["pattern"] = fr"^{re.escape(profile.rules_version)}-[0-9a-f]{{24}}$"
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(schedule)
        check_coverage(schedule, manifest)
        return schedule
    except (FetchError, OSError, KeyError, TypeError, ValueError, AttributeError, ValidationError):
        raise SyncError("invalid_schedule_artifact") from None


def check_coverage(schedule: dict, manifest: dict) -> None:
    weeks = schedule["weeks"]
    by_week = {w["week_id"]: w for w in weeks}
    files = {f["file"]: f for f in manifest["files"]}
    stats = schedule["recognition"]
    if (stats.get("status") != "verified" or stats.get("safe_for_deletions") is not True
            or schedule["ambiguities"] or stats.get("ambiguous_week_count") != 0):
        raise SyncError("ambiguous_schedule")
    if (not weeks or len(weeks) != len(by_week) or len(weeks) != len(files)
            or len(files) != manifest["pdf_count"]
            or {w["source_pdf"] for w in weeks} != files.keys()
            or stats.get("processed_week_count") != len(weeks)
            or stats.get("verified_week_count") != len(weeks)
            or schedule["semester"] != manifest["semester"] or schedule["source"] != SCHEDULE_URL):
        raise SyncError("incomplete_week_coverage")
    numbers = sorted(f["week_number"] for f in files.values())
    if numbers != list(range(1, len(files) + 1)):
        raise SyncError("incomplete_week_number_sequence")
    ranges = sorted(week_bounds(key) for key in by_week)
    if any(left[1] >= right[0] for left, right in zip(ranges, ranges[1:])):
        raise SyncError("overlapping_weeks")

    included = Counter(e["week_id"] for e in schedule["events"])
    excluded = Counter(e["week_id"] for e in schedule["exclusions"])
    if not excluded.keys() <= by_week.keys():
        raise SyncError("unknown_exclusion_week")
    for week in weeks:
        key = week["week_id"]
        pdf = files[week["source_pdf"]]
        if (week["status"] != "verified" or week["safe_for_deletions"] is not True
                or week["ambiguity_count"] != 0 or week["included_count"] != included[key]
                or week["excluded_count"] != excluded[key] or pdf["sha256"] != week["source_pdf_sha256"]
                or pdf["week_number"] != week["week_number"]
                or pdf.get("download_status") != "success" or pdf.get("validation_status") != "verified"):
            raise SyncError("invalid_week_metadata")
    for event in schedule["events"]:
        week = by_week[event["week_id"]]
        first, last = week_bounds(event["week_id"])
        if (event["recognition_status"] != "verified" or event["source_pdf"] != week["source_pdf"]
                or not first <= moment(event["date"], event["start"]).date() <= last
                or any(len(value.splitlines()) > 1 for value in (event["teacher"] or "", event["lesson_type"] or ""))):
            raise SyncError("event_outside_verified_week")


def verify_event(raw: dict | None, desired: dict, ident: str, profile: Profile, calendar_id: str) -> None:
    try:
        old = parse_existing(raw or {}, profile, calendar_id)
        if old and old.raw["id"] == ident and matches(old, desired):
            return
    except (KeyError, AttributeError, TypeError, ValueError):
        pass
    raise SyncError("calendar_event_readback_mismatch")


def mutate(calendar: Calendar, op: Operation, clock, profile: Profile, calendar_id: str) -> bool:
    """Return False when a concurrent insert already produced the desired event."""
    desired = op.desired
    now = clock()
    if ((desired and moment(desired["date"], desired["start"]) < now)
            or (op.previous and op.previous.start < now)):
        raise SyncError("event_started_during_sync")
    if op.kind == "create":
        ident = event_id(desired["source_id"], profile)
        try:
            calendar.create(ident, payload(desired, profile))
        except HTTPError as exc:
            if exc.status != 409:
                raise
            verify_event(calendar.get(ident), desired, ident, profile, calendar_id)
            return False
        return True

    old = op.previous
    ident, etag = old.raw["id"], old.raw["etag"]
    current = calendar.get(ident)
    if not current or current.get("etag") != etag or current.get("id") != ident:
        raise SyncError("calendar_changed_before_write")
    if old.start < clock() or (desired and moment(desired["date"], desired["start"]) < clock()):
        raise SyncError("event_started_during_sync")
    if op.kind == "update":
        new_body = payload(desired, profile)
        private = current.get("extendedProperties", {}).get("private", {})
        if private:
            # Preserve other properties and update any inherited copies of our markers.
            values = markers(new_body, profile)
            new_body["extendedProperties"] = {
                "private": {**private, **{key: values[key] for key in private.keys() & values.keys()}}
            }
        calendar.patch(ident, etag, new_body)
    else:
        calendar.delete(ident, etag)
    return True


def reconcile(calendar: Calendar, schedule: dict, mode: str, allow_deletes: bool, report: dict,
              *, clock=lambda: datetime.now(timezone.utc),
              profile: Profile = SKT225, calendar_id: str = CALENDAR_ID) -> None:
    cutoff = clock()
    bounds = window(schedule)
    first = calendar.list(*bounds)
    plan = build_plan(schedule, first, cutoff, profile=profile, calendar_id=calendar_id)
    report.update(expected=plan.expected, managed=plan.managed, unchanged=plan.unchanged,
                  conflicts=len(plan.conflicts), errors=sorted(set(plan.conflicts)))
    if plan.conflicts:
        report["status"] = "conflict"
        return
    report["planned"] = [op.public() for op in plan.operations]
    if mode == "dry-run":
        report["status"] = "dry_run"
        return

    if allow_deletes:
        guard_deletions(plan, 3)
    blocked_deletes = [op for op in plan.operations if op.kind == "delete" and not allow_deletes]
    blocked_creates = []
    for op in plan.operations:
        if op.kind != "create" or not blocked_deletes:
            continue
        start, end = interval(payload(op.desired, profile))
        if any(start < stale.previous.end and end > stale.previous.start for stale in blocked_deletes):
            blocked_creates.append(op)
    pending = blocked_creates + blocked_deletes
    report["deletions_blocked"] = len(blocked_deletes)
    report["overlapping_creates_blocked"] = len(blocked_creates)
    operations = [op for op in plan.operations if op not in pending]
    if operations:
        # Equality ignores ordering, but catches every changed or missing managed record.
        second = calendar.list(*bounds)
        if build_plan(schedule, second, cutoff, profile=profile, calendar_id=calendar_id) != plan:
            raise SyncError("calendar_changed_before_write")
    for op in operations:
        performed = mutate(calendar, op, clock, profile, calendar_id)
        if performed:
            report["changes"].append(op.public())
            report[{"create": "created", "update": "updated", "delete": "deleted"}[op.kind]] += 1
        else:
            report["unchanged"] += 1
        if op.desired:
            ident = op.previous.raw["id"] if op.previous else event_id(op.desired["source_id"], profile)
            verify_event(calendar.get(ident), op.desired, ident, profile, calendar_id)

    # A no-op run already read the complete calendar. After writes, rebuild from a fresh listing.
    verified = build_plan(schedule, calendar.list(*bounds), cutoff, profile=profile,
                          calendar_id=calendar_id) if operations else plan
    if (verified.conflicts or verified.unchanged + len(blocked_creates) != plan.expected
            or verified.operations != pending):
        raise SyncError("calendar_final_verification_failed")
    report["verified"] = True
    report["status"] = "deletions_disabled" if pending else "success"


def run(directory: Path, schema: Path, mode: str, allow_deletes: bool, profile: Profile = SKT225) -> dict:
    report = dict(schema_version=1, started_at=datetime.now(timezone.utc).isoformat(),
                  run_id=os.environ.get("GITHUB_RUN_ID"), run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"),
                  sha=os.environ.get("GITHUB_SHA"), manifest_fingerprint=None, validation="failed",
                  expected=0, managed=0, created=0, updated=0, deleted=0, unchanged=0, conflicts=0,
                  deletions_blocked=0, overlapping_creates_blocked=0, verified=False,
                  errors=[], changes=[], planned=[], mode=mode,
                  status="error")
    try:
        if mode == "upstream-failed":
            raise SyncError("fetch_or_recognition_failed")
        schedule = load_schedule(directory, schema, profile)
        now = datetime.now(timezone.utc)
        report.update(validation="verified", manifest_fingerprint=schedule["manifest_fingerprint"],
                      expected=sum(moment(event["date"], event["start"]) >= now for event in schedule["events"]))
        if mode == "off":
            report["status"] = "disabled"
        else:
            if profile == SKT225 and mode == "apply" and os.environ.get("SKT225_CHATGPT_WRITES_DISABLED") != "true":
                raise SyncError("legacy_calendar_writer_not_disabled")
            calendar_id = CALENDAR_ID if profile == SKT225 else os.environ.get("VM126_GOOGLE_CALENDAR_ID", "")
            if profile != SKT225 and not re.fullmatch(r"[a-z0-9]+@group\.calendar\.google\.com", calendar_id):
                raise SyncError("vm126_calendar_id_missing_or_invalid")
            calendar = Calendar.connect(write=mode == "apply", profile=profile, calendar_id=calendar_id)
            reconcile(calendar, schedule, mode, allow_deletes, report, profile=profile, calendar_id=calendar_id)
    except SyncError as exc:
        report["errors"].append(str(exc))
    except Exception:
        # Credential exceptions and provider responses must never escape to public logs.
        report["errors"].append("unexpected_sync_error")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--profile", choices=PROFILES, default="skt225")
    parser.add_argument("--schema", type=Path, default=Path("schema/schedule.schema.json"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--mode", choices=("off", "dry-run", "apply", "upstream-failed"), default="dry-run")
    parser.add_argument("--allow-deletes", action="store_true")
    args = parser.parse_args()
    profile = PROFILES[args.profile]
    report = run(args.directory, args.schema, args.mode, args.allow_deletes, profile)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = (
        f"### {profile.group} · {report['status']}\n\n"
        f"Validation: {report['validation']}. Expected: {report['expected']}. "
        f"Managed: {report['managed']}. Created: {report['created']}, updated: {report['updated']}, "
        f"deleted: {report['deleted']}, unchanged: {report['unchanged']}, conflicts: {report['conflicts']}.\n"
        f"Planned: {len(report['planned'])}. Errors: {', '.join(report['errors']) or 'none'}.\n"
    )
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fp:
            fp.write(summary)
    if report["status"] in ("error", "conflict"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
