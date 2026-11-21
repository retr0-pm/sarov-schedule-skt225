from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


def observe_verified_pdf(
    state: dict[str, Any],
    *,
    week_id: str,
    pdf_sha256: str,
    rules_version: str,
    checked_at: str | None = None,
) -> dict[str, Any]:
    """Зафиксировать PDF, не выдавая наблюдение за успешную синхронизацию."""

    result = deepcopy(state)
    timestamp = checked_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    weeks = result.setdefault("weeks", {})
    previous = weeks.get(week_id, {})
    previous_sync = deepcopy(previous.get("calendar_sync", {}))
    changed = previous.get("pdf_sha256") not in (None, pdf_sha256)
    rules_changed = previous.get("rules_version") not in (None, rules_version)
    if changed or rules_changed or not previous_sync:
        previous_sync.update(
            {
                "status": "pending",
                "last_error": None,
            }
        )
    weeks[week_id] = {
        "week_id": week_id,
        "pdf_sha256": pdf_sha256,
        "rules_version": rules_version,
        "pdf": {
            "download_status": "success",
            "validation_status": "verified",
            "changed": changed,
            "checked_at": timestamp,
        },
        "calendar_sync": previous_sync,
    }
    return result
