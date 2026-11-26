from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from google.auth.exceptions import RefreshError

from sarov_schedule.calendar_api import Calendar, HTTPError, event_id
from sarov_schedule.calendar_cli import load_schedule, reconcile, run
from sarov_schedule.calendar_sync import MOSCOW, SyncError, build_plan, payload
from sarov_schedule.recognizer import build_schedule, write_schedule
from test_recognizer import six_week_manifest_and_loader

NOW = datetime(2026, 11, 22, tzinfo=timezone.utc)
W1 = "2026-11-23/2026-11-28"
W2 = "2026-11-30/2026-12-05"


def lesson(number=1, *, day="2026-11-23", week=W1, series=1, **fields):
    event = dict(source_id=f"skt225-v3-{number:024x}", series_id=f"series-{series:024x}",
                 subject="Основы теории графов", teacher="А. Автор", room="Б101", lesson_type=None,
                 date=day, start="09:00", end="10:35", week_id=week,
                 source_pdf_sha256="a" * 64)
    return {**event, **fields}


def schedule(*events):
    return {"weeks": [dict(week_id=week, status="verified", safe_for_deletions=True) for week in (W1, W2)],
            "events": list(events)}


def saved(event, ident=None):
    return {**payload(event), "id": ident or event_id(event["source_id"]), "etag": '"a"',
            "status": "confirmed"}


def report():
    return dict(status="error", created=0, updated=0, deleted=0, changes=[])


class FakeCalendar:
    def __init__(self, events=()):
        self.events = {event["id"]: deepcopy(event) for event in events}
        self.writes = []
        self.fail_at = None
        self.race = False
        self.drop = False

    def list(self, *_):
        return deepcopy(list(self.events.values()))

    def get(self, ident):
        return deepcopy(self.events.get(ident))

    def write(self, kind, ident):
        if len(self.writes) == self.fail_at:
            raise HTTPError(503)
        self.writes.append((kind, ident))

    def create(self, ident, data):
        self.write("create", ident)
        if not self.drop:
            self.events[ident] = {**data, "id": ident, "etag": '"new"', "status": "confirmed"}
        if self.race:
            raise HTTPError(409)

    def patch(self, ident, etag, data):
        self.write("patch", ident)
        if self.events[ident]["etag"] != etag:
            raise HTTPError(412)
        self.events[ident].update(data, etag='"updated"')

    def delete(self, ident, etag):
        self.write("delete", ident)
        if self.events[ident]["etag"] != etag:
            raise HTTPError(412)
        del self.events[ident]


def sync(cal, target, *, deletes=False, mode="apply", now=NOW):
    summary = report()
    reconcile(cal, target, mode, deletes, summary, clock=lambda: now)
    return summary


