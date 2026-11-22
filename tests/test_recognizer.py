from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from sarov_schedule.recognizer import (
    GROUP,
    PageGeometry,
    RecognitionError,
    WordBox,
    build_schedule,
    recognize_page_geometry,
    validate_schedule,
)
from sarov_schedule.rules import RULES_VERSION, apply_skt225_rules, normalize_subject

FIXTURES = Path(__file__).parent / "fixtures" / "geometry"
SEMESTER = "Осенний семестр 2026/2027 учебного года"
FIXED_TIME = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def load_page(label: str) -> tuple[dict, PageGeometry]:
    payload = json.loads((FIXTURES / f"{label}.json").read_text(encoding="utf-8"))
    return payload, PageGeometry.from_dict(payload["geometry"])


def recognize_fixture(label: str):
    payload, geometry = load_page(label)
    return recognize_page_geometry(geometry, semester=SEMESTER, source_pdf=payload["derived_from"])


def included_events(result):
    return [
        event
        for event in result.raw_events
        if apply_skt225_rules(event["subject"], event.get("lesson_type")).include
    ]


def six_week_manifest_and_loader():
    pages_by_name: dict[str, list[PageGeometry]] = {}
    files = []
    for week in range(13, 19):
        payload = json.loads((FIXTURES / "weeks" / f"week-{week:02d}.json").read_text(encoding="utf-8"))
        pages_by_name[payload["derived_from"]] = [PageGeometry.from_dict(item) for item in payload["pages"]]
        files.append(
            {
                "file": payload["derived_from"],
                "label": f"Неделя {week}",
                "week_number": week,
                "source_url": f"https://sarov.msu.ru/sites/default/files/2026-11/week-{week}.pdf",
                "size_bytes": 1,
                "sha256": payload["source_pdf_sha256"],
                "content_type": "application/pdf",
            }
        )
    manifest = {
        "schema_version": 1,
        "fetched_at": "2026-11-21T12:00:00Z",
        "source": "https://sarov.msu.ru/raspisanie",
        "semester": SEMESTER,
        "course": 2,
        "pdf_count": len(files),
        "files": files,
    }

    def loader(path: Path):
        return deepcopy(pages_by_name[path.name])

    return manifest, loader


class RulesV3Tests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(
            normalize_subject("  «ОСОБЕННОСТИ\n нейронных   сетей»  "),
            "особенности нейронных сетей",
        )

    def test_two_subject_families_are_excluded(self):
        values = [
            "Особенности нейронных сетей",
            "Особенности практического использования нейронных сетей",
            "Решение нелинейных дифференциальных уравнений",
            "Нелинейные дифференциальные уравнения",
        ]
        self.assertTrue(all(not apply_skt225_rules(value).include for value in values))

    def test_only_named_special_seminar_is_kept(self):
        allowed = apply_skt225_rules('Спецсеминар «Параллельные вычисления и анализ алгоритмов»')
        rejected = apply_skt225_rules('Спецсеминар «Вычислительные технологии»')
        self.assertTrue(allowed.include)
        self.assertEqual(allowed.reason, "allowed_special_seminar")
        self.assertFalse(rejected.include)
        self.assertEqual(rejected.reason, "excluded_special_seminar")

    def test_ordinary_subject_is_not_excluded_by_single_word(self):
        self.assertTrue(apply_skt225_rules("Параллельные алгоритмы").include)
        self.assertTrue(apply_skt225_rules("Основы теории графов").include)


