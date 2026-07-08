# src/kickass_loop_engineer/notify.py
"""Best-effort webhook / shell-command notify hook.

Events are fire-and-forget: the notify functions never raise. A delivery failure
is logged and returns ``False``; the caller decides whether to escalate.
"""
from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("kickass_loop_engineer.notify")


def build_event(
    run_id: str,
    stage: str,
    event: str,
    level: str,
    payload: dict[str, Any],
    ts: str = "",
) -> dict[str, Any]:
    """Build a structured event dict matching the notify contract.

    Args:
        run_id: Unique identifier for the current loop run.
        stage: Loop stage name (e.g. ``"verify"``, ``"plan"``).
        event: Event type (e.g. ``"escalation_required"``, ``"terminal"``).
        level: Severity/routing level (``"info"``, ``"warn"``, ``"escalate"``).
        payload: Arbitrary key/value metadata.
        ts: ISO-8601 timestamp; auto-generated when omitted.

    Returns:
        Dict with keys: ``ts``, ``run_id``, ``stage``, ``event``, ``level``, ``payload``.
    """
    return {
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "stage": stage,
        "event": event,
        "level": level,
        "payload": payload,
    }


class Notifier:
    """Fire-and-forget event dispatcher.

    Precedence: webhook (if set) → command (if set) → no-op.

    Args:
        webhook: HTTP(S) URL to POST event JSON to.
        command: Shell command that receives event JSON on stdin.
        timeout: Seconds before the delivery attempt is abandoned.
    """

    def __init__(
        self,
        webhook: str | None = None,
        command: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.webhook = webhook
        self.command = command
        self.timeout = timeout

    def notify(self, event: dict[str, Any]) -> bool:
        """Dispatch *event* to the configured hook.

        Returns:
            ``True`` if a hook fired and succeeded; ``False`` otherwise.
        """
        if self.webhook:
            return self._send_webhook(event)
        if self.command:
            return self._send_command(event)
        return False

    def _send_webhook(self, event: dict[str, Any]) -> bool:
        """POST event JSON to the webhook URL."""
        body = json.dumps(event).encode()
        req = urllib.request.Request(
            self.webhook,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ok = 200 <= resp.status < 300
                if not ok:
                    logger.warning("webhook returned status %s", resp.status)
                return ok
        except (OSError, urllib.error.URLError) as exc:
            logger.warning("webhook delivery failed: %s", exc)
            return False

    def _send_command(self, event: dict[str, Any]) -> bool:
        """Pipe event JSON to the shell command on stdin."""
        try:
            result = subprocess.run(
                self.command,
                input=json.dumps(event),
                text=True,
                shell=True,
                timeout=self.timeout,
            )
            ok = result.returncode == 0
            if not ok:
                logger.warning("notify command exited %s", result.returncode)
            return ok
        except subprocess.SubprocessError as exc:
            logger.warning("notify command failed: %s", exc)
            return False
        except OSError as exc:
            logger.warning("notify command OS error: %s", exc)
            return False
