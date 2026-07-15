"""Task modes: build / enhance / fix / audit, with coarse auto-detection.

A run is either greenfield (``build``) or targets an existing codebase
(``enhance``/``fix``/``audit``). ``detect_mode`` resolves the requested mode
at run start: an explicit mode passes through unchanged, while ``auto`` uses
one deliberately coarse signal — the workspace has git-tracked files
(``.loop-engineer/`` run artifacts excluded) → ``enhance``, otherwise
``build``. ``fix`` and ``audit`` are never auto-detected; they must be
requested explicitly.
"""

from __future__ import annotations

from .repomap import tracked_files

MODE_AUTO = "auto"
MODE_BUILD = "build"
MODE_ENHANCE = "enhance"
MODE_FIX = "fix"
MODE_AUDIT = "audit"

#: The four concrete task modes a run can resolve to.
MODES = (MODE_BUILD, MODE_ENHANCE, MODE_FIX, MODE_AUDIT)
#: Everything a caller may request (the concrete modes plus ``auto``).
REQUESTABLE_MODES = (MODE_AUTO,) + MODES


def validate_mode(requested: str) -> str:
    """Return *requested* unchanged when it is a requestable mode.

    Args:
        requested: The mode string to validate.

    Returns:
        The validated mode.

    Raises:
        RuntimeError: When *requested* is not one of ``REQUESTABLE_MODES``;
            the message lists the valid modes.
    """
    if requested not in REQUESTABLE_MODES:
        raise RuntimeError(
            f"mode must be one of {', '.join(REQUESTABLE_MODES)} "
            f"(got {requested!r})")
    return requested


def detect_mode(workspace: str, requested: str = MODE_AUTO) -> str:
    """Resolve the task mode for a run against *workspace*.

    Auto-detection is coarse BY DESIGN: tracked files present → ``enhance``,
    else ``build``. A repo whose only tracked content lives under
    ``.loop-engineer/`` counts as empty (``repomap.tracked_files`` excludes
    run artifacts and soft-fails to ``[]`` for a non-repo).

    Args:
        workspace: The run workspace to inspect when *requested* is ``auto``.
        requested: A concrete mode (passed through) or ``auto`` (detected).

    Returns:
        One of ``MODES`` — never ``auto``, and auto-detection never returns
        ``fix`` or ``audit``.

    Raises:
        RuntimeError: When *requested* is not a requestable mode.
    """
    validate_mode(requested)
    if requested != MODE_AUTO:
        return requested
    return MODE_ENHANCE if tracked_files(workspace) else MODE_BUILD
