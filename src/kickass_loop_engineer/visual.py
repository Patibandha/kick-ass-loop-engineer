"""Advisory VLM screenshot critique: findings only, never a gate.

After a slice's gates pass, an allowlist-validated screenshot command captures
the application's UI and a vision-capable provider critiques the image against
the slice objective. The spec is explicit that VLM visual judgment is
unreliable (~50% pairwise accuracy), so the critique NEVER gates: its findings
join the reviewer's advisory stream (oscillation detection + next-slice
feedback), and any failure anywhere — rejected command, missing screenshot,
provider error — degrades to a logged warning and zero findings.

Output-path convention: the screenshot command's LAST whitespace token names
the image it writes (relative paths resolve against the gated workspace), e.g.
``npx playwright-cli screenshot http://localhost:3000 shot.png`` -> shot.png.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from .agents import parse_findings
from .guardrails import VerificationPolicy

logger = logging.getLogger("kickass_loop_engineer.visual")

VISUAL_SYSTEM = (
    "You are a UI reviewer examining a screenshot of the application under "
    "development. Report concrete visual defects only: broken layout, "
    "overlapping or clipped elements, missing content, unreadable text or "
    "contrast, visible error messages. List each defect on its own line "
    "starting with exactly 'FINDING: '. If the screenshot looks correct, "
    "write exactly 'NO FINDINGS'. Your critique is advisory — executable "
    "gates decide pass/fail, never you."
)


def _output_path(screenshot_cmd: str, workspace: str) -> str:
    """Resolve the image path named by the command's last whitespace token.

    Args:
        screenshot_cmd: The screenshot command (non-empty by contract).
        workspace: Base directory for relative output paths.

    Returns:
        The absolute path the screenshot command is expected to have written.
    """
    token = screenshot_cmd.split()[-1]
    return token if os.path.isabs(token) else os.path.join(workspace, token)


def visual_critique(
    screenshot_cmd: str,
    workspace: str,
    policy: Optional[VerificationPolicy],
    provider,
    objective: str,
    on_usage: Optional[Callable] = None,
) -> list:
    """Screenshot the app and return the VLM's advisory findings; never raises.

    The screenshot command runs under the SAME :class:`VerificationPolicy`
    authority as gates (allowlist + metachar rejection + scrubbed env). The
    produced image — named by the command's last whitespace token — is sent to
    a vision-capable provider whose reply is parsed with the reviewer's
    ``FINDING:``/``NO FINDINGS`` format. Every failure mode (policy rejection,
    failed command, missing image, provider or callback error) logs a warning
    and yields no findings; the run is never affected.

    Args:
        screenshot_cmd: Allowlisted command that writes the screenshot; its
            LAST whitespace token is the output image path.
        workspace: The gated worktree the command runs in (and the base for a
            relative output path).
        policy: Verification policy; the default policy is used when ``None``.
        provider: A vision-capable provider — its ``complete`` must accept an
            ``images`` keyword (a text-only provider here is a config error,
            surfaced as a warning).
        objective: The slice objective the screenshot is critiqued against.
        on_usage: Optional callback receiving the raw ``ProviderResult`` so
            the caller can ledger the call's cost.

    Returns:
        The parsed advisory findings; empty on a clean screenshot or ANY error.
    """
    try:
        active = policy or VerificationPolicy()
        result = active.run(screenshot_cmd, cwd=workspace)
        if result.error or not result.passed:
            logger.warning(
                "visual critique skipped: screenshot command failed: %s (%s)",
                screenshot_cmd, result.error or "non-zero exit")
            return []
        image = _output_path(screenshot_cmd, workspace)
        if not os.path.isfile(image):
            logger.warning(
                "visual critique skipped: screenshot %s was not produced by %r "
                "(convention: the command's last token names the output image)",
                image, screenshot_cmd)
            return []
        brief = (
            f"Objective: {objective}\n\n"
            "Critique the attached UI screenshot against this objective."
        )
        response = provider.complete(VISUAL_SYSTEM, brief, images=(image,))
        if on_usage is not None:
            on_usage(response)
        return parse_findings(response.text)
    except Exception as exc:  # noqa: BLE001 - advisory only, must never fail the run
        logger.warning(
            "visual critique failed (advisory only, run continues): %s", exc)
        return []
