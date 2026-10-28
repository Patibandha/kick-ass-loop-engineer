"""Tests for ``scripts/check_mutations.py`` and the mutation patches under ``mutations/``.

The patches themselves are checked with ``git apply --check`` (so a patch that drifts from the code it
mutates fails here, quickly). The script's verdict logic is exercised on a tiny temporary git repository
holding one caught and one uncaught mutation. The real end-to-end run is ``python scripts/check_mutations.py``.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.mutation_check

ROOT = Path(__file__).resolve().parent.parent
MUTATIONS = ROOT / "mutations"
SCRIPT = ROOT / "scripts" / "check_mutations.py"


def _load_script():
    """Import the script by path: ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("check_mutations", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses looks its module up here
    spec.loader.exec_module(module)
    return module


cm = _load_script()


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write(repo, rel, text):
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _diff(repo):
    """The repo's uncommitted change as a patch, after which the change is undone."""
    patch = subprocess.run(["git", "-c", "core.autocrlf=false", "diff", "--no-color"], cwd=repo, check=True,
                           capture_output=True, text=True).stdout
    _git(repo, "checkout", "-q", "--", ".")
    return patch


def _save(folder, name, text):
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def tiny(tmp_path):
    """``(repo, caught_patch, uncaught_patch)``: a committed one-function project with a gate test."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "pytest.ini",
           "[pytest]\npythonpath = .\nmarkers =\n    guardrail_gate: gate\n"
           "    guardrail_isolation: isolation\n    guardrail_isolation_capability: capability\n"
           "    guardrail_proof: proof\n")
    _write(repo, "impl.py", "def check(value):\n    return value == 1\n\n\n# spare line\n")
    _write(repo, "test_impl.py",
           "import pytest\nfrom impl import check\n\n\n@pytest.mark.guardrail_gate\n"
           "def test_check():\n    assert check(1)\n    assert not check(2)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    _write(repo, "impl.py", "def check(value):\n    return True\n\n\n# spare line\n")
    caught = _diff(repo)
    _write(repo, "impl.py", "def check(value):\n    return value == 1\n\n\n# spare line, reworded\n")
    uncaught = _diff(repo)
    return repo, caught, uncaught


def test_every_mutation_patch_applies_to_head():
    patches = cm.find_patches(MUTATIONS)
    with cm.Scratch(str(ROOT), 120.0) as scratch:
        for patch in patches:
            checked = subprocess.run([*cm._GIT, "apply", "--check", str(patch)], cwd=scratch.path,
                                     capture_output=True, text=True)
            assert checked.returncode == 0, f"{patch.name} no longer applies: {checked.stderr.strip()}"


def test_the_required_families_have_a_patch():
    names = [p.name for p in cm.find_patches(MUTATIONS)]
    for family in ("gate-", "isolation-", "proof-", "proof-transition-"):
        assert any(n.startswith(family) for n in names), family


def test_every_patch_name_maps_to_a_marker():
    for patch in cm.find_patches(MUTATIONS):
        assert cm.marker_for(patch).startswith("guardrail_")


def test_the_longest_matching_family_picks_the_marker():
    assert cm.marker_for(Path("proof-transition-skip-freshness.patch")) == "guardrail_proof_transition"
    assert cm.marker_for(Path("proof-skip-revert.patch")) == "guardrail_proof"
    assert cm.marker_for(Path("proof-authority-x.patch")) == "guardrail_proof_authority"
    assert cm.marker_for(Path("proof-transitional.patch")) == "guardrail_proof"


def test_an_unknown_family_is_an_error():
    with pytest.raises(cm.MutationCheckError):
        cm.marker_for(Path("whatever-x.patch"))
    with pytest.raises(cm.MutationCheckError):
        cm.marker_for(Path("gate.patch"))


def test_a_caught_mutation_and_a_green_clean_run_exit_zero(tiny, tmp_path, capsys):
    repo, caught, _ = tiny
    _save(tmp_path / "muts", "gate-always-true.patch", caught)
    assert cm.main(["--repo", str(repo), "--mutations", str(tmp_path / "muts")]) == 0
    out = capsys.readouterr().out
    assert "[CAUGHT" in out and "[CLEAN-OK" in out


def test_an_uncaught_mutation_fails_the_run(tiny, tmp_path, capsys):
    repo, caught, uncaught = tiny
    _save(tmp_path / "muts", "gate-always-true.patch", caught)
    _save(tmp_path / "muts", "gate-comment-only.patch", uncaught)
    assert cm.main(["--repo", str(repo), "--mutations", str(tmp_path / "muts")]) == 1
    out = capsys.readouterr().out
    assert "[CAUGHT     ] gate-always-true.patch" in out
    assert "[SURVIVED   ] gate-comment-only.patch" in out


def test_a_patch_that_does_not_apply_is_a_failure_not_a_catch(tiny, tmp_path, capsys):
    repo, _, _ = tiny
    stale = ("diff --git a/impl.py b/impl.py\n--- a/impl.py\n+++ b/impl.py\n@@ -1,2 +1,2 @@\n"
             " def gone(value):\n-    return 1\n+    return 2\n")
    _save(tmp_path / "muts", "gate-stale.patch", stale)
    assert cm.main(["--repo", str(repo), "--mutations", str(tmp_path / "muts")]) == 1
    assert "[NOT-APPLIED" in capsys.readouterr().out


def test_a_red_clean_run_fails_even_when_every_mutation_is_caught(tiny, tmp_path, capsys):
    repo, caught, _ = tiny
    _write(repo, "test_impl.py",
           "import pytest\n\n\n@pytest.mark.guardrail_gate\ndef test_check():\n    assert False\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "break the clean tree")
    _save(tmp_path / "muts", "gate-always-true.patch", caught)
    assert cm.main(["--repo", str(repo), "--mutations", str(tmp_path / "muts")]) == 1
    assert "[CLEAN-FAIL" in capsys.readouterr().out


def test_a_marker_with_no_tests_is_not_a_catch(tiny, tmp_path, capsys):
    repo, caught, _ = tiny
    _save(tmp_path / "muts", "proof-always-true.patch", caught)  # no guardrail_proof tests in the tiny repo
    assert cm.main(["--repo", str(repo), "--mutations", str(tmp_path / "muts")]) == 1
    assert "[ERROR" in capsys.readouterr().out


def test_no_patches_is_a_usage_error(tiny, tmp_path, capsys):
    repo, _, _ = tiny
    (tmp_path / "empty").mkdir()
    assert cm.main(["--repo", str(repo), "--mutations", str(tmp_path / "empty")]) == 2
    assert cm.main(["--repo", str(repo), "--mutations", str(tmp_path / "missing")]) == 2


def test_no_worktree_is_left_behind(tiny, tmp_path):
    repo, caught, _ = tiny
    _save(tmp_path / "muts", "gate-always-true.patch", caught)
    cm.main(["--repo", str(repo), "--mutations", str(tmp_path / "muts")])
    listed = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo, check=True,
                            capture_output=True, text=True).stdout
    assert "mutcheck-" not in listed