class ReconciliationTests(unittest.TestCase):
    def test_import_recovery_with_real_schedule_and_legacy_markers(self):
        manifest, loader = six_week_manifest_and_loader()
        target = build_schedule(Path("."), manifest, geometry_loader=loader)
        self.assertEqual(len(target["events"]), 69)
        cal = FakeCalendar(saved(event, f"legacy-{i}") for i, event in enumerate(target["events"][:12]))
        preview = sync(cal, target, mode="dry-run")
        self.assertEqual((preview["unchanged"], len(preview["planned"]), cal.writes), (12, 57, []))
        first = sync(cal, target)
        self.assertEqual((first["created"], first["unchanged"], first["verified"]), (57, 12, True))
        second = sync(cal, target)
        self.assertEqual((second["created"], second["unchanged"], len(cal.writes)), (0, 69, 57))

    def test_initial_import_then_new_lesson(self):
        cal = FakeCalendar()
        self.assertEqual(sync(cal, schedule(lesson()))["created"], 1)
        next_event = lesson(2, day="2026-11-24")
        self.assertEqual(sync(cal, schedule(lesson(), next_event))["created"], 1)

    def test_changes_update_in_place(self):
        for updated in (lesson(room="Б202"), lesson(start="10:45", end="12:20"),
                        lesson(2, series=2, teacher="Б. Автор")):
            with self.subTest(updated=updated):
                cal = FakeCalendar([saved(lesson(), "legacy")])
                self.assertEqual(sync(cal, schedule(updated))["updated"], 1)
                self.assertEqual(list(cal.events), ["legacy"])
                self.assertEqual(sync(cal, schedule(updated))["unchanged"], 1)

    def test_metadata_only_change_is_noop(self):
        cal = FakeCalendar([saved(lesson(), "legacy")])
        cal.events["legacy"]["description"] += "\nmanifest_fingerprint=" + "e" * 64
        result = sync(cal, schedule(lesson(source_pdf_sha256="b" * 64)))
        self.assertEqual((cal.writes, result["unchanged"]), ([], 1))

    def test_presentation_variant_updates_but_unrelated_replacement_is_held(self):
        cal = FakeCalendar([saved(lesson(), "legacy")])
        variant = lesson(2, series=2, subject="Основы теории графов (семинар) online", lesson_type="семинар")
        self.assertEqual(sync(cal, schedule(variant))["updated"], 1)
        replacement = lesson(3, series=3, subject="Теория чисел")
        result = sync(cal, schedule(replacement))
        self.assertEqual((result["overlapping_creates_blocked"], result["deletions_blocked"]), (1, 1))
        self.assertEqual((result["created"], result["deleted"]), (0, 0))

    def test_move_between_weeks_and_ambiguous_move(self):
        cal = FakeCalendar([saved(lesson(), "legacy")])
        new = lesson(2, week=W2, day="2026-11-30")
        conflict = sync(cal, schedule(new, lesson(3, week=W2, day="2026-12-01")))
        self.assertEqual((conflict["status"], cal.writes), ("conflict", []))
        self.assertEqual(sync(cal, schedule(new))["updated"], 1)
        self.assertEqual(list(cal.events), ["legacy"])

    def test_cancellation_threshold_and_user_event(self):
        events = [lesson(i, series=i) for i in range(1, 6)]
        user_event = {**saved(lesson(), "user"), "description": "User data"}
        cal = FakeCalendar([*(saved(event) for event in events), user_event])
        target = schedule(*events[1:])
        self.assertEqual(sync(cal, target)["status"], "deletions_disabled")
        self.assertEqual(cal.writes, [])
        self.assertEqual(sync(cal, target, deletes=True)["deleted"], 1)
        self.assertEqual(cal.events["user"], user_event)
        with self.assertRaisesRegex(SyncError, "deletion_budget_exceeded"):
            sync(cal, schedule(events[-1]), deletes=True)
        self.assertEqual(len(cal.writes), 1)

    def test_past_events_and_malformed_or_duplicate_markers(self):
        old = saved(lesson(), "past")
        cal = FakeCalendar([old])
        self.assertEqual(sync(cal, schedule(), deletes=True, now=datetime(2026, 11, 25, tzinfo=MOSCOW))["deleted"], 0)
        broken = {**old, "description": old["description"].replace("source_id=", "bad=")}
        copies = [old, {**old, "id": "duplicate"}]
        for records in ([broken], copies):
            cal = FakeCalendar(records)
            self.assertEqual(sync(cal, schedule(lesson(2)))["status"], "conflict")
            self.assertEqual(cal.writes, [])

    def test_partial_write_resumes_from_calendar(self):
        target = schedule(lesson(), lesson(2, series=2, day="2026-11-24"))
        cal = FakeCalendar()
        cal.fail_at = 1
        with self.assertRaises(HTTPError):
            sync(cal, target)
        self.assertEqual(len(cal.events), 1)
        cal.fail_at = None
        result = sync(cal, target)
        self.assertEqual((result["created"], result["unchanged"]), (1, 1))

    def test_concurrent_409_and_readback_failure(self):
        cal = FakeCalendar()
        cal.race = True
        self.assertEqual(sync(cal, schedule(lesson()))["unchanged"], 1)
        self.assertEqual(len(cal.events), 1)
        cal = FakeCalendar()
        cal.drop = True
        with self.assertRaisesRegex(SyncError, "readback_mismatch"):
            sync(cal, schedule(lesson()))
        cal.race = True
        with self.assertRaisesRegex(SyncError, "readback_mismatch"):
            sync(cal, schedule(lesson()))

    def test_etag_precondition_and_final_list_after_write(self):
        cal = FakeCalendar([saved(lesson(), "legacy")])
        cal.get = Mock(return_value={**cal.get("legacy"), "etag": '"changed"'})
        with self.assertRaisesRegex(SyncError, "changed_before_write"):
            sync(cal, schedule(lesson(room="Б202")))
        self.assertEqual(cal.writes, [])
        cal = FakeCalendar()
        cal.list = Mock(return_value=[])
        with self.assertRaisesRegex(SyncError, "final_verification"):
            sync(cal, schedule(lesson()))