class GeometryRegressionTests(unittest.TestCase):
    def test_26_november_does_not_take_vm_225_pairs_1_to_3(self):
        result = recognize_fixture("2026-11-26")
        self.assertEqual(result.ambiguities, [])
        self.assertFalse(any(event["lesson_number"] in {1, 2, 3} for event in result.raw_events))
        self.assertIn(4, {event["lesson_number"] for event in result.raw_events})

    def test_3_december_keeps_unix_and_filters_special_seminar(self):
        result = recognize_fixture("2026-12-03")
        self.assertFalse(any(event["lesson_number"] in {1, 2} for event in result.raw_events))
        sixth = [event for event in result.raw_events if event["lesson_number"] == 6]
        self.assertEqual(len(sixth), 2)
        included = included_events(result)
        self.assertTrue(
            any(event["lesson_number"] == 6 and "операционные системы" in event["subject"] for event in included)
        )
        self.assertFalse(any("Вычислительные технологии" in event["subject"] for event in included))

    def test_24_december_merged_cells_do_not_mix_physics_column(self):
        result = recognize_fixture("2026-12-24")
        self.assertFalse(any(event["lesson_number"] in {1, 2, 3} for event in result.raw_events))
        included = included_events(result)
        self.assertEqual([event["lesson_number"] for event in included], [4, 5])
        self.assertTrue(all("История и методология" in event["subject"] for event in included))
        self.assertFalse(any("исследовательская работа" in event["subject"].casefold() for event in included))

    def test_25_december_separates_nir_and_high_performance_computing(self):
        result = recognize_fixture("2026-12-25")
        self.assertFalse(any(event["lesson_number"] in {1, 2} for event in result.raw_events))
        included = included_events(result)
        hpc = [event for event in included if "Высокопроизводительные вычисления" in event["subject"]]
        self.assertEqual([event["lesson_number"] for event in hpc], [3, 4])
        self.assertTrue(all("Научно" not in event["subject"] for event in hpc))

    def test_28_december_pair_5_keeps_only_parallel_special_seminar(self):
        result = recognize_fixture("2026-12-28")
        pair_five = [event for event in included_events(result) if event["lesson_number"] == 5]
        self.assertEqual(len(pair_five), 1)
        self.assertIn("Параллельные вычисления и анализ алгоритмов", pair_five[0]["subject"])

    def test_geometry_is_scale_independent(self):
        payload, geometry = load_page("2026-12-03")
        factor = 1.37
        scaled = PageGeometry(
            page_number=geometry.page_number,
            width=geometry.width * factor,
            height=geometry.height * factor,
            words=tuple(
                replace(
                    word,
                    x0=word.x0 * factor,
                    x1=word.x1 * factor,
                    top=word.top * factor,
                    bottom=word.bottom * factor,
                )
                for word in geometry.words
            ),
            verticals=tuple(
                replace(segment, x=segment.x * factor, top=segment.top * factor, bottom=segment.bottom * factor)
                for segment in geometry.verticals
            ),
            horizontals=tuple(
                replace(segment, y=segment.y * factor, x0=segment.x0 * factor, x1=segment.x1 * factor)
                for segment in geometry.horizontals
            ),
        )
        original = recognize_page_geometry(geometry, semester=SEMESTER, source_pdf=payload["derived_from"])
        resized = recognize_page_geometry(scaled, semester=SEMESTER, source_pdf=payload["derived_from"])
        original_core = [(event["lesson_number"], event["subject"]) for event in original.raw_events]
        resized_core = [(event["lesson_number"], event["subject"]) for event in resized.raw_events]
        self.assertEqual(original_core, resized_core)

    def test_missing_group_header_is_ambiguous(self):
        payload, geometry = load_page("2026-12-03")
        words = tuple(
            replace(word, text="UNKNOWN") if word.text == GROUP else word
            for word in geometry.words
        )
        result = recognize_page_geometry(
            replace(geometry, words=words), semester=SEMESTER, source_pdf=payload["derived_from"]
        )
        self.assertEqual(result.raw_events, [])
        self.assertEqual(result.ambiguities[0]["code"], "target_group_header_count")


