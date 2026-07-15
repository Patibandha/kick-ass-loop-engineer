#!/usr/bin/env python3
"""Record a REAL proof-of-test run and render it as a terminal-style demo GIF.

This is not a hand-drawn animation. The script:

1. Builds a throwaway git repo with a COMMITTED baseline — a rate-limiter stub
   whose two tests FAIL — plus an UNCOMMITTED edit that makes them pass.
   ``prove()`` needs an uncommitted change to revert (verifier.py); a clean tree
   would abort the proof.
2. Runs the REAL CLI: ``loop-engineer verify --gate unit
   --cmd "python3 -m pytest -q" --prove`` against that repo and captures the JSON
   evidence record. The rendered frames are grounded in that record's actual
   ``green_before / red_when_reverted / green_after / proven`` booleans — if the
   real run does not prove, the script fails loudly instead of drawing a fake.
3. Renders the captured cycle as dark-theme terminal frames with Pillow and writes
   ``assets/proof-of-test-demo.gif`` (<800KB). Each frame carries a unique marker
   pixel so Pillow never coalesces identical "hold" frames (the house pattern from
   ``linkedin/make_visuals.py``); ``duration`` governs timing.

Re-runnable: ``python3 scripts/make_demo_gif.py``. Pillow is imported here only —
it is NOT a package dependency (this script lives outside ``src/`` and is never
imported by the engine).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

# --- paths ----------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
_OUT = os.path.join(_REPO_ROOT, "assets", "proof-of-test-demo.gif")
_MAX_BYTES = 800 * 1024

# --- terminal theme -------------------------------------------------------
BG = (11, 14, 20)          # window background
BAR = (21, 27, 40)         # title bar
EDGE = (40, 52, 74)
INK = (230, 233, 240)
MUTED = (138, 147, 166)
CYAN = (111, 168, 255)
GREEN = (53, 214, 164)
RED = (255, 92, 108)
AMBER = (255, 122, 69)
DIM = (74, 84, 104)

_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
_MONO_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

W, H = 760, 470
MARGIN = 30
LINE_H = 30
FS = 18


def _font(bold: bool = False, size: int = FS) -> ImageFont.FreeTypeFont:
    path = _MONO_BOLD if bold else _MONO
    try:
        return ImageFont.truetype(path, size)
    except OSError:  # pragma: no cover - font is present on the build box
        return ImageFont.load_default()


# --- 1. record a real proof-of-test run -----------------------------------
def record_proof() -> dict:
    """Build the toy repo, run the real verify --prove, return its JSON record."""
    workdir = tempfile.mkdtemp(prefix="loop-engineer-demo-")
    try:
        _write(workdir, ".gitignore", "__pycache__/\n.pytest_cache/\n")
        _write(workdir, "rate_limiter.py", _STUB_IMPL)
        _write(workdir, "test_rate_limiter.py", _TESTS)
        _git(workdir, "init", "-q")
        _git(workdir, "add", ".gitignore", "rate_limiter.py", "test_rate_limiter.py")
        _git(workdir, "-c", "user.email=demo@example.com", "-c", "user.name=demo",
             "commit", "-q", "-m", "baseline: rate limiter stub + tests")
        # The uncommitted change: the real implementation the tests demand.
        _write(workdir, "rate_limiter.py", _REAL_IMPL)

        env = dict(os.environ, PYTHONPATH=_SRC)
        proc = subprocess.run(
            [sys.executable, "-m", "kickass_loop_engineer.cli", "verify",
             "--gate", "unit", "--cmd", "python3 -m pytest -q", "--prove",
             "--workspace", workdir],
            capture_output=True, text=True, env=env, cwd=_REPO_ROOT, timeout=180)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise SystemExit(
                f"verify --prove did not run cleanly (exit {proc.returncode}):\n"
                f"{proc.stderr}")
        record = json.loads(proc.stdout)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    proof = record.get("proof") or {}
    if not proof.get("proven"):
        raise SystemExit(
            "the real proof-of-test run did NOT prove — refusing to render a "
            f"misleading GIF. record={json.dumps(record)}")
    return record


_STUB_IMPL = (
    "def allow(count, limit):\n"
    '    """Allow a request only when the running count is under the limit."""\n'
    "    return True  # stub: always allows — the tests must catch this\n"
)
_REAL_IMPL = (
    "def allow(count, limit):\n"
    '    """Allow a request only when the running count is under the limit."""\n'
    "    return count < limit\n"
)
_TESTS = (
    "from rate_limiter import allow\n\n\n"
    "def test_under_limit_is_allowed():\n"
    "    assert allow(2, 5) is True\n\n\n"
    "def test_over_limit_is_blocked():\n"
    "    assert allow(9, 5) is False\n"
)


def _write(root: str, name: str, body: str) -> None:
    with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
        handle.write(body)


def _git(cwd: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   text=True, timeout=60)


# --- 2. build the terminal script from the REAL record --------------------
def build_lines(record: dict) -> list:
    """Return ``(segments, hold)`` rows narrating the real proof record.

    ``segments`` is a list of ``(text, color, bold)`` chunks on one line; ``hold``
    is how many extra frames to linger after the line lands.
    """
    proof = record["proof"]
    passed = record["passed"]

    def mark(ok: bool) -> tuple:
        return ("[PASS]" if ok else "[FAIL]", GREEN if ok else RED, True)

    rows = [
        ([("$ ", DIM, False),
          ("loop-engineer verify --gate unit \\", INK, False)], 1),
        ([("      --cmd ", INK, False),
          ('"python3 -m pytest -q"', CYAN, False),
          (" --prove", AMBER, False)], 3),
        ([("", INK, False)], 0),
        ([("  1/3  gate is green with the change", MUTED, False),
          ("   ", INK, False), mark(proof["green_before"])], 3),
        ([("  2/3  revert the change ", MUTED, False),
          ("->", AMBER, False),
          (" it MUST go red", MUTED, False),
          ("  ", INK, False), mark(proof["red_when_reverted"])], 3),
        ([("  3/3  restore ", MUTED, False),
          ("->", GREEN, False),
          (" green again", MUTED, False),
          ("       ", INK, False), mark(proof["green_after"])], 3),
        ([("", INK, False)], 0),
        ([("  PROVEN ", GREEN if proof["proven"] else RED, True),
          ("the test is real, not a rubber stamp", INK, False)], 2),
        ([('  {"passed": ', DIM, False),
          ("true" if passed else "false", GREEN if passed else RED, False),
          (', "proven": ', DIM, False),
          ("true" if proof["proven"] else "false",
           GREEN if proof["proven"] else RED, False),
          ("}", DIM, False)], 8),
    ]
    return rows


# --- 3. render frames -----------------------------------------------------
def render_frame(rows_shown: list) -> Image.Image:
    """Draw one terminal frame with the given rows revealed."""
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    # window chrome
    draw.rectangle([0, 0, W - 1, 40], fill=BAR)
    draw.rectangle([0, 0, W - 1, H - 1], outline=EDGE)
    for i, color in enumerate((RED, AMBER, GREEN)):
        cx = 24 + i * 22
        draw.ellipse([cx - 6, 14, cx + 6, 26], fill=color)
    draw.text((W // 2, 20), "proof-of-test  ·  loop-engineer", font=_font(size=15),
              fill=MUTED, anchor="mm")
    # body
    y = 62
    for segments in rows_shown:
        x = MARGIN
        for text, color, bold in segments:
            if not text:
                continue
            draw.text((x, y), text, font=_font(bold=bold), fill=color)
            x += draw.textlength(text, font=_font(bold=bold))
        y += LINE_H
    return img


def make_gif() -> None:
    record = record_proof()
    rows = build_lines(record)

    frames = []
    # Reveal rows one at a time; linger per each row's hold count.
    for count in range(1, len(rows) + 1):
        shown = [segs for segs, _hold in rows[:count]]
        hold = rows[count - 1][1]
        for _ in range(1 + hold):
            frames.append(render_frame(shown))

    _save_gif(frames, _OUT, duration=180)


def _save_gif(frames: list, path: str, duration: int) -> None:
    """Write frames as a GIF, defeating Pillow's identical-frame coalescing.

    A unique marker pixel per frame (index encoded into corner (0,0)) keeps every
    hold frame distinct so ``duration`` alone governs timing.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    uniq = []
    for i, frame in enumerate(frames):
        frame = frame.copy()
        frame.putpixel((0, 0), (i & 255, (i >> 8) & 255, 20))
        uniq.append(frame)
    uniq[0].save(path, save_all=True, append_images=uniq[1:], duration=duration,
                 loop=0, optimize=True, disposal=1)
    size = os.path.getsize(path)
    print(f"wrote {path}  ({len(uniq)} frames, {size // 1024} KB)")
    if size > _MAX_BYTES:
        raise SystemExit(f"GIF is {size} bytes (> {_MAX_BYTES}); tighten the render")


if __name__ == "__main__":
    make_gif()
