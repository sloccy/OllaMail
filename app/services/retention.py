from app import db, gmail_client


def cleanup_retention(account: dict, creds) -> None:
    """Trash emails that exceed per-label or global retention rules."""
    account_id = account["id"]
    email_addr = account["email"]
    try:
        retention = db.get_retention(account_id)
        trashed_ids = set()

        exempt_names = {e["label_name"].lower() for e in retention.get("exemptions", [])}

        for rule in retention["labels"]:
            if rule["label_name"].lower() in exempt_names:
                continue
            ids = gmail_client.fetch_emails_older_than(creds, rule["days"], rule["label_name"])
            newly_trashed = 0
            for msg_id in ids:
                if msg_id not in trashed_ids:
                    gmail_client.trash_email(creds, msg_id)
                    trashed_ids.add(msg_id)
                    newly_trashed += 1
            if newly_trashed:
                db.add_log(
                    "INFO",
                    f"[{email_addr}] Retention: trashed {newly_trashed} email(s) with label "
                    f"'{rule['label_name']}' older than {rule['days']} day(s).",
                )

        if retention["global_days"]:
            excluded = [rule["label_name"] for rule in retention["labels"]] + list(exempt_names)
            ids = gmail_client.fetch_emails_older_than(
                creds, retention["global_days"], excluded_labels=excluded
            )
            new_ids = [i for i in ids if i not in trashed_ids]
            for msg_id in new_ids:
                gmail_client.trash_email(creds, msg_id)
            if new_ids:
                db.add_log(
                    "INFO",
                    f"[{email_addr}] Retention: trashed {len(new_ids)} email(s) older than "
                    f"{retention['global_days']} day(s) (global rule).",
                )
    except Exception as e:
        db.add_log("ERROR", f"[{email_addr}] Retention cleanup failed: {e}")
