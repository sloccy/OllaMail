import os
import json
import base64
import time
import requests
import datetime
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from app.config import GMAIL_MAX_RESULTS, GMAIL_LOOKBACK_HOURS, EMAIL_BODY_TRUNCATION

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "/credentials/credentials.json")
REDIRECT_URI = "http://localhost"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

_session = requests.Session()


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
    resp = _session.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"},
    )
    resp.raise_for_status()
    return resp.json()["email"]


def get_service(credentials_json: str):
    """Load and refresh credentials. Returns (creds, refreshed_json)."""
    creds = Credentials.from_authorized_user_info(json.loads(credentials_json), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds, creds.to_json()


def _gmail_request(method, path, creds, **kwargs):
    """Make an authenticated Gmail API request."""
    headers = {"Authorization": f"Bearer {creds.token}"}
    resp = _session.request(method, f"{GMAIL_API}/{path}", headers=headers, **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.content else None


def get_or_create_label(creds, label_name: str) -> str:
    result = _gmail_request("GET", "labels", creds)
    for label in result.get("labels", []):
        if label["name"].lower() == label_name.lower():
            return label["id"]
    created = _gmail_request("POST", "labels", creds, json={
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    })
    return created["id"]


def build_label_cache(creds, label_names: list) -> dict:
    """Fetch the Gmail label list once, create any missing labels, return {name: id}."""
    result = _gmail_request("GET", "labels", creds)
    existing = {l["name"].lower(): l["id"] for l in result.get("labels", [])}
    cache = {}
    for name in label_names:
        if name.lower() in existing:
            cache[name] = existing[name.lower()]
        else:
            created = _gmail_request("POST", "labels", creds, json={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            })
            cache[name] = created["id"]
    return cache


def fetch_recent_emails(creds, max_results=GMAIL_MAX_RESULTS, lookback_hours=GMAIL_LOOKBACK_HOURS):
    after_ts = int(time.time() - lookback_hours * 3600)
    response = _gmail_request("GET", "messages", creds, params={
        "maxResults": max_results,
        "q": f"in:inbox after:{after_ts}",
    })
    messages = response.get("messages", [])
    emails = []
    for msg in messages:
        full = _gmail_request("GET", f"messages/{msg['id']}", creds, params={"format": "full"})
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        body = _extract_body(full["payload"])
        emails.append({
            "id": msg["id"],
            "subject": headers.get("Subject", "(no subject)"),
            "sender": headers.get("From", "unknown"),
            "snippet": full.get("snippet", ""),
            "body": body[:EMAIL_BODY_TRUNCATION],
        })
    return emails


def apply_label(creds, message_id: str, label_id: str):
    _gmail_request("POST", f"messages/{message_id}/modify", creds,
                   json={"addLabelIds": [label_id]})


def archive_email(creds, message_id: str):
    _gmail_request("POST", f"messages/{message_id}/modify", creds,
                   json={"removeLabelIds": ["INBOX"]})


def spam_email(creds, message_id: str):
    _gmail_request("POST", f"messages/{message_id}/modify", creds,
                   json={"addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]})


def trash_email(creds, message_id: str):
    _gmail_request("POST", f"messages/{message_id}/trash", creds)


def list_labels(creds) -> list:
    result = _gmail_request("GET", "labels", creds)
    return sorted(
        [{"id": l["id"], "name": l["name"]} for l in result.get("labels", [])],
        key=lambda x: x["name"].lower(),
    )


def fetch_emails_older_than(creds, days: int, label_name: str = None, excluded_labels: list = None) -> list:
    """Return message IDs older than `days` days, optionally filtered by label."""
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    query = f"before:{cutoff.strftime('%Y/%m/%d')}"
    if label_name:
        query += f" label:{label_name}"
    if excluded_labels:
        for lbl in excluded_labels:
            query += f" -label:{lbl}"
    ids = []
    page_token = None
    while True:
        params = {"q": query, "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        resp = _gmail_request("GET", "messages", creds, params=params)
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def mark_email_read(creds, message_id: str):
    _gmail_request("POST", f"messages/{message_id}/modify", creds,
                   json={"removeLabelIds": ["UNREAD"]})


def _extract_body(payload) -> str:
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain":
                data = part["body"].get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        for part in payload["parts"]:
            result = _extract_body(part)
            if result:
                return result
    else:
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return ""
