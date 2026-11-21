from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from sarov_schedule.fetcher import (
    FetchError,
    ScheduleDownloader,
    annotate_changes,
    extract_course_pdf_links,
    fetch_to_output,
)
from sarov_schedule.sync_state import observe_verified_pdf
from sarov_schedule.validate import validate_artifact

FIXTURES = Path(__file__).parent / "fixtures"
PDF_A = (FIXTURES / "week_a.pdf").read_bytes()
PDF_B = (FIXTURES / "week_b.pdf").read_bytes()


class FakeResponse:
    def __init__(self, data: bytes, *, url: str, content_type: str, declared_size: int | None = None):
        self.data = data
        self.url = url
        self.encoding = "utf-8"
        self.headers = {"content-type": content_type}
        if declared_size is not None:
            self.headers["content-length"] = str(declared_size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.data), chunk_size):
            yield self.data[offset : offset + chunk_size]


class QueueSession:
    def __init__(self, responses):
        self.responses = list(responses)

    def get(self, *_args, **_kwargs):
        if not self.responses:
            raise AssertionError("Неожиданный HTTP-запрос")
        return self.responses.pop(0)


class ParserTests(unittest.TestCase):
    def test_newest_semester_and_only_second_course(self):
        semester, links = extract_course_pdf_links((FIXTURES / "schedule_page.html").read_text(encoding="utf-8"))
        self.assertEqual(semester, "Осенний семестр 2026/2027 учебного года")
        self.assertEqual([item["week_number"] for item in links], [1, 2])
        self.assertTrue(all("second_" in item["source_url"] for item in links))

    def test_spring_is_newer_within_same_academic_year(self):
        html = """
        <h4>Весенний семестр 2026/2027 учебного года</h4><p>2 курс</p>
        <a href='/sites/default/files/2027-02/spring_01.pdf'>Неделя 1</a>
        <h4>Осенний семестр 2026/2027 учебного года</h4><p>2 курс</p>
        <a href='/sites/default/files/2026-09/autumn_01.pdf'>Неделя 1</a>
        """
        semester, links = extract_course_pdf_links(html)
        self.assertTrue(semester.startswith("Весенний"))
        self.assertIn("spring_01.pdf", links[0]["source_url"])

    def test_other_courses_and_postgraduate_are_excluded(self):
        _, links = extract_course_pdf_links((FIXTURES / "schedule_page.html").read_text(encoding="utf-8"))
        urls = " ".join(item["source_url"] for item in links)
        self.assertNotIn("first", urls)
        self.assertNotIn("postgrad", urls)
        self.assertNotIn("old", urls)

    def test_external_and_non_pdf_links_are_excluded(self):
        _, links = extract_course_pdf_links((FIXTURES / "schedule_page.html").read_text(encoding="utf-8"))
        self.assertEqual(len(links), 2)
        self.assertTrue(all(item["source_url"].startswith("https://sarov.msu.ru/sites/default/files/") for item in links))

    def test_missing_course_section_fails(self):
        html = "<h4>Осенний семестр 2026/2027 учебного года</h4><p>1 курс</p>"
        with self.assertRaisesRegex(FetchError, "2 курс"):
            extract_course_pdf_links(html)


class DownloadValidationTests(unittest.TestCase):
    URL = "https://sarov.msu.ru/sites/default/files/2026-09/week.pdf"

    def test_wrong_pdf_signature_fails(self):
        downloader = ScheduleDownloader(
            session=QueueSession([FakeResponse(b"not a pdf", url=self.URL, content_type="application/pdf")])
        )
        with self.assertRaisesRegex(FetchError, "сигнатуры"):
            downloader.fetch_pdf(self.URL)

    def test_oversized_pdf_fails_from_declared_length(self):
        downloader = ScheduleDownloader(
            session=QueueSession([FakeResponse(PDF_A, url=self.URL, content_type="application/pdf", declared_size=999)]),
            max_pdf_bytes=100,
        )
        with self.assertRaisesRegex(FetchError, "превышает лимит"):
            downloader.fetch_pdf(self.URL)

    def test_oversized_pdf_fails_while_streaming(self):
        downloader = ScheduleDownloader(
            session=QueueSession([FakeResponse(PDF_A, url=self.URL, content_type="application/pdf")]),
            max_pdf_bytes=10,
        )
        with self.assertRaisesRegex(FetchError, "превышает лимит"):
            downloader.fetch_pdf(self.URL)


