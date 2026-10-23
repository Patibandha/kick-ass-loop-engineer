"""Negative tests for workspace isolation, written for Windows first.

Every case asserts a DEFINITE refusal on every host: the path is rejected by ``WritePolicy.check_path``
and by ``Workspace.apply`` (nothing written, a reason recorded), never an "either way" outcome. The
inputs are hostile spellings a builder could emit: backslash and mixed separators, drive letters in
either case, another drive, root-relative paths, ``..``, ``\\\\?\\`` and UNC prefixes, and
``file:stream`` alternate data streams (which would otherwise slip past the protected-name patterns).

A junction needs no privilege on Windows, so it runs in the main marker. Creating a symlink may need
privilege there, so that case lives under its OWN marker (``guardrail_isolation_capability``) and reports
"capability missing" only in that labelled job, never as a skip in the ``guardrail_isolation`` run.
"""

import os
import subprocess

import pytest

from kickass_loop_engineer.guardrails import WritePolicy
from kickass_loop_engineer.workspace import Workspace

isolation = pytest.mark.guardrail_isolation

REFUSED_PATHS = [
    # backslash and mixed separators
    ("backslash-parent", "..\\evil.txt"),
    ("backslash-deep-parent", "sub\\..\\..\\evil.txt"),
    ("mixed-parent-a", "sub/..\\..\\evil.txt"),
    ("mixed-parent-b", "sub\\../../evil.txt"),
    ("backslash-into-git", "sub\\..\\.git\\config"),
    ("mixed-into-env", "sub/..\\.env"),
    # drive letters, either case, with either separator, and drive-relative
    ("drive-upper-backslash", "C:\\Windows\\evil.txt"),
    ("drive-lower-backslash", "c:\\evil.txt"),
    ("drive-upper-slash", "C:/evil.txt"),
    ("drive-lower-slash", "c:/evil.txt"),
    ("drive-relative-upper", "C:evil.txt"),
    ("drive-relative-lower", "d:evil.txt"),
    # a different drive from wherever the workspace lives
    ("other-drive-z", "Z:\\evil.txt"),
    ("other-drive-a", "A:\\evil.txt"),
    ("other-drive-b-slash", "b:/evil.txt"),
    ("other-drive-e-relative", "E:evil.txt"),
    # root-relative
    ("root-relative-backslash", "\\foo"),
    ("root-relative-backslash-deep", "\\foo\\bar.txt"),
    ("root-relative-slash", "/foo"),
    ("root-relative-slash-deep", "/foo/bar.txt"),
    # parent traversal
    ("dotdot", ".."),
    ("dotdot-child", "../evil.txt"),
    ("dotdot-nested", "a/../../evil.txt"),
    ("dotdot-backslash", "..\\"),
    ("dot", "."),
    # extended-length, device and UNC prefixes
    ("extended-drive", "\\\\?\\C:\\evil.txt"),
    ("extended-drive-slash", "//?/C:/evil.txt"),
    ("extended-unc", "\\\\?\\UNC\\server\\share\\evil.txt"),
    ("device-namespace", "\\\\.\\C:\\evil.txt"),
    ("unc-backslash", "\\\\server\\share\\evil.txt"),
    ("unc-slash", "//server/share/evil.txt"),
    # alternate data streams: a stream on a protected name must not slip past the patterns
    ("ads-plain", "notes.txt:hidden"),
    ("ads-env", ".env:hidden"),
    ("ads-git", ".git:hidden"),
    ("ads-nested", "src/main.py:evil"),
    ("ads-data-default", "notes.txt::$DATA"),
    ("ads-on-directory", "src:hidden"),
]

_PATHS = [path for _, path in REFUSED_PATHS]
_IDS = [case_id for case_id, _ in REFUSED_PATHS]


def _file_block(path: str, body: str = "payload\n") -> str:
    """One builder FILE block for *path*."""
    return f"=== FILE: {path} ===\n{body}\n=== END FILE ==="


def _tree(root) -> list:
    """Every file path under *root*, relative and sorted (empty directories ignored)."""
    found = []
    for folder, _dirs, files in os.walk(root):
        for name in files:
            found.append(os.path.relpath(os.path.join(folder, name), root))
    return sorted(found)


