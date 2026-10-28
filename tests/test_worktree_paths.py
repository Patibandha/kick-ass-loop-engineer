"""Seeding and promotion carry every changed file, whatever its name and whatever the host's code page.

git reports paths in UTF-8. Decoding its status with the locale code page (cp1252 on a default Windows) turned a
non-ASCII name into one that does not exist, and the file was silently neither seeded nor promoted. The old line
parser also stripped spaces from names and read a rename's ``old -> new`` as one path.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from kickass_loop_engineer.worktree import WorktreeManager

SRC = str(Path(__file__).resolve().parents[1] / "src")
NAME = "café.py"


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=repo, check=True,
                   capture_output=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "existing.txt").write_text("original\n", encoding="utf-8")
    (repo / "old_name.py").write_text("RENAMED = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


CHILD = """
import json, sys
from pathlib import Path
from kickass_loop_engineer.worktree import WorktreeManager
repo, base, name = sys.argv[1], sys.argv[2], sys.argv[3]
(Path(repo) / ("seed_" + name)).write_text("SEEDED = 1\\n", encoding="utf-8")
mgr = WorktreeManager(repo, base_dir=base)
wt = mgr.create("attempt")
seeded = (Path(wt) / ("seed_" + name)).exists()
(Path(wt) / name).write_text("VALUE = 2\\n", encoding="utf-8")
promoted = mgr.promote(wt, repo)
mgr.remove(wt)
print(json.dumps({"seeded": seeded, "promoted": promoted, "encoding": sys.flags.utf8_mode}))
"""


def test_non_ascii_names_are_seeded_and_promoted_without_utf8_mode(tmp_path):
    repo = _make_repo(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUTF8"}
    env["PYTHONPATH"] = SRC
    done = subprocess.run([sys.executable, "-X", "utf8=0", "-c", CHILD, str(repo), str(tmp_path / "wts"), NAME],
                          env=env, capture_output=True, text=True, encoding="utf-8", check=True)

    result = json.loads(done.stdout.strip().splitlines()[-1])

    assert result["encoding"] == 0
    assert result["seeded"] is True
    assert NAME in result["promoted"]
    assert (repo / NAME).read_text(encoding="utf-8") == "VALUE = 2\n"


def test_names_with_spaces_and_renames_are_promoted_at_their_real_path(tmp_path):
    repo = _make_repo(tmp_path)
    mgr = WorktreeManager(str(repo), base_dir=str(tmp_path / "wts"))
    wt = Path(mgr.create("attempt"))
    (wt / " padded name.py").write_text("PADDED = 1\n", encoding="utf-8")
    _git(wt, "mv", "old_name.py", "new_name.py")

    promoted = mgr.promote(str(wt), str(repo))
    mgr.remove(str(wt))

    assert promoted == sorted([" padded name.py", "new_name.py"])
    assert (repo / " padded name.py").read_text(encoding="utf-8") == "PADDED = 1\n"
    assert (repo / "new_name.py").read_text(encoding="utf-8") == "RENAMED = 1\n"
    assert (repo / "old_name.py").exists()