class ManifestAndStateTests(unittest.TestCase):
    def _item(self, data: bytes) -> dict:
        return {
            "file": "week-01_week.pdf",
            "label": "Неделя 1",
            "week_number": 1,
            "source_url": "https://sarov.msu.ru/sites/default/files/2026-09/week.pdf",
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_type": "application/pdf",
        }

    def test_same_url_changed_pdf_is_detected(self):
        old = {"files": [self._item(PDF_A)]}
        files = [self._item(PDF_B)]
        annotate_changes(files, old)
        self.assertEqual(files[0]["change_status"], "changed")
        self.assertEqual(files[0]["previous_sha256"], old["files"][0]["sha256"])

    def test_identical_runs_have_identical_hashes(self):
        first = [self._item(PDF_A)]
        second = [self._item(PDF_A)]
        annotate_changes(second, {"files": first})
        self.assertEqual(first[0]["sha256"], second[0]["sha256"])
        self.assertEqual(second[0]["change_status"], "unchanged")

    def test_change_is_reflected_in_manifest_entry(self):
        files = [self._item(PDF_B)]
        annotate_changes(files, {"files": [self._item(PDF_A)]})
        encoded = json.dumps({"files": files})
        self.assertIn('"change_status": "changed"', encoded)

    def test_pdf_observation_does_not_confirm_calendar_sync(self):
        state = {
            "schema_version": 1,
            "weeks": {
                "2026-W01": {
                    "pdf_sha256": self._item(PDF_A)["sha256"],
                    "rules_version": "v1",
                    "calendar_sync": {"status": "verified", "last_success_at": "2026-01-01T00:00:00Z"},
                }
            },
        }
        result = observe_verified_pdf(
            state,
            week_id="2026-W01",
            pdf_sha256=self._item(PDF_B)["sha256"],
            rules_version="v1",
        )
        self.assertEqual(result["weeks"]["2026-W01"]["pdf"]["validation_status"], "verified")
        self.assertEqual(result["weeks"]["2026-W01"]["calendar_sync"]["status"], "pending")
        self.assertEqual(state["weeks"]["2026-W01"]["calendar_sync"]["status"], "verified")


class EndToEndOfflineTests(unittest.TestCase):
    def test_complete_artifact_is_built_and_validated(self):
        html = (FIXTURES / "schedule_page.html").read_bytes()
        responses = [
            FakeResponse(html, url="https://sarov.msu.ru/raspisanie", content_type="text/html; charset=utf-8"),
            FakeResponse(PDF_A, url="https://sarov.msu.ru/sites/default/files/2026-09/second_01.pdf", content_type="application/pdf"),
            FakeResponse(PDF_B, url="https://sarov.msu.ru/sites/default/files/2026-09/second_02.pdf", content_type="application/pdf"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            manifest = fetch_to_output(output, downloader=ScheduleDownloader(session=QueueSession(responses)))
            self.assertEqual(manifest["pdf_count"], 2)
            self.assertEqual(validate_artifact(output)["pdf_count"], 2)
            self.assertEqual({path.name for path in output.iterdir()}, {"manifest.json", *(item["file"] for item in manifest["files"])})

    def test_failed_rebuild_does_not_replace_last_good_output(self):
        html = (FIXTURES / "schedule_page.html").read_bytes()
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "output"
            output.mkdir()
            marker = output / "last-good.txt"
            marker.write_text("keep", encoding="utf-8")
            responses = [
                FakeResponse(html, url="https://sarov.msu.ru/raspisanie", content_type="text/html"),
                FakeResponse(b"broken", url="https://sarov.msu.ru/sites/default/files/2026-09/second_01.pdf", content_type="application/pdf"),
            ]
            with self.assertRaises(FetchError):
                fetch_to_output(output, downloader=ScheduleDownloader(session=QueueSession(responses)))
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
