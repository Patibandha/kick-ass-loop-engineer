"""Gate confinement (PA-2): a gate writes only inside its working folder and a private temp folder.

On Windows every test asserts the confined behaviour; on hosts without a confinement mechanism the same test
asserts the plain, unconfined run and that it says so, so every test has a definite result everywhere.
"""

import os
import subprocess
import sys

import pytest

from kickass_loop_engineer import confine
from kickass_loop_engineer.gates import DEFAULT_GATE_CMD
from kickass_loop_engineer.guardrails import VerificationPolicy
from kickass_loop_engineer.verifier import run_gate

pytestmark = pytest.mark.guardrail_gate_confinement
PROBE = "import sys; open(sys.argv[1], 'w').write('x')"


def _run(tmp_path, target):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return confine.run_confined([sys.executable, "-c", PROBE, str(target)], cwd=str(work),
                                env=dict(os.environ), timeout=60)


def test_confinement_is_available_exactly_on_windows():
    assert confine.CONFINEMENT_AVAILABLE is (os.name == "nt")


def test_a_confined_gate_writes_inside_its_working_folder(tmp_path):
    done, confined = _run(tmp_path, tmp_path / "work" / "ok.txt")
    assert done.returncode == 0 and (tmp_path / "work" / "ok.txt").exists()
    assert confined is confine.CONFINEMENT_AVAILABLE


def test_a_confined_gate_cannot_write_outside(tmp_path):
    outside = tmp_path / "outside.txt"
    done, confined = _run(tmp_path, outside)
    if confine.CONFINEMENT_AVAILABLE:
        assert done.returncode != 0 and "PermissionError" in done.stderr and not outside.exists()
    else:
        assert done.returncode == 0 and confined is False


def test_the_run_state_folder_stays_out_of_reach(tmp_path):
    state = tmp_path / "work" / ".loop-engineer"
    state.mkdir(parents=True)
    done, _ = _run(tmp_path, state / "pipeline.json")
    assert (done.returncode != 0) is confine.CONFINEMENT_AVAILABLE


def test_the_working_folder_label_is_restored(tmp_path):
    _run(tmp_path, tmp_path / "work" / "ok.txt")
    if confine.CONFINEMENT_AVAILABLE:
        label = subprocess.run(["icacls", str(tmp_path / "work")], capture_output=True, text=True).stdout
        assert "Low Mandatory Level" not in label


def test_large_output_is_captured_in_full(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    done, _ = confine.run_confined([sys.executable, "-c", "print('y' * 200000)"], cwd=str(work),
                                   env=dict(os.environ), timeout=60)
    assert len(done.stdout.strip()) == 200000


def test_a_failed_launch_is_a_failed_gate_not_an_unconfined_run(tmp_path, monkeypatch):
    if not confine.CONFINEMENT_AVAILABLE:
        # No confinement step exists to fail here: the run is plainly unconfined.
        assert _run(tmp_path, tmp_path / "work" / "ok.txt")[1] is False
        return
    monkeypatch.setattr(confine, "_label", lambda *a, **k: (_ for _ in ()).throw(OSError("icacls failed")))
    with pytest.raises(OSError, match="icacls failed"):
        _run(tmp_path, tmp_path / "work" / "ok.txt")


def _repo(tmp_path, body):
    (tmp_path / "test_x.py").write_text(body, encoding="utf-8")
    return str(tmp_path)


def test_a_gate_run_reports_whether_it_was_confined(tmp_path):
    result = VerificationPolicy().run(f"{DEFAULT_GATE_CMD} -p no:cacheprovider",
                                      cwd=_repo(tmp_path, "def test_ok():\n    pass\n"))
    assert result.passed and result.confined is confine.CONFINEMENT_AVAILABLE


def test_unconfined_evidence_says_so(tmp_path):
    record = run_gate("unit", f"{DEFAULT_GATE_CMD} -p no:cacheprovider",
                      workspace=_repo(tmp_path, "def test_ok():\n    pass\n"))
    assert record.passed
    assert record.evidence.endswith("; unconfined") is (not confine.CONFINEMENT_AVAILABLE)


def test_a_confined_gate_that_hangs_times_out(tmp_path):
    result = VerificationPolicy(timeout_seconds=10).run(
        f"{DEFAULT_GATE_CMD} -p no:cacheprovider",
        cwd=_repo(tmp_path, "import time\n\ndef test_hang():\n    time.sleep(60)\n"))
    assert not result.passed and "timed out" in result.error


def test_a_confinement_failure_fails_the_gate_with_its_error(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise confine.ConfinementError("DuplicateTokenEx failed")

    monkeypatch.setattr(confine, "run_confined", broken)
    result = VerificationPolicy().run(f"{DEFAULT_GATE_CMD} -p no:cacheprovider",
                                      cwd=_repo(tmp_path, "def test_ok():\n    pass\n"))
    assert not result.passed and result.error == "confinement failed: DuplicateTokenEx failed"
