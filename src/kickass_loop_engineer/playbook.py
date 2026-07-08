"""Cross-run playbook governance for kickass_loop_engineer.

Lessons are untrusted until promoted after N independent successes.
STALE lessons are kept (never silently dropped) but are not promoted.
This design prevents premature canonicalization of uncertain practices
while preserving the full audit trail.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

# Default entry shape — successes start at 0, not stale.
_DEFAULT_ENTRY: dict = {"successes": 0, "stale": False}


class Playbook:
    """JSON-backed playbook that promotes lessons after N independent successes.

    Lessons accumulate success counts across runs. A lesson is only
    considered *promoted* (ready for reuse) once it has at least
    ``promote_after`` successes and has not been marked stale.

    Stale lessons are retained in the file for audit purposes but are
    excluded from promotion queries.

    Args:
        path: File path for the JSON backing store.
        promote_after: Number of successes required before a lesson is
            considered promoted. Defaults to 3.
    """

    def __init__(self, path: str, promote_after: int = 3) -> None:
        """Load (or initialize) the playbook from *path*.

        Missing or corrupt files are treated as an empty playbook; a
        warning is logged but no exception is raised.

        Args:
            path: Absolute or relative path to the JSON file.
            promote_after: Promotion threshold (successes required).
        """
        self.path = path
        self.promote_after = promote_after
        self._lessons: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load lessons from the JSON file, or start with an empty dict."""
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._lessons = data
            else:
                logger.warning(
                    "playbook: unexpected root type in %s, starting fresh", self.path
                )
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("playbook: could not load %s (%s), starting fresh", self.path, exc)

    def _save(self) -> None:
        """Persist lessons to the JSON file.

        Creates parent directories as needed. Logs a warning and returns
        without raising on I/O errors so a save failure never crashes
        a running agent.
        """
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self._lessons, fh, indent=2)
        except OSError as exc:
            logger.warning("playbook: could not save %s (%s)", self.path, exc)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def record_attempt(self, lesson: str, *, success: bool) -> None:
        """Record one attempt for *lesson*, incrementing successes on success.

        Failures are recorded implicitly (the entry is created if absent)
        but do not increment the success counter.

        Args:
            lesson: Identifier for the lesson or practice being evaluated.
            success: Whether this attempt was successful.
        """
        if lesson not in self._lessons:
            self._lessons[lesson] = dict(_DEFAULT_ENTRY)
        if success:
            self._lessons[lesson]["successes"] += 1
        self._save()

    def mark_stale(self, lesson: str) -> None:
        """Mark *lesson* as stale so it is no longer promoted.

        The entry is created if it does not exist. Stale entries are
        retained in the store for audit purposes and are visible via
        :meth:`is_stale`, but :meth:`is_promoted` will return False.

        Args:
            lesson: Identifier for the lesson to mark stale.
        """
        if lesson not in self._lessons:
            self._lessons[lesson] = dict(_DEFAULT_ENTRY)
        self._lessons[lesson]["stale"] = True
        self._save()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_promoted(self, lesson: str) -> bool:
        """Return True iff *lesson* has reached the promotion threshold and is not stale.

        Args:
            lesson: Identifier for the lesson to check.

        Returns:
            True when ``successes >= promote_after`` and ``stale`` is False.
        """
        entry = self._lessons.get(lesson)
        if entry is None:
            return False
        return (
            entry.get("successes", 0) >= self.promote_after
            and not entry.get("stale", False)
        )

    def is_stale(self, lesson: str) -> bool:
        """Return the stale flag for *lesson* (False if lesson is unknown).

        Args:
            lesson: Identifier for the lesson to check.

        Returns:
            The stale flag value, or False if the lesson has no entry.
        """
        entry = self._lessons.get(lesson)
        if entry is None:
            return False
        return bool(entry.get("stale", False))
