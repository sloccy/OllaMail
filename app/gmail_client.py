import base64
import datetime
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app import db
from app.config import EMAIL_BODY_TRUNCATION, GMAIL_LOOKBACK_HOURS, GMAIL_MAX_RESULTS

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "/credentials/credentials.json")
REDIRECT_URI = "http://localhost"

# Gmail system label IDs
LABEL_SPAM = "SPAM"
LABEL_INBOX = "INBOX"
LABEL_UNREAD = "UNREAD"
LABEL_TRASH = "TRASH"

_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
_OAUTH2_API = "https://www.googleapis.com/oauth2/v2"


def _mount_retry_adapter(session: AuthorizedSession) -> None:
    retry = Retry(
        total=3,
        connect=3,
        read=0,
        status=3,
        backoff_factor=1,
        backoff_jitter=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self._parts.append(stripped)


def _html_to_text(html: str) -> str:
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    return "\n".join(extractor._parts)


def get_auth_url(state: str) -> str:
    flow = Flow.from_client_secrets_file(CREDENTIALS_FILE, scopes=SCOPES, state=state)
    flow.redirect_uri = REDIRECT_URI
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )
    return auth_url


def exchange_code(state: str, code: str) -> tuple[str, str]:
    flow = Flow.from_client_secrets_file(CREDENTIALS_FILE, scopes=SCOPES, state=state)
    flow.redirect_uri = REDIRECT_URI
    flow.fetch_token(code=code)
    creds = flow.credentials
    email = _get_email(creds)
    return email, creds.to_json()


def _get_email(creds: Credentials) -> str:
    session = AuthorizedSession(creds)
    _mount_retry_adapter(session)
    resp = session.get(f"{_OAUTH2_API}/userinfo")
    resp.raise_for_status()
    return resp.json()["email"]


def get_session(account: dict) -> AuthorizedSession:
    creds = Credentials.from_authorized_user_info(json.loads(account["credentials_json"]), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise ValueError("Credentials are invalid and no refresh token is available. Please reconnect the account.")
        creds.refresh(Request())
        db.update_account_credentials(account["id"], creds.to_json())
    session = AuthorizedSession(creds)
    _mount_retry_adapter(session)
    return session


def build_label_cache(session, label_names: list) -> dict:
    """Return {name: id} for the given label names, creating any that are missing."""
    all_labels = list_labels(session)
    existing = {lbl["name"].lower(): lbl["id"] for lbl in all_labels}
    cache = {}
    for name in label_names:
        if name.lower() in existing:
            cache[name] = existing[name.lower()]
        else:
            try:
                resp = session.post(
                    f"{_GMAIL_API}/users/me/labels",
                    json={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
                )
                resp.raise_for_status()
                cache[name] = resp.json()["id"]
            except Exception as e:
                db.add_log("WARNING", f"Could not create label '{name}': {e}")
    return cache


def _paginate_message_ids(session, params: dict, max_pages: int = 0) -> list:
    params = dict(params)
    ids = []
    pages = 0
    while True:
        resp = session.get(f"{_GMAIL_API}/users/me/messages", params=params)
        resp.raise_for_status()
        data = resp.json()
        ids.extend(m["id"] for m in data.get("messages", []))
        pages += 1
        if max_pages and pages >= max_pages:
            break
        next_token = data.get("nextPageToken")
        if not next_token:
            break
        params["pageToken"] = next_token
    return ids


def list_recent_message_ids(session, max_results=GMAIL_MAX_RESULTS, lookback_hours=GMAIL_LOOKBACK_HOURS) -> list:
    after_ts = int(time.time() - lookback_hours * 3600)
    return _paginate_message_ids(session, {"maxResults": max_results, "q": f"in:inbox after:{after_ts}"})


def iter_message_details(session, message_ids: list):
    """Yield email dicts one at a time, fetching in batches of 100 via thread pool.
    Frees each raw API response after yielding to minimise peak memory."""
    for i in range(0, len(message_ids), 100):
        batch_ids = message_ids[i : i + 100]
        results: dict = {}

        def _fetch_one(msg_id):
            s = AuthorizedSession(session.credentials)
            _mount_retry_adapter(s)
            resp = s.get(f"{_GMAIL_API}/users/me/messages/{msg_id}", params={"format": "full"})
            resp.raise_for_status()
            return resp.json()

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_fetch_one, mid): mid for mid in batch_ids}
            for future in as_completed(futures):
                msg_id = futures[future]
                try:
                    results[msg_id] = future.result()
                except Exception as e:
                    db.add_log("WARNING", f"Batch fetch failed for message {msg_id}: {e}")

        for msg_id in batch_ids:
            full = results.pop(msg_id, None)
            if not full:
                continue
            headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
            body = _extract_body(full["payload"], EMAIL_BODY_TRUNCATION)
            yield {
                "id": msg_id,
                "subject": headers.get("Subject", "(no subject)"),
                "sender": headers.get("From", "unknown"),
                "snippet": full.get("snippet", ""),
                "body": body[:EMAIL_BODY_TRUNCATION],
            }


