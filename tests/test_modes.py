# tests/test_modes.py
"""Tests for task-mode detection (build/enhance/fix/audit + coarse auto)."""
import subprocess

import pytest

from kickass_loop_engineer.modes import (
    MODE_AUDIT,
    MODE_AUTO,
    MODE_BUILD,
    MODE_ENHANCE,
    MODE_FIX,
    MODES,
    REQUESTABLE_MODES,
    detect_mode,
    validate_mode,
)


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _make_repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init")
    return repo


class TestAutoDetection:
    def test_auto_with_tracked_files_is_enhance(self, tmp_path):
        repo = _make_repo(tmp_path, {"app.py": "X = 1\n"})
        assert detect_mode(str(repo), "auto") == MODE_ENHANCE

    def test_auto_on_loop_engineer_only_repo_is_build(self, tmp_path):
        repo = _make_repo(tmp_path, {".loop-engineer/events.jsonl": "{}\n"})
        assert detect_mode(str(repo), "auto") == MODE_BUILD

    def test_auto_on_non_repo_is_build(self, tmp_path):
        assert detect_mode(str(tmp_path), "auto") == MODE_BUILD

    def test_auto_is_the_default_requested_mode(self, tmp_path):
        repo = _make_repo(tmp_path, {"app.py": "X = 1\n"})
        assert detect_mode(str(repo)) == MODE_ENHANCE

    def test_auto_never_returns_fix_or_audit(self, tmp_path):
        repo = _make_repo(tmp_path, {"app.py": "X = 1\n"})
        for workspace in (str(repo), str(tmp_path)):
            assert detect_mode(workspace, "auto") in (MODE_BUILD, MODE_ENHANCE)


class TestExplicitPassthrough:
    @pytest.mark.parametrize("mode", MODES)
    def test_explicit_mode_passes_through_on_a_tracked_repo(self, tmp_path, mode):
        repo = _make_repo(tmp_path, {"app.py": "X = 1\n"})
        assert detect_mode(str(repo), mode) == mode

    @pytest.mark.parametrize("mode", MODES)
    def test_explicit_mode_passes_through_on_a_non_repo(self, tmp_path, mode):
        assert detect_mode(str(tmp_path), mode) == mode


class TestValidation:
    def test_invalid_mode_raises_runtime_error_listing_modes(self, tmp_path):
        with pytest.raises(RuntimeError) as excinfo:
            detect_mode(str(tmp_path), "refactor")
        message = str(excinfo.value)
        assert "refactor" in message
        for valid in ("auto", "build", "enhance", "fix", "audit"):
            assert valid in message

    def test_validate_mode_returns_valid_values(self):
        for mode in REQUESTABLE_MODES:
            assert validate_mode(mode) == mode

    def test_validate_mode_rejects_invalid_value(self):
        with pytest.raises(RuntimeError) as excinfo:
            validate_mode("sometimes")
        assert "sometimes" in str(excinfo.value)

    def test_mode_constants_cover_the_four_task_modes(self):
        assert MODES == (MODE_BUILD, MODE_ENHANCE, MODE_FIX, MODE_AUDIT)
        assert REQUESTABLE_MODES == (MODE_AUTO,) + MODES
