"""Alert sink (spec §17.3: "Slack webhook or similar"). Logs the first line to stderr; posts the
full text to ALERT_WEBHOOK_URL when set (Slack-compatible {"text": ...} body).

The repo is public, so its Actions logs are too: only the summary line goes to stderr. Detail
lines name matches and go to the webhook; missed windows are also in capture_log."""

from __future__ import annotations

import sys

import httpx

from adatta.config import Settings


def send(settings: Settings, text: str, http: httpx.Client | None = None) -> bool:
    print(f"ALERT {text.splitlines()[0]}", file=sys.stderr)
    if not settings.alert_webhook_url:
        return False
    client = http or httpx.Client(timeout=10)
    try:
        client.post(settings.alert_webhook_url, json={"text": text}).raise_for_status()
        return True
    except httpx.HTTPError as exc:  # an alert failure must not hide the original problem
        print(f"ALERT webhook failed: {type(exc).__name__}", file=sys.stderr)
        return False