def list_labels(session) -> list:
    resp = session.get(f"{_GMAIL_API}/users/me/labels")
    resp.raise_for_status()
    result = resp.json()
    return sorted(
        [{"id": lbl["id"], "name": lbl["name"]} for lbl in result.get("labels", [])],
        key=lambda x: x["name"].lower(),
    )


def fetch_emails_older_than(
    session, days: int, label_name: str | None = None, excluded_labels: list | None = None
) -> list:
    cutoff = datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=days)
    query = f"before:{cutoff.strftime('%Y/%m/%d')}"
    if label_name:
        query += f' label:"{label_name}"'
    if excluded_labels:
        for lbl in excluded_labels:
            query += f' -label:"{lbl}"'
    return _paginate_message_ids(session, {"maxResults": 500, "q": query}, max_pages=5)


def batch_modify_emails(session, modifications: list) -> None:
    """Apply label modifications using batchModify. Groups by identical add/remove combos."""
    if not modifications:
        return
    groups: dict = {}
    for message_id, add_labels, remove_labels in modifications:
        key = (tuple(sorted(add_labels)), tuple(sorted(remove_labels)))
        groups.setdefault(key, []).append(message_id)
    for (add_labels, remove_labels), message_ids in groups.items():
        for i in range(0, len(message_ids), 1000):
            body: dict = {"ids": message_ids[i : i + 1000]}
            if add_labels:
                body["addLabelIds"] = list(add_labels)
            if remove_labels:
                body["removeLabelIds"] = list(remove_labels)
            resp = session.post(f"{_GMAIL_API}/users/me/messages/batchModify", json=body)
            resp.raise_for_status()


def batch_trash_emails(session, message_ids: list) -> int:
    if not message_ids:
        return 0
    for i in range(0, len(message_ids), 1000):
        resp = session.post(
            f"{_GMAIL_API}/users/me/messages/batchModify",
            json={"ids": message_ids[i : i + 1000], "addLabelIds": [LABEL_TRASH], "removeLabelIds": [LABEL_INBOX]},
        )
        resp.raise_for_status()
    return len(message_ids)


def _b64_slice(data: str, max_chars: int) -> str:
    """Slice a URL-safe base64 string to decode at most ~max_chars bytes, with correct padding."""
    limit = max_chars * 4 // 3 + 4
    data = data.rstrip("=")[:limit]
    # A leftover of 1 char (mod 4) is always invalid base64; trim it to 0.
    # Leftovers of 2 or 3 are valid partial groups — add the required padding.
    if len(data) % 4 == 1:
        data = data[:-1]
    data += "=" * (-len(data) % 4)
    return data


def _extract_body(payload, max_chars: int = 0) -> str:
    if "parts" not in payload:
        data = payload.get("body", {}).get("data", "")
        if not data:
            return ""
        if max_chars:
            data = _b64_slice(data, max_chars)
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    # Single pass: collect plain/html data references (prefer plain, decode lazily)
    plain_data = html_data = ""
    for part in payload["parts"]:
        if part["mimeType"] == "text/plain" and not plain_data:
            plain_data = part["body"].get("data", "")
        elif part["mimeType"] == "text/html" and not html_data:
            html_data = part["body"].get("data", "")
    if plain_data:
        if max_chars:
            plain_data = _b64_slice(plain_data, max_chars)
        return base64.urlsafe_b64decode(plain_data).decode("utf-8", errors="ignore")
    if html_data:
        if max_chars:
            # HTML is tag-heavy; use a larger multiplier to preserve enough text after stripping
            html_data = _b64_slice(html_data, max_chars * 10)
        html = base64.urlsafe_b64decode(html_data).decode("utf-8", errors="ignore")
        return _html_to_text(html)
    # Recurse into nested parts (e.g., multipart/alternative within multipart/mixed)
    for part in payload["parts"]:
        result = _extract_body(part, max_chars)
        if result:
            return result
    return ""
