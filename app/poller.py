import time
import threading
from app import db
from app.llm import get_provider
from app.services.email_processor import process_account
from app.services.retention import cleanup_retention
from app.config import POLL_INTERVAL

_stop_event = threading.Event()
_scan_lock = threading.Lock()
_status_lock = threading.Lock()
_thread = None
_status = {"running": False, "last_run": None, "next_run": None}


def get_status():
    with _status_lock:
        return dict(_status)


def _set_status(**kwargs):
    with _status_lock:
        _status.update(kwargs)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()


def stop():
    _stop_event.set()


def run_now():
    threading.Thread(target=_scan_all_accounts, daemon=True).start()


def _loop():
    _set_status(running=True)
    while not _stop_event.is_set():
        _scan_all_accounts()
        interval = int(db.get_setting("poll_interval", str(POLL_INTERVAL)))
        _set_status(next_run=time.time() + interval)
        _stop_event.wait(timeout=interval)
    _set_status(running=False)


def _scan_all_accounts():
    if not _scan_lock.acquire(blocking=False):
        db.add_log("INFO", "Scan already in progress, skipping.")
        return
    try:
        _run_scan()
    finally:
        _scan_lock.release()


def _run_scan():
    _set_status(last_run=time.time())
    db.trim_logs()
    accounts = [a for a in db.list_accounts() if a["active"]]

    if not accounts:
        db.add_log("INFO", "Poller ran: no active accounts configured.")
        return

    provider = get_provider()

    for account in accounts:
        prompts = [p for p in db.list_prompts(account_id=account["id"]) if p["active"]]
        if not prompts:
            db.add_log("INFO", f"[{account['email']}] No active prompts for this account.")
            continue
        db.add_log("INFO", f"Starting scan: [{account['email']}] with {len(prompts)} prompt(s).")
        try:
            service = process_account(account, prompts, provider)
            cleanup_retention(account, service)
        except Exception as e:
            db.add_log("ERROR", f"[{account['email']}] Scan failed: {e}")
