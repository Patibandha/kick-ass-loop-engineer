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


def ensure_cross_model(
    builder_model: str,
    reviewer_model: str,
    builder_family: str = "",
    reviewer_family: str = "",
) -> tuple:
    """Resolve both families and fail unless they PROVABLY differ.

    The single enforcement point for the cross-model guard: the constructor
    (config-time fail-fast) and the per-call winner-identity check both funnel
    through here, so rejection semantics can never drift apart. A declared
    family overrides detection for that side; two ``"unknown"`` families are
    rejected because independence cannot be proven.

    Args:
        builder_model: Identifier of the model that built the work.
        reviewer_model: Identifier of the model that reviews it.
        builder_family: Optional declared builder family (overrides detection).
        reviewer_family: Optional declared reviewer family (overrides detection).

    Returns:
        ``(builder_family, reviewer_family)`` as resolved.

    Raises:
        CrossModelReviewError: When both sides resolve to the same family
            (including two ``"unknown"`` models).
    """
    bf = builder_family or model_family(builder_model)
    rf = reviewer_family or model_family(reviewer_model)
    if bf == rf:
        raise CrossModelReviewError(
            f"Builder ({builder_model!r}, family={bf!r}) and reviewer "
            f"({reviewer_model!r}, family={rf!r}) must come from different model "
            "families to guarantee adversarial independence. "
            "Choose a reviewer from a different family (e.g. kimi→qwen, qwen→claude). "
            "For third-party/unknown models, declare independence explicitly in "
            "config: builder: {family: <line>} / reviewer: {family: <different-line>}."
        )
    return bf, rf


# ---------------------------------------------------------------------------
# Cross-model reviewer
# ---------------------------------------------------------------------------


class CrossModelReviewer:
    """Wraps a :class:`~kickass_loop_engineer.agents.Reviewer` and enforces
    that the reviewer model comes from a different family than the builder model.

    The cross-model constraint is checked at construction time so mismatches
    fail fast, before any expensive model calls are made.

    Families are auto-detected from the model identifiers via :func:`model_family`;
    a declared family (``builder_family`` / ``reviewer_family``) takes precedence
    over detection for that side. This lets ANY third-party model act as builder
    or reviewer: declare which model line each belongs to and the independence
    check runs on the declared values. Rejection semantics are unchanged from
    2.0 — the error fires only when both families resolve equal (including both
    ``"unknown"``).

    Args:
        builder_model: Identifier of the model used to build (e.g. ``"kimi-k2.7-code:cloud"``).
        reviewer_model: Identifier of the model used to review (e.g. ``"qwen2.5"``).
        reviewer: A fully configured :class:`~kickass_loop_engineer.agents.Reviewer` backed
                  by *reviewer_model*.
        builder_family: Optional declared family for the builder; overrides
            detection when non-empty.
        reviewer_family: Optional declared family for the reviewer; overrides
            detection when non-empty.

    Raises:
        CrossModelReviewError: If the builder and reviewer families resolve to the
            same value (including two ``"unknown"`` models — we cannot prove they differ).

    Attributes:
        builder_family: The resolved family of the builder model (declared or detected).
        reviewer_family: The resolved family of the reviewer model (declared or detected).
        reviewer_model: The reviewer model identifier as configured.
        reviewer: The underlying reviewer agent.
    """

    def __init__(
        self,
        builder_model: str,
        reviewer_model: str,
        reviewer: Reviewer,
        builder_family: str = "",
        reviewer_family: str = "",
    ) -> None:
        bf, rf = ensure_cross_model(builder_model, reviewer_model,
                                    builder_family=builder_family,
                                    reviewer_family=reviewer_family)
        self.builder_family: str = bf
        self.reviewer_family: str = rf
        self.reviewer_model: str = reviewer_model
        self.reviewer: Reviewer = reviewer

    def check(self, provider, family: str = "") -> None:
        """Re-verify builder ≠ reviewer against a CONSTRUCTED provider object.

        The review-time invariant behind diverse ensembles: the winning
        attempt's provider object — not any config label or roster entry — is
        what carries the model identity that actually built the work, so it is
        compared against the reviewer family again just before the review call.
        Identity is read the same way the config-time check reads it:
        ``provider.model`` falling back to ``provider.name``.

        Args:
            provider: The winning attempt's constructed provider instance.
            family: Optional declared family for the winning attempt (the
                per-attempt ``family:`` knob); overrides detection.

        Raises:
            CrossModelReviewError: When the provider's family resolves equal to
                the reviewer's (including two ``"unknown"`` families).
        """
        model = getattr(provider, "model", None) or provider.name
        ensure_cross_model(str(model), self.reviewer_model,
                           builder_family=family,
                           reviewer_family=self.reviewer_family)

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
