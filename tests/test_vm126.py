from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock

from sarov_schedule.calendar_api import Calendar
from sarov_schedule.calendar_cli import reconcile
from sarov_schedule.calendar_sync import CALENDAR_ID, SyncError, markers, payload
from sarov_schedule.profiles import VM126
from sarov_schedule.recognizer import TextLine, _parse_entry, _split_entries
from sarov_schedule.rules import apply_vm126_rules
from test_calendar_actions import FakeCalendar, lesson, report, response, saved, schedule


class VM126Tests(TestCase):
    def test_only_other_special_seminars_are_excluded(self):
        self.assertTrue(apply_vm126_rules("Спецсеминар «Вычислительная гидродинамика»").include)
        self.assertTrue(apply_vm126_rules("Вычислительная гидродинамика", "спецсеминар").include)
        self.assertFalse(apply_vm126_rules("Спецсеминар «Вычислительная гидродинамика и теплообмен»").include)
        self.assertFalse(apply_vm126_rules("Спецсеминар «Параллельные вычисления»").include)
        for title in ("Особенности нейронных сетей", "Нелинейные дифференциальные уравнения",
                      "Вычислительные технологии (семинар)"):
            self.assertTrue(apply_vm126_rules(title).include)

    def test_time_only_line_belongs_to_following_seminar(self):
        texts = ["18:00", "Спецсеминар «Параллельные вычисления»", "С.А. Ростомян", "аудитория Б119"]
        entries = _split_entries([TextLine(value, index * 10, index * 10 + 5)
                                  for index, value in enumerate(texts)])
        self.assertEqual(len(entries), 1)
        item, error = _parse_entry(entries[0])
        self.assertIsNone(error)
        self.assertEqual(item["explicit_start"], "18:00")
        self.assertFalse(apply_vm126_rules(item["subject"], item["lesson_type"]).include)

    def test_vm126_cannot_open_skt225_calendar(self):
        with self.assertRaisesRegex(SyncError, "calendar_not_isolated_from_skt225"):
            Calendar(Mock(), profile=VM126, calendar_id=CALENDAR_ID)

    def test_vm126_supports_writer_with_no_access_to_private_events(self):
        session = Mock()
        session.request.return_value = response(200, {
            "summary": VM126.calendar_title, "timeZone": "Europe/Moscow",
            "accessRole": "writerWithoutPrivateAccess", "items": [],
        })
        owner_calendar = "fixture@group.calendar.google.com"
        now = datetime(2026, 11, 22, tzinfo=timezone.utc)
        for write in (False, True):
            client = Calendar(session, write=write, profile=VM126, calendar_id=owner_calendar)
            self.assertEqual(client.list(now, now), [])

    def test_group_markers_and_idempotent_calendar_writes(self):
        target = lesson(source_id="vm126-v1-" + "f" * 24)
        target_calendar_id = "fixture@group.calendar.google.com"
        data = payload(target, VM126)
        self.assertIsNone(markers(data))
        self.assertEqual(markers(data, VM126)["source_id"], target["source_id"])

        foreign = saved(lesson(), "foreign")
        calendar = FakeCalendar([foreign])
        options = dict(clock=lambda: datetime(2026, 11, 22, tzinfo=timezone.utc),
                       profile=VM126, calendar_id=target_calendar_id)
        initial, second = report(), report()
        reconcile(calendar, schedule(target), "apply", False, initial, **options)
        reconcile(calendar, schedule(target), "apply", False, second, **options)
        self.assertEqual((initial["created"], second["created"], second["unchanged"]), (1, 0, 1))
        self.assertEqual(calendar.events["foreign"], foreign)
        self.assertTrue(any(ident.startswith("vm126") for ident in calendar.events))