def response(status, data=None, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, json=lambda: data)


class TransportTests(unittest.TestCase):
    def test_pagination_retry_and_scope(self):
        session = Mock()
        metadata = {"accessRole": "reader", "timeZone": "Europe/Moscow", "summary": "Расписание СКТ-225"}
        session.request.side_effect = [
            response(429, headers={"Retry-After": "2"}), response(503), requests.Timeout(),
            response(200, {**metadata, "items": [{"id": "first"}], "nextPageToken": "next"}),
            response(200, {**metadata, "items": [{"id": "second"}]}),
        ]
        sleep = Mock()
        calendar = Calendar(session, sleep=sleep)
        self.assertEqual(calendar.list(NOW, NOW), [{"id": "first"}, {"id": "second"}])
        self.assertEqual([call.args for call in sleep.call_args_list], [(2,), (2,), (4,)])
        self.assertEqual(session.request.call_args.kwargs["params"]["pageToken"], "next")
        with self.assertRaisesRegex(SyncError, "read_only"):
            calendar.create("id", {})

    def test_auth_errors_are_redacted_and_write_is_not_retried(self):
        session = Mock()
        cal = Calendar(session, write=True)
        session.request.side_effect = RefreshError("sensitive provider response")
        with self.assertRaisesRegex(SyncError, "^google_authentication_failed$"):
            cal.get("id")
        session.request.reset_mock(side_effect=True)
        session.request.return_value = response(503)
        with self.assertRaisesRegex(HTTPError, "calendar_http_503"):
            cal.patch("id", '"etag"', {})
        session.request.assert_called_once()
        self.assertEqual(session.request.call_args.kwargs["headers"], {"If-Match": '"etag"'})
        self.assertEqual(session.request.call_args.kwargs["params"], {"sendUpdates": "none"})
        with patch.dict("os.environ", {"SKT225_GOOGLE_SERVICE_ACCOUNT_JSON": "bad-secret"}):
            with self.assertRaisesRegex(SyncError, "^google_credentials_missing_or_invalid$"):
                Calendar.connect(write=False)


class ValidationTests(unittest.TestCase):
    def test_partial_or_ambiguous_input_prevents_calendar_access(self):
        manifest, loader = six_week_manifest_and_loader()
        manifest["source"] = "https://sarov.msu.ru/raspisanie"
        pdf = b"%PDF-fake"
        # These PDFs are trusted geometry fixtures with bytes checked separately.
        for index, item in enumerate(manifest["files"], 1):
            item.update(week_number=index, size_bytes=len(pdf), sha256=hashlib.sha256(pdf).hexdigest(),
                        download_status="success", validation_status="verified")
        target = build_schedule(Path("."), manifest, geometry_loader=loader)
        schema = Path(__file__).parents[1] / "schema" / "schedule.schema.json"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            for item in manifest["files"]:
                (path / item["file"]).write_bytes(pdf)
            (path / "manifest.json").write_text(json.dumps(manifest))
            (path / "schedule.json").write_text(json.dumps(target))
            (path / "ambiguities.json").write_text(json.dumps({"items": [], "status": "verified"}))
            self.assertEqual(len(load_schedule(path, schema)["events"]), 69)
            with patch.object(Calendar, "connect") as connect:
                for broken in ("missing_pdf", "ambiguous", "missing_week"):
                    corrupted = deepcopy(target)
                    if broken == "missing_pdf":
                        failed_file = path / manifest["files"][0]["file"]
                        failed_file.unlink()
                    elif broken == "ambiguous":
                        corrupted["recognition"].update(status="ambiguous", safe_for_deletions=False)
                        (path / "ambiguities.json").write_text(json.dumps({"items": [], "status": "ambiguous"}))
                    else:
                        corrupted["weeks"] = corrupted["weeks"][:-1]
                    (path / "schedule.json").write_text(json.dumps(corrupted))
                    self.assertEqual(run(path, schema, "dry-run", False)["status"], "error")
                    if broken == "missing_pdf":
                        failed_file.write_bytes(pdf)
                    (path / "ambiguities.json").write_text(json.dumps({"items": [], "status": "verified"}))
                connect.assert_not_called()