@pytest.fixture
def layout(tmp_path):
    """``(workspace root, outside dir)``: the workspace is one folder in a larger scratch tree."""
    root = tmp_path / "ws"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    return root, outside


@isolation
@pytest.mark.parametrize("path", _PATHS, ids=_IDS)
def test_check_path_refuses_hostile_spelling(path):
    ok, reason = WritePolicy().check_path(path)
    assert ok is False, f"{path!r} must be refused"
    assert reason


@isolation
@pytest.mark.parametrize("path", _PATHS, ids=_IDS)
def test_apply_refuses_hostile_spelling_and_writes_nothing(layout, path):
    root, outside = layout
    workspace = Workspace(root=str(root))

    outcome = workspace.apply(_file_block(path))

    assert outcome.written == []
    assert [rejected for rejected, _ in outcome.rejected] == [path]
    assert outcome.rejected[0][1]
    assert workspace.written == set()
    assert _tree(root) == []
    assert _tree(outside) == []


@isolation
def test_ads_on_protected_name_leaves_the_protected_file_alone(layout):
    root, _outside = layout
    (root / ".env").write_text("KEEP=1\n", encoding="utf-8")
    workspace = Workspace(root=str(root))

    outcome = workspace.apply(_file_block(".env:hidden", "TOKEN=leak\n"))

    assert outcome.written == []
    assert outcome.rejected
    assert (root / ".env").read_text(encoding="utf-8") == "KEEP=1\n"
    assert not os.path.exists(str(root / ".env") + ":hidden")


@isolation
def test_a_hostile_path_does_not_stop_a_clean_sibling_block(layout):
    root, _outside = layout
    workspace = Workspace(root=str(root))
    text = _file_block("..\\evil.txt") + "\n" + _file_block("ok/fine.txt", "fine\n")

    outcome = workspace.apply(text)

    assert outcome.written == ["ok/fine.txt"]
    assert [path for path, _ in outcome.rejected] == ["..\\evil.txt"]
    assert _tree(root) == [os.path.join("ok", "fine.txt")]


@isolation
@pytest.mark.parametrize("path", ["a.py", "pkg/mod.py", "pkg\\mod.py", "pkg/sub/../mod.py",
                                  "tests/test_x.py"])
def test_clean_relative_paths_are_still_accepted(path):
    ok, reason = WritePolicy().check_path(path)
    assert ok, reason


def _make_directory_link(link, target) -> None:
    """Create a directory junction on Windows (no privilege needed), a symlink elsewhere.

    Raises:
        subprocess.CalledProcessError: When ``mklink /J`` fails (a real failure, not a missing capability).
    """
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True, text=True)
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


@isolation
def test_write_through_a_directory_junction_is_refused(layout):
    root, outside = layout
    _make_directory_link(root / "link", outside)
    workspace = Workspace(root=str(root))

    outcome = workspace.apply(_file_block("link/evil.txt"))

    assert outcome.written == []
    assert [path for path, _ in outcome.rejected] == ["link/evil.txt"]
    assert _tree(outside) == []


@isolation
def test_write_through_a_nested_junction_with_backslashes_is_refused(layout):
    root, outside = layout
    (root / "pkg").mkdir()
    _make_directory_link(root / "pkg" / "link", outside)
    workspace = Workspace(root=str(root))

    outcome = workspace.apply(_file_block("pkg\\link\\deep\\evil.txt"))

    assert outcome.written == []
    assert outcome.rejected
    assert _tree(outside) == []


@pytest.mark.guardrail_isolation_capability
def test_write_through_a_symlink_is_refused_where_symlinks_can_be_made(layout):
    """Capability job only: symlink creation may need privilege on Windows."""
    root, outside = layout
    try:
        os.symlink(str(outside), str(root / "link"), target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"capability missing: cannot create symlinks here ({exc})")
    workspace = Workspace(root=str(root))

    outcome = workspace.apply(_file_block("link/evil.txt"))

    assert outcome.written == []
    assert outcome.rejected
    assert _tree(outside) == []
