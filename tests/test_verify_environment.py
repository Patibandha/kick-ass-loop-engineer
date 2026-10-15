"""Gate commands run like they would in the user's shell, against the workspace's own source tree."""

import os
import sys

import pytest

from kickass_loop_engineer.guardrails import VerificationPolicy

POLICY = VerificationPolicy(allowed_prefixes=("python", "python -m", "probe-tool"))
#: shlex (POSIX rules) eats backslashes, so commands name the interpreter with forward slashes.
PYTHON = sys.executable.replace("\\", "/")


@pytest.mark.skipif(os.name != "nt", reason="Windows searches the parent program's folder before PATH")
def test_the_gate_program_is_found_on_path_before_the_engines_own_folder(tmp_path, monkeypatch):
    bin_dir = tmp_path / "venv-bin"
    bin_dir.mkdir()
    (bin_dir / "python.cmd").write_text("@echo project-venv-python\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    result = POLICY.run("python -m anything", str(tmp_path))
    assert result.passed and "project-venv-python" in result.stdout_tail


def test_a_src_layout_project_tests_its_own_source(tmp_path):
    package = tmp_path / "src" / "probe_pkg_le"
    package.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'probe'\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text("print('from-the-workspace-src')\n", encoding="utf-8")
    result = VerificationPolicy(allowed_prefixes=(f"{PYTHON} -m",)).run(
        f"{PYTHON} -m probe_pkg_le", str(tmp_path))
    assert result.passed and "from-the-workspace-src" in result.stdout_tail


def test_a_project_without_pyproject_gets_no_src_path(tmp_path, monkeypatch):
    package = tmp_path / "src" / "probe_pkg_le2"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text("print('should-not-import')\n", encoding="utf-8")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    result = VerificationPolicy(allowed_prefixes=(f"{PYTHON} -m",)).run(
        f"{PYTHON} -m probe_pkg_le2", str(tmp_path))
    assert not result.passed


def test_a_missing_program_is_still_reported_as_not_found(tmp_path):
    result = POLICY.run("probe-tool --version", str(tmp_path))
    assert not result.passed and result.error == "verification tool not found"
