"""Think-tank interview: fixed question templates and the measurability lint.

The engine emits questions; it never invents them (a structured taxonomy beats
free-form LLM interviewing). ``next_questions`` may interpolate the CURRENT
value of a slot into a fixed template, nothing more. The session conducts one
question at a time and re-runs ``refine`` with the accumulated answers.
"""
from __future__ import annotations

import re

from .guardrails import DEFAULT_VERIFY_PREFIXES

SLOTS: tuple[str, ...] = (
    "purpose", "users", "constraints", "success_metrics", "anti_goals", "risks",
)

# Fixed, per-slot question text — multiple-choice biased; five-whys phrased as
# what/how (never "why", which invites justification instead of information).
QUESTION_TEMPLATES: dict = {
    "purpose": {
        "question": ("What job does this do, and what outcome does it produce? "
                     "Complete: 'When ___ happens, I want this to ___ so that ___.'"),
        "hint": "One sentence; the job, not the implementation.",
    },
    "users": {
        "question": ("Who uses it? (a) just me, (b) my team, (c) external users, "
                     "(d) other programs/machines. Pick all that apply and name them."),
        "hint": "Letters plus names, e.g. 'a — me, on my laptop'.",
    },
    "constraints": {
        "question": ("What hard constraints apply? (a) offline/local only, "
                     "(b) specific language or stack, (c) cost ceiling, (d) deadline, "
                     "(e) must integrate with an existing system, (f) none. "
                     "Pick all that apply and name specifics."),
        "hint": "Constraints the build MUST respect, not preferences.",
    },
    "success_metrics": {
        "question": ("Name 1-3 measurable success metrics. Each needs a runnable "
                     "check or a number, e.g. 'python3 -m pytest -q passes', "
                     "'p95 < 200ms', 'handles 10000 rows in < 5 s'."),
        "hint": "These become the loop's verifier targets.",
    },
    "anti_goals": {
        "question": ("What should this explicitly NOT do or NOT become? "
                     "Name the scope creep you want blocked."),
        "hint": "Non-goals; e.g. 'no web UI, no multi-user, no cloud'.",
    },
    "risks": {
        "question": ("What could go wrong, or what do you know least about? "
                     "(a) unclear requirements, (b) new technology, (c) integration "
                     "unknowns, (d) data/security exposure, (e) other — name it."),
        "hint": "The thing you'd want a spike or research pass on first.",
    },
}

# A numeric threshold: comparator+number, or number+unit/percent.
_NUMERIC_THRESHOLD = re.compile(
    r"(?:<=|>=|==|<|>|≤|≥)\s*\d"                    # comparator then number
    r"|\d[\d,.]*\s*(?:%|ms|s\b|sec|seconds|minutes|MB|GB|KB|req|rows|items|users|x\b|k\b)",
    re.IGNORECASE,
)


def lint_done_when(done_when: str) -> tuple[bool, str]:
    """Deterministically check that *done_when* is measurable.

    Passes iff the text contains an allowlisted runnable command prefix (from
    ``guardrails.DEFAULT_VERIFY_PREFIXES``) or a numeric threshold (comparator
    + number, or number + unit). Kills "feels fast".

    Args:
        done_when: The completion criterion to lint.

    Returns:
        ``(ok, reason)`` — ``reason`` is empty on success, otherwise explains
        what a measurable criterion needs.
    """
    text = (done_when or "").strip()
    if text:
        lowered = text.lower()
        for prefix in DEFAULT_VERIFY_PREFIXES:
            if re.search(rf"\b{re.escape(prefix.lower())}\b", lowered):
                return True, ""
        if _NUMERIC_THRESHOLD.search(text):
            return True, ""
    return False, (
        "done_when is not measurable — include a runnable command "
        "(e.g. 'python3 -m pytest -q passes') or a numeric threshold "
        "(e.g. 'p95 < 200ms')"
    )


def slot_filled(spec, slot: str) -> bool:
    """A slot is filled when non-empty (success_metrics: a non-empty list)."""
    value = getattr(spec, slot, "")
    if slot == "success_metrics":
        return bool(value)
    return bool(value and str(value).strip())


def next_questions(spec) -> list:
    """Return the fixed questions still needed to complete *spec*.

    A failing done_when lint prepends a done_when question (interpolating the
    current, unmeasurable text into a fixed template). Then one question per
    unfilled, undeferred slot in :data:`SLOTS` order. Filled or explicitly
    deferred slots are never asked about.

    Args:
        spec: An ``IdeaSpec`` (duck-typed; needs the slot attributes and
            ``deferred``/``done_when``).

    Returns:
        A list of ``{"slot", "question", "hint"}`` dicts (empty when the
        interview is complete).
    """
    questions: list = []
    ok, _ = lint_done_when(getattr(spec, "done_when", ""))
    if not ok:
        current = (getattr(spec, "done_when", "") or "").strip() or "(empty)"
        questions.append({
            "slot": "done_when",
            "question": (f"'{current}' is not checkable. Restate it as a runnable "
                         "command (e.g. 'python3 -m pytest -q passes') or a numeric "
                         "threshold (e.g. 'p95 < 200ms')."),
            "hint": "The loop cannot converge on a wish.",
        })
    deferred = set(getattr(spec, "deferred", []) or [])
    for slot in SLOTS:
        if slot in deferred or slot_filled(spec, slot):
            continue
        entry = QUESTION_TEMPLATES[slot]
        questions.append({"slot": slot, "question": entry["question"],
                          "hint": entry["hint"]})
    return questions
