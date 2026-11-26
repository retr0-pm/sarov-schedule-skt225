from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://sarov.msu.ru"
SCHEDULE_URL = f"{BASE_URL}/raspisanie"
COURSE = 2
DEFAULT_MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PAGE_BYTES = 5 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30
USER_AGENT = "sarov-schedule-skt225/1.0 (+https://github.com/retr0-pm/sarov-schedule-skt225)"

SEMESTER_RE = re.compile(
    r"(Осенний|Весенний)\s+семестр\s+(\d{4})\s*[\\/]\s*(\d{4})\s+учебного\s+года",
    re.IGNORECASE,
)
COURSE_RE = re.compile(r"^(\d+)\s*курс$", re.IGNORECASE)
WEEK_RE = re.compile(r"неделя\s*(\d+)", re.IGNORECASE)
PDF_CONTENT_TYPES = {
    "application/pdf",
    "application/octet-stream",
    "binary/octet-stream",
}


class FetchError(RuntimeError):
    """Ошибка, после которой артефакт нельзя считать полным и корректным."""


def normalize_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


@dataclass(frozen=True)
class PageToken:
    kind: str
    text: str
    href: str | None = None


class _ScheduleHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[PageToken] = []
        self._anchor_depth = 0
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        self._anchor_depth += 1
        if self._anchor_depth == 1:
            self._anchor_href = dict(attrs).get("href")
            self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or self._anchor_depth == 0:
            return
        self._anchor_depth -= 1
        if self._anchor_depth == 0:
            label = normalize_text(" ".join(self._anchor_parts))
            self.tokens.append(PageToken("link", label, self._anchor_href))
            self._anchor_href = None
            self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        text = normalize_text(data)
        if not text:
            return
        if self._anchor_depth:
            self._anchor_parts.append(text)
        else:
            self.tokens.append(PageToken("text", text))


def _strip_query_and_fragment(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def is_allowed_pdf_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname == "sarov.msu.ru"
        and parts.port in (None, 443)
        and parts.path.startswith("/sites/default/files/")
        and parts.path.casefold().endswith(".pdf")
    )


def is_allowed_page_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname == "sarov.msu.ru"
        and parts.port in (None, 443)
        and parts.path.rstrip("/") == "/raspisanie"
    )


def _week_number(label: str, url: str) -> int | None:
    match = WEEK_RE.search(label)
    if match:
        return int(match.group(1))
    filename = unquote(Path(urlsplit(url).path).name)
    match = re.search(r"(?:^|[_-])(\d{1,2})(?:[_-])", filename)
    return int(match.group(1)) if match else None


def _semester_key(match: re.Match[str]) -> tuple[int, int, int]:
    # В пределах одного учебного года весенний семестр следует за осенним.
    season_rank = 1 if match.group(1).casefold().startswith("весен") else 0
    return int(match.group(2)), int(match.group(3)), season_rank


def extract_course_pdf_links(page_text: str, course: int = COURSE) -> tuple[str, list[dict[str, Any]]]:
    """Выбрать PDF только из нужного курса наиболее нового семестра."""

    parser = _ScheduleHTMLParser()
    parser.feed(page_text)
    parser.close()
    tokens = parser.tokens

    semesters: list[tuple[tuple[int, int, int], int, str]] = []
    for index, token in enumerate(tokens):
        if token.kind != "text":
            continue
        match = SEMESTER_RE.search(token.text)
        if match:
            semesters.append((_semester_key(match), index, normalize_text(match.group(0))))
    if not semesters:
        raise FetchError("На странице не найден заголовок семестра")

    _, semester_index, semester_title = max(semesters, key=lambda item: item[0])
    section_end = len(tokens)
    for _, index, _ in semesters:
        if index > semester_index:
            section_end = min(section_end, index)

    in_course = False
    course_seen = False
    found: dict[str, dict[str, Any]] = {}
    for token in tokens[semester_index + 1 : section_end]:
        if token.kind == "text":
            course_match = COURSE_RE.fullmatch(token.text)
            if course_match:
                seen_course = int(course_match.group(1))
                if seen_course == course:
                    in_course = True
                    course_seen = True
                elif in_course:
                    break
                continue
            if in_course and token.text.casefold() == "аспирантура":
                break
            continue

        if not in_course or not token.href:
            continue
        absolute_url = _strip_query_and_fragment(urljoin(BASE_URL, token.href.strip()))
        if not is_allowed_pdf_url(absolute_url):
            continue
        found[absolute_url] = {
            "source_url": absolute_url,
            "label": token.text,
            "week_number": _week_number(token.text, absolute_url),
        }

    if not course_seen:
        raise FetchError(f"В семестре «{semester_title}» не найден раздел «{course} курс»")
    if not found:
        raise FetchError(f"В разделе «{course} курс» семестра «{semester_title}» нет PDF")

    links = list(found.values())
    links.sort(
        key=lambda item: (
            item["week_number"] is None,
            item["week_number"] if item["week_number"] is not None else 10_000,
            item["source_url"],
        )
    )
    return semester_title, links


