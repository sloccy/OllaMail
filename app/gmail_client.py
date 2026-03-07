import os
import json
import base64
import time
import datetime
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from app.config import GMAIL_MAX_RESULTS, GMAIL_LOOKBACK_HOURS

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "/credentials/credentials.json")
REDIRECT_URI = "http://localhost"


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
    service = build("oauth2", "v2", credentials=creds)
    info = service.userinfo().get().execute()
    return info["email"]


def get_service(credentials_json: str):
    creds = Credentials.from_authorized_user_info(json.loads(credentials_json), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds), creds.to_json()


def get_or_create_label(service, label_name: str) -> str:
    result = service.users().labels().list(userId="me").execute()
    for label in result.get("labels", []):
        if label["name"].lower() == label_name.lower():
            return label["id"]
    created = service.users().labels().create(
        userId="me",
        body={
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    return created["id"]


def build_label_cache(service, label_names: list) -> dict:
    """Fetch the Gmail label list once, create any missing labels, return {name: id}."""
    result = service.users().labels().list(userId="me").execute()
    existing = {l["name"].lower(): l["id"] for l in result.get("labels", [])}
    cache = {}
    for name in label_names:
        if name.lower() in existing:
            cache[name] = existing[name.lower()]
        else:
            created = service.users().labels().create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            ).execute()
            cache[name] = created["id"]
    return cache


def fetch_recent_emails(service, max_results=GMAIL_MAX_RESULTS, lookback_hours=GMAIL_LOOKBACK_HOURS):
    after_ts = int(time.time() - lookback_hours * 3600)
    response = service.users().messages().list(
        userId="me", maxResults=max_results, q=f"in:inbox after:{after_ts}"
    ).execute()
    messages = response.get("messages", [])
    emails = []
    for msg in messages:
        full = service.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        body = _extract_body(full["payload"])
        emails.append({
            "id": msg["id"],
            "subject": headers.get("Subject", "(no subject)"),
            "sender": headers.get("From", "unknown"),
            "snippet": full.get("snippet", ""),
            "body": body[:3000],
        })
    return emails


def apply_label(service, message_id: str, label_id: str):
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id]},
    ).execute()


def archive_email(service, message_id: str):
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["INBOX"]},
    ).execute()


def spam_email(service, message_id: str):
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]},
    ).execute()


def trash_email(service, message_id: str):
    service.users().messages().trash(
        userId="me",
        id=message_id,
    ).execute()


def list_labels(service) -> list:
    result = service.users().labels().list(userId="me").execute()
    return sorted(
        [{"id": l["id"], "name": l["name"]} for l in result.get("labels", [])],
        key=lambda x: x["name"].lower(),
    )


def fetch_emails_older_than(service, days: int, label_name: str = None, excluded_labels: list = None) -> list:
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
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def mark_email_read(service, message_id: str):
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()


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
