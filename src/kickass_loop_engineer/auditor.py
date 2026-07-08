"""Loop-auditor: classify a loop run as KEEP, PIVOT, RETIRE, or KILL.

The auditor is the antidote to "iterate forever". After each run it
measures two signals — hit-rate (how often the loop produced something
valuable) and waste-ratio (how much compute/cost was burned on rejected
outputs) — and returns a verdict:

KEEP
    The loop is earning its budget: high hit-rate, low waste. Continue
    without intervention.

PIVOT
    The loop is wasteful but salvageable: waste-ratio is high even though
    hit-rate is not catastrophically low. Change the strategy (prompt,
    model, verifier thresholds) before the next run.

RETIRE
    Low value but not actively harmful: hit-rate is poor yet waste is
    also low, so the loop is just quietly failing. Stop it and reconsider
    the objective rather than tweaking parameters.

KILL
    Failing AND wasteful: both hit-rate is below the kill threshold and
    waste-ratio is above the high-waste threshold. Shut down immediately.
"""

from __future__ import annotations


def classify_run(
    hit_rate: float,
    waste_ratio: float,
    *,
    keep_hit: float = 0.7,
    kill_hit: float = 0.2,
    high_waste: float = 0.6,
) -> str:
    """Return a verdict string for a completed loop run.

    Parameters
    ----------
    hit_rate:
        Fraction of loop iterations that produced an accepted output,
        in the range [0.0, 1.0].
    waste_ratio:
        Fraction of total work that was ultimately discarded or rejected,
        in the range [0.0, 1.0].
    keep_hit:
        Minimum hit-rate required for a KEEP verdict.  Default 0.7.
    kill_hit:
        Hit-rate below which the run is considered critically failing.
        Default 0.2.
    high_waste:
        Waste-ratio at or above which the run is considered wasteful.
        Default 0.6.

    Returns
    -------
    str
        One of ``"KILL"``, ``"PIVOT"``, ``"KEEP"``, or ``"RETIRE"``.
    """
    if hit_rate < kill_hit and waste_ratio >= high_waste:
        return "KILL"
    if waste_ratio >= high_waste:
        return "PIVOT"
    if hit_rate >= keep_hit:
        return "KEEP"
    return "RETIRE"