def make_http_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    session.mount("https://", adapter)
    return session


def _content_type(headers: Any) -> str:
    return str(headers.get("content-type", "")).split(";", 1)[0].strip().casefold()


def _declared_size(headers: Any) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise FetchError(f"Некорректный Content-Length: {raw!r}") from exc


class ScheduleDownloader:
    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES,
    ) -> None:
        self.session = session or make_http_session()
        self.max_pdf_bytes = max_pdf_bytes

    def _read_response(self, response: Any, *, limit: int) -> bytes:
        declared = _declared_size(response.headers)
        if declared is not None and declared > limit:
            raise FetchError(f"Объект превышает лимит {limit} байт (Content-Length={declared})")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise FetchError(f"Объект превышает лимит {limit} байт")
            chunks.append(chunk)
        return b"".join(chunks)

    def fetch_page(self, url: str = SCHEDULE_URL) -> tuple[str, str]:
        try:
            with self.session.get(
                url,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                allow_redirects=True,
                stream=True,
            ) as response:
                response.raise_for_status()
                if not is_allowed_page_url(response.url):
                    raise FetchError(f"Недопустимый адрес после перенаправления: {response.url}")
                content_type = _content_type(response.headers)
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise FetchError(f"Неожиданный тип страницы: {content_type or 'не указан'}")
                data = self._read_response(response, limit=MAX_PAGE_BYTES)
        except requests.RequestException as exc:
            raise FetchError(f"Не удалось получить страницу расписания: {exc}") from exc
        return data.decode(response.encoding or "utf-8", errors="replace"), content_type

    def fetch_pdf(self, url: str) -> tuple[bytes, str, str]:
        if not is_allowed_pdf_url(url):
            raise FetchError(f"Недопустимый URL PDF: {url}")
        try:
            with self.session.get(
                url,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                allow_redirects=True,
                stream=True,
            ) as response:
                response.raise_for_status()
                final_url = _strip_query_and_fragment(response.url)
                if not is_allowed_pdf_url(final_url):
                    raise FetchError(f"Недопустимый адрес PDF после перенаправления: {response.url}")
                content_type = _content_type(response.headers)
                if content_type not in PDF_CONTENT_TYPES:
                    raise FetchError(f"Неожиданный тип PDF: {content_type or 'не указан'}")
                data = self._read_response(response, limit=self.max_pdf_bytes)
        except requests.RequestException as exc:
            raise FetchError(f"Не удалось скачать PDF {url}: {exc}") from exc
        if not data.startswith(b"%PDF-"):
            raise FetchError(f"Файл {url} не имеет сигнатуры PDF")
        return data, content_type, final_url


def _safe_filename(source_url: str, index: int, week_number: int | None) -> str:
    original = unquote(Path(urlsplit(source_url).path).name)
    cleaned = re.sub(r"[^\w.-]+", "_", original, flags=re.UNICODE).strip("._")
    if not cleaned.casefold().endswith(".pdf"):
        cleaned += ".pdf"
    week = f"week-{week_number:02d}" if week_number is not None else f"item-{index:02d}"
    return f"{week}_{cleaned}"


