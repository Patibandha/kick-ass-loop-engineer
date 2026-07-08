"""Cross-model reviewer that enforces builder ≠ reviewer model family.

Adversarial review is most effective when the reviewer cannot share the builder's
blind spots. Two models from the same family (e.g. Kimi-k2-code and Kimi-k2-thinking)
often have correlated failure modes — training data, alignment tuning, and RLHF
targets are too similar. By requiring the reviewer to come from a *different* family
(e.g. Kimi builds, Qwen reviews, Claude arbitrates) we get independent judgment and
catch bugs that same-family pairs systematically miss.

Usage::

    cmr = CrossModelReviewer("kimi-k2.7-code:cloud", "qwen2.5", reviewer)
    review = cmr.review(objective, snapshot)
    if review.findings:
        print(review.feedback)
"""

from __future__ import annotations

from .agents import Review, Reviewer
from .objective import Objective

# ---------------------------------------------------------------------------
# Family detection
# ---------------------------------------------------------------------------

_FAMILY_TOKENS = [
    ("kimi", "kimi"),
    ("qwen", "qwen"),
    ("llama", "llama"),
]


def model_family(model_name: str) -> str:
    """Return the model family for *model_name*, or ``"unknown"``.

    Comparison is case-insensitive. Detection order:

    1. ``"kimi"`` if the substring ``kimi`` is present.
    2. ``"qwen"`` if the substring ``qwen`` is present.
    3. ``"llama"`` if the substring ``llama`` is present.
    4. ``"claude"`` if ``claude`` or ``anthropic`` is present.
    5. ``"unknown"`` otherwise.

    First matching token wins; this assumes family tokens do not co-occur in a
    single identifier (true for real model names — kimi/qwen/llama/claude do not
    appear together).

    Args:
        model_name: The identifier string for a model (e.g. ``"kimi-k2.7-code:cloud"``).

    Returns:
        A lowercase family token such as ``"kimi"``, ``"qwen"``, ``"llama"``,
        ``"claude"``, or ``"unknown"``.

    Examples:
        >>> model_family("kimi-k2.7-code:cloud")
        'kimi'
        >>> model_family("anthropic/claude")
        'claude'
        >>> model_family("mystery-model")
        'unknown'
    """
    lowered = model_name.lower()
    for token, family in _FAMILY_TOKENS:
        if token in lowered:
            return family
    if "claude" in lowered or "anthropic" in lowered:
        return "claude"
    return "unknown"


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class CrossModelReviewError(Exception):
    """Raised when the builder and reviewer belong to the same model family.

    This is a hard guard: we cannot guarantee adversarial independence when the
    two models share training lineage. The caller must supply a reviewer from a
    different family, or (for ``"unknown"`` families) provide explicit model names
    that can be verified.
    """


# ---------------------------------------------------------------------------
# Cross-model reviewer
# ---------------------------------------------------------------------------


class CrossModelReviewer:
    """Wraps a :class:`~kickass_loop_engineer.agents.Reviewer` and enforces
    that the reviewer model comes from a different family than the builder model.

    The cross-model constraint is checked at construction time so mismatches
    fail fast, before any expensive model calls are made.

    Args:
        builder_model: Identifier of the model used to build (e.g. ``"kimi-k2.7-code:cloud"``).
        reviewer_model: Identifier of the model used to review (e.g. ``"qwen2.5"``).
        reviewer: A fully configured :class:`~kickass_loop_engineer.agents.Reviewer` backed
                  by *reviewer_model*.

    Raises:
        CrossModelReviewError: If *builder_model* and *reviewer_model* resolve to the
            same family (including two ``"unknown"`` models — we cannot prove they differ).

    Attributes:
        builder_family: The detected family of the builder model.
        reviewer_family: The detected family of the reviewer model.
        reviewer: The underlying reviewer agent.
    """

    def __init__(
        self,
        builder_model: str,
        reviewer_model: str,
        reviewer: Reviewer,
    ) -> None:
        bf = model_family(builder_model)
        rf = model_family(reviewer_model)
        if bf == rf:
            raise CrossModelReviewError(
                f"Builder ({builder_model!r}, family={bf!r}) and reviewer "
                f"({reviewer_model!r}, family={rf!r}) must come from different model "
                "families to guarantee adversarial independence. "
                "Choose a reviewer from a different family (e.g. kimi→qwen, qwen→claude)."
            )
        self.builder_family: str = bf
        self.reviewer_family: str = rf
        self.reviewer: Reviewer = reviewer

    def review(self, objective: Objective, snapshot: str) -> Review:
        """Run a cross-model adversarial review of *snapshot* against *objective*.

        Delegates to the wrapped :class:`~kickass_loop_engineer.agents.Reviewer`.
        The cross-model guarantee is that the reviewer's model family differs from
        the builder's, so their independent blind spots reduce the risk of correlated
        failures slipping through.

        Args:
            objective: The loop objective (goal + completion criteria).
            snapshot: A textual snapshot of the current workspace to review.

        Returns:
            A :class:`~kickass_loop_engineer.agents.Review` with concrete findings
            (empty when clean) and actionable feedback.
        """
        return self.reviewer.review(objective, snapshot)