class StructuredScheduleTests(unittest.TestCase):
    def test_six_real_geometry_weeks_produce_69_events(self):
        manifest, loader = six_week_manifest_and_loader()
        schedule = build_schedule(Path("."), manifest, generated_at=FIXED_TIME, geometry_loader=loader)
        self.assertEqual(schedule["rules_version"], RULES_VERSION)
        self.assertEqual(schedule["recognition"]["status"], "verified")
        self.assertEqual(schedule["recognition"]["included_count"], 69)
        self.assertEqual(schedule["recognition"]["excluded_count"], 29)
        self.assertEqual(schedule["recognition"]["ambiguity_count"], 0)
        self.assertEqual([week["included_count"] for week in schedule["weeks"]], [12, 16, 12, 15, 9, 5])
        self.assertTrue(all(event["timezone"] == "Europe/Moscow" for event in schedule["events"]))
        self.assertTrue(all(event["date"].startswith("2026-") for event in schedule["events"]))

    def test_output_is_stable_and_source_ids_are_unique(self):
        manifest, loader = six_week_manifest_and_loader()
        first = build_schedule(Path("."), manifest, generated_at=FIXED_TIME, geometry_loader=loader)
        second = build_schedule(Path("."), manifest, generated_at=FIXED_TIME, geometry_loader=loader)
        self.assertEqual(first, second)
        source_ids = [event["source_id"] for event in first["events"]]
        self.assertEqual(len(source_ids), len(set(source_ids)))

    def test_source_id_is_not_based_only_on_date(self):
        payload, geometry = load_page("2026-12-25")
        manifest = {
            "schema_version": 1,
            "source": "https://sarov.msu.ru/raspisanie",
            "semester": SEMESTER,
            "course": 2,
            "pdf_count": 1,
            "files": [
                {
                    "file": payload["derived_from"],
                    "week_number": 17,
                    "sha256": payload["source_pdf_sha256"],
                }
            ],
        }
        moved = replace(
            geometry,
            words=tuple(
                replace(word, text="23.12") if word.text == "25.12" else word
                for word in geometry.words
            ),
        )
        original_schedule = build_schedule(
            Path("."), manifest, generated_at=FIXED_TIME, geometry_loader=lambda _path: [geometry]
        )
        moved_schedule = build_schedule(
            Path("."), manifest, generated_at=FIXED_TIME, geometry_loader=lambda _path: [moved]
        )
        original_ids = {(event["subject"], str(event["lesson_number"])): event["source_id"] for event in original_schedule["events"]}
        moved_ids = {(event["subject"], str(event["lesson_number"])): event["source_id"] for event in moved_schedule["events"]}
        self.assertEqual(original_ids, moved_ids)
        self.assertNotEqual(original_schedule["weeks"][0]["week_id"], moved_schedule["weeks"][0]["week_id"])

    def test_previous_schedule_is_reference_not_current_data(self):
        manifest, loader = six_week_manifest_and_loader()
        previous = build_schedule(Path("."), manifest, generated_at=FIXED_TIME, geometry_loader=loader)
        with tempfile.TemporaryDirectory() as temp:
            previous_path = Path(temp) / "schedule.json"
            previous_path.write_text(json.dumps(previous, ensure_ascii=False), encoding="utf-8")
            current = build_schedule(
                Path("."),
                manifest,
                generated_at=FIXED_TIME,
                geometry_loader=loader,
                previous_schedule_path=previous_path,
            )
        self.assertFalse(current["previous_schedule"]["used_as_current"])
        self.assertTrue(all(week["previous_verified_data_available"] for week in current["weeks"]))

    def test_schema_validation_rejects_unsafe_status(self):
        manifest, loader = six_week_manifest_and_loader()
        schedule = build_schedule(Path("."), manifest, generated_at=FIXED_TIME, geometry_loader=loader)
        schedule["ambiguities"].append({"code": "test"})
        schedule["recognition"]["ambiguity_count"] = 1
        schedule["recognition"]["safe_for_deletions"] = True
        with self.assertRaisesRegex(RecognitionError, "полностью проверенный"):
            validate_schedule(schedule, manifest)

    def test_json_schema_file_is_valid_json_and_matches_versions(self):
        schema = json.loads((Path(__file__).parents[1] / "schema" / "schedule.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(schema["properties"]["rules_version"]["const"], RULES_VERSION)


if __name__ == "__main__":
    unittest.main()