def annotate_changes(files: list[dict[str, Any]], previous_manifest: dict[str, Any] | None) -> None:
    previous_by_week: dict[int, dict[str, Any]] = {}
    previous_by_url: dict[str, dict[str, Any]] = {}
    if previous_manifest:
        for item in previous_manifest.get("files", []):
            if isinstance(item.get("week_number"), int):
                previous_by_week[item["week_number"]] = item
            if isinstance(item.get("source_url"), str):
                previous_by_url[item["source_url"]] = item

    for item in files:
        old = None
        if isinstance(item.get("week_number"), int):
            old = previous_by_week.get(item["week_number"])
        old = old or previous_by_url.get(item["source_url"])
        if old is None:
            item["change_status"] = "new" if previous_manifest else "unknown"
            item["previous_sha256"] = None
        else:
            item["previous_sha256"] = old.get("sha256")
            item["change_status"] = "unchanged" if old.get("sha256") == item["sha256"] else "changed"


def build_manifest(
    *,
    semester: str,
    page_content_type: str,
    files: list[dict[str, Any]],
    previous_manifest: dict[str, Any] | None = None,
    fetched_at: datetime | None = None,
    course: int = COURSE,
) -> dict[str, Any]:
    annotate_changes(files, previous_manifest)
    timestamp = (fetched_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "schema_version": 1,
        "fetched_at": timestamp.isoformat().replace("+00:00", "Z"),
        "source": SCHEDULE_URL,
        "semester": semester,
        "course": course,
        "page_content_type": page_content_type,
        "pdf_count": len(files),
        "files": files,
    }


def load_manifest(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FetchError(f"Не удалось прочитать предыдущий манифест {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise FetchError(f"Некорректный предыдущий манифест: {path}")
    return data


def _commit_directory(staging: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if destination.exists():
        backup = destination.with_name(f".{destination.name}.backup-{os.getpid()}")
        if backup.exists():
            shutil.rmtree(backup)
        os.replace(destination, backup)
    try:
        os.replace(staging, destination)
    except Exception:
        if backup is not None and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def fetch_to_output(
    output_dir: Path,
    *,
    previous_manifest_path: Path | None = None,
    downloader: ScheduleDownloader | None = None,
    course: int = COURSE,
) -> dict[str, Any]:
    """Собрать полный результат в staging и заменить output только после успеха."""

    downloader = downloader or ScheduleDownloader()
    previous = load_manifest(previous_manifest_path)
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        page_text, page_content_type = downloader.fetch_page()
        if previous is not None and previous.get("course") != course:
            raise FetchError("Предыдущий манифест относится к другому курсу")
        semester, links = extract_course_pdf_links(page_text, course=course)
        files: list[dict[str, Any]] = []
        for index, link in enumerate(links, start=1):
            data, content_type, final_url = downloader.fetch_pdf(link["source_url"])
            filename = _safe_filename(final_url, index, link["week_number"])
            path = staging / filename
            if path.exists():
                raise FetchError(f"Коллизия имён файлов: {filename}")
            path.write_bytes(data)
            files.append(
                {
                    "file": filename,
                    "label": link["label"],
                    "week_number": link["week_number"],
                    "source_url": link["source_url"],
                    "final_url": final_url,
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "content_type": content_type,
                    "download_status": "success",
                    "validation_status": "verified",
                }
            )
        if not files:
            raise FetchError("Нельзя публиковать пустой результат")
        manifest = build_manifest(
            semester=semester,
            page_content_type=page_content_type,
            files=files,
            previous_manifest=previous,
            course=course,
        )
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _commit_directory(staging, output_dir)
        return manifest
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def iter_manifest_pdf_paths(output_dir: Path, manifest: dict[str, Any]) -> Iterable[Path]:
    for item in manifest.get("files", []):
        yield output_dir / item["file"]
