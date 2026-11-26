"""Google Calendar REST transport scoped to the single allowed calendar."""

from __future__ import annotations

import hashlib
import json
import os
import time
from urllib.parse import quote

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from .calendar_sync import CALENDAR_ID, MOSCOW, SyncError

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/" + quote(CALENDAR_ID, safe="") + "/events"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
FORBIDDEN_REASONS = {
    "SERVICE_DISABLED": "google_calendar_api_disabled",
    "accessNotConfigured": "google_calendar_api_disabled",
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": "google_calendar_scope_insufficient",
    "insufficientPermissions": "google_calendar_permission_insufficient",
    "forbidden": "google_calendar_access_forbidden",
    "domainPolicy": "google_calendar_access_restricted_by_policy",
    "rateLimitExceeded": "google_calendar_rate_limited",
    "userRateLimitExceeded": "google_calendar_rate_limited",
    "quotaExceeded": "google_calendar_quota_exceeded",
    "forbiddenForNonOrganizer": "google_calendar_organizer_required",
}


class HTTPError(SyncError):
    def __init__(self, status: int, diagnostic: str | None = None):
        self.status = status
        super().__init__(diagnostic or f"calendar_http_{status}")


def forbidden_diagnostic(response: requests.Response) -> str | None:
    # Google can put reason codes in legacy errors or structured ErrorInfo.
    # Publish only fixed messages, without error messages or project metadata.
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    for key in ("details", "errors"):
        items = error.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            reason = item.get("reason") if isinstance(item, dict) else None
            if isinstance(reason, str) and reason in FORBIDDEN_REASONS:
                return FORBIDDEN_REASONS[reason]
    return None


def event_id(source_id: str) -> str:
    # Google accepts lowercase base32hex. Hex is a subset of that alphabet.
    return "skt225" + hashlib.sha256(source_id.encode("ascii")).hexdigest()


class Calendar:
    def __init__(self, session, *, write: bool = False, sleep=time.sleep):
        self.session = session
        self.write = write
        self.sleep = sleep

    @classmethod
    def connect(cls, *, write: bool) -> Calendar:
        scopes = [SCOPE if write else SCOPE + ".readonly"]
        try:
            key = os.environ.get("SKT225_GOOGLE_SERVICE_ACCOUNT_JSON")
            if key:
                settings = json.loads(key)
                if (settings.get("type") != "service_account"
                        or settings.get("token_uri") != "https://oauth2.googleapis.com/token"):
                    raise ValueError("Invalid service account configuration")
                credentials = service_account.Credentials.from_service_account_info(settings, scopes=scopes)
            else:
                raise ValueError("Missing service account configuration")
            return cls(AuthorizedSession(credentials, max_refresh_attempts=0, refresh_timeout=15), write=write)
        except (GoogleAuthError, KeyError, ValueError, TypeError, AttributeError):
            raise SyncError("google_credentials_missing_or_invalid") from None

    def request(self, method: str, ident: str = "", **kwargs) -> dict | None:
        if method != "GET" and not self.write:
            raise SyncError("write_in_read_only_mode")
        url = EVENTS_URL + ("/" + quote(ident, safe="") if ident else "")
        attempts = 4 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(method, url, timeout=(5, 20), allow_redirects=False, **kwargs)
                status = response.status_code
                if 200 <= status < 300:
                    if status == 204:
                        return None
                    try:
                        data = response.json()
                    except ValueError:
                        raise SyncError("calendar_invalid_response") from None
                    if not isinstance(data, dict):
                        raise SyncError("calendar_invalid_response")
                    return data
                diagnostic = forbidden_diagnostic(response) if status == 403 else None
                error = HTTPError(status, diagnostic)
                transient = status == 429 or 500 <= status < 600 or diagnostic == "google_calendar_rate_limited"
                retry_after = response.headers.get("Retry-After", "")
            except GoogleAuthError:
                raise SyncError("google_authentication_failed") from None
            except (requests.Timeout, requests.ConnectionError):
                error = SyncError("calendar_network_error")
                transient = True
                retry_after = ""
            except requests.RequestException:
                raise SyncError("calendar_transport_error") from None
            if not transient or attempt + 1 == attempts:
                raise error
            self.sleep(max(2**attempt, min(15, int(retry_after)) if retry_after.isdecimal() else 0))
        raise AssertionError("Unreachable retry state")

    def list(self, start, end) -> list[dict]:
        params = {
            "timeMin": start.isoformat(), "timeMax": end.isoformat(),
            "timeZone": MOSCOW.key, "maxResults": 2500, "showDeleted": "false",
        }
        events = []
        tokens = set()
        for _ in range(50):
            page = self.request("GET", params=params)
            if (page is None or page.get("timeZone") != MOSCOW.key
                    or page.get("summary") != "Расписание СКТ-225"
                    or page.get("accessRole") not in (("writer", "owner") if self.write
                                                     else ("reader", "writer", "owner"))):
                raise SyncError("calendar_identity_or_access_mismatch")
            items = page.get("items", [])
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise SyncError("calendar_invalid_response")
            events.extend(items)
            token = page.get("nextPageToken")
            if not token:
                return events
            if not isinstance(token, str) or token in tokens:
                break
            tokens.add(token)
            params["pageToken"] = token
        raise SyncError("calendar_incomplete_pagination")

    def get(self, ident: str) -> dict | None:
        return self.request("GET", ident)

    def create(self, ident: str, body: dict) -> dict | None:
        return self.request("POST", json={**body, "id": ident}, params={"sendUpdates": "none"})

    def patch(self, ident: str, etag: str, body: dict) -> dict | None:
        return self.request("PATCH", ident, json=body, params={"sendUpdates": "none"}, headers={"If-Match": etag})

    def delete(self, ident: str, etag: str) -> None:
        self.request("DELETE", ident, params={"sendUpdates": "none"}, headers={"If-Match": etag})
