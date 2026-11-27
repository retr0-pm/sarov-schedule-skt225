from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

RULES_VERSION = "skt225-v3"

_EXCLUDED_SUBJECTS = {
    "особенности нейронных сетей",
    "особенности практического использования нейронных сетей",
    "решение нелинейных дифференциальных уравнений",
    "нелинейные дифференциальные уравнения",
}
_ALLOWED_SPECIAL_SEMINAR = "параллельные вычисления и анализ алгоритмов"
_LESSON_TYPE_SUFFIX_RE = re.compile(
    r"\s*\((?:лекция|семинар|практика|практическое занятие|лабораторная|зач[её]т|экзамен)\)\s*$",
    re.IGNORECASE,
)


def normalize_subject(value: str) -> str:
    """Нормализация для правил без широкого нечёткого совпадения."""

    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    text = text.translate(str.maketrans({"«": '"', "»": '"', "„": '"', "“": '"', "”": '"'}))
    text = re.sub(r"(?<=\w)-\s+(?=\w)", "-", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" \"'.,;:")
    text = _LESSON_TYPE_SUFFIX_RE.sub("", text)
    return text.strip(" \"'.,;:")


def _seminar_title(normalized: str) -> str | None:
    match = re.match(r"^спецсеминар\s*[\"']?\s*(.*?)\s*[\"']?$", normalized)
    return match.group(1).strip(" \"'.,;:") if match else None


@dataclass(frozen=True)
class RuleDecision:
    include: bool
    reason: str
    normalized_subject: str


def apply_skt225_rules(subject: str, lesson_type: str | None = None) -> RuleDecision:
    normalized = normalize_subject(subject)
    normalized_type = normalize_subject(lesson_type or "")
    seminar_title = _seminar_title(normalized)
    is_special_seminar = normalized_type == "спецсеминар" or seminar_title is not None

    if is_special_seminar:
        title = seminar_title if seminar_title is not None else normalized
        if _ALLOWED_SPECIAL_SEMINAR in title:
            return RuleDecision(True, "allowed_special_seminar", normalized)
        return RuleDecision(False, "excluded_special_seminar", normalized)

    if normalized in _EXCLUDED_SUBJECTS:
        return RuleDecision(False, "excluded_subject", normalized)
    return RuleDecision(True, "included", normalized)


def allowed_special_seminar_title() -> str:
    return _ALLOWED_SPECIAL_SEMINAR


def apply_vm126_rules(
    subject: str, lesson_type: str | None = None, *, teacher: str | None = None,
) -> RuleDecision:
    normalized = normalize_subject(subject)
    title = _seminar_title(normalized)
    if title is None and normalize_subject(lesson_type or "") != "спецсеминар":
        if normalized in {"иностранный язык", "английский язык"}:
            name = normalize_subject(teacher or "")
            if re.search(r"\bзабродина\b", name) and not re.search(r"\bмордвинова\b", name):
                return RuleDecision(False, "excluded_english_teacher", normalized)
        return RuleDecision(True, "included", normalized)
    seminar = title if title is not None else normalized
    include = seminar == "вычислительная гидродинамика"
    return RuleDecision(include, "allowed_special_seminar" if include else "excluded_special_seminar", normalized)
