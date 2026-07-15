# tests/test_repomap.py
"""Tests for the repo orientation brief and relevant-file selection."""
import subprocess

from kickass_loop_engineer.repomap import (
    UPDATE_INSTRUCTION,
    assemble_context,
    repo_map,
    select_files,
    tracked_files,
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
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init")
    return repo


class TestTrackedFiles:
    def test_lists_committed_paths(self, tmp_path):
        repo = _make_repo(tmp_path, {"a.py": "X = 1\n",
                                     "docs/readme.md": "hi\n"})
        assert tracked_files(str(repo)) == ["a.py", "docs/readme.md"]

    def test_excludes_loop_engineer_dir(self, tmp_path):
        repo = _make_repo(tmp_path, {
            "a.py": "X = 1\n",
            ".loop-engineer/events.jsonl": "{}\n",
        })
        assert tracked_files(str(repo)) == ["a.py"]

    def test_non_repo_returns_empty_list(self, tmp_path):
        assert tracked_files(str(tmp_path)) == []


class TestRepoMap:
    def test_lists_python_signatures_without_bodies(self, tmp_path):
        repo = _make_repo(tmp_path, {"parser.py": (
            "class Parser:\n"
            "    def parse(self, text):\n"
            "        return text\n"
            "\n"
            "def helper(a, b):\n"
            "    return a + b\n"
        )})
        out = repo_map(str(repo))
        assert "parser.py" in out
        assert "class Parser" in out
        assert "def parse(self, text)" in out
        assert "def helper(a, b)" in out
        assert "return" not in out

    def test_lists_non_python_files_bare(self, tmp_path):
        repo = _make_repo(tmp_path, {"config.yaml": "key: value\n",
                                     "m.py": "def f():\n    pass\n"})
        out = repo_map(str(repo))
        assert "config.yaml" in out
        assert "key: value" not in out

    def test_lists_unparseable_python_bare(self, tmp_path):
        repo = _make_repo(tmp_path, {"broken.py": "def broken(:\n"})
        out = repo_map(str(repo))
        assert "broken.py" in out
        assert "def broken" not in out

    def test_ranks_central_modules_first(self, tmp_path):
        repo = _make_repo(tmp_path, {
            "core.py": "def run():\n    pass\n",
            "alpha.py": "import core\n",
            "beta.py": "import core\n",
        })
        lines = repo_map(str(repo)).splitlines()
        assert lines.index("core.py") < lines.index("alpha.py")
        assert lines.index("core.py") < lines.index("beta.py")

    def test_counts_absolute_and_relative_package_imports(self, tmp_path):
        repo = _make_repo(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/core.py": "def run():\n    pass\n",
            "pkg/one.py": "from pkg.core import run\n",
            "pkg/two.py": "from .core import run\n",
        })
        lines = repo_map(str(repo)).splitlines()
        assert lines.index("pkg/core.py") < lines.index("pkg/one.py")
        assert lines.index("pkg/core.py") < lines.index("pkg/two.py")

    def test_hard_caps_at_budget_with_truncation_marker(self, tmp_path):
        body = "".join(
            f"def function_number_{i}(argument_one, argument_two):\n"
            "    pass\n\n"
            for i in range(50)
        )
        repo = _make_repo(tmp_path, {"big.py": body})
        out = repo_map(str(repo), budget_chars=200)
        assert len(out) <= 200
        assert out.endswith("... [truncated]")

    def test_non_repo_returns_empty_string(self, tmp_path):
        assert repo_map(str(tmp_path)) == ""

    def test_repo_with_no_tracked_files_returns_empty_string(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        assert repo_map(str(repo)) == ""


class TestSelectFiles:
    def test_matches_path_keywords_and_returns_full_content(self, tmp_path):
        parser_src = ("class Parser:\n"
                      "    def parse(self, text):\n"
                      "        return text\n")
        repo = _make_repo(tmp_path, {"parser.py": parser_src,
                                     "unrelated.py": "X = 1\n"})
        result = select_files(str(repo), "improve the parser error messages")
        assert result == [("parser.py", parser_src)]

    def test_matches_python_symbol_keywords(self, tmp_path):
        engine_src = "def load_tokens(path):\n    return path\n"
        repo = _make_repo(tmp_path, {"engine.py": engine_src,
                                     "other.py": "Y = 2\n"})
        result = select_files(str(repo), "fix how tokens get counted")
        assert result == [("engine.py", engine_src)]

    def test_zero_score_files_are_not_selected(self, tmp_path):
        repo = _make_repo(tmp_path, {"alpha.py": "A = 1\n"})
        assert select_files(str(repo), "completely different objective") == []

    def test_orders_by_score_desc_then_path_asc(self, tmp_path):
        strong = ("def parser_one():\n    pass\n\n\n"
                  "def parser_two():\n    pass\n")
        weak_b = "def parser_only():\n    pass\n"
        weak_a = "def parser_solo():\n    pass\n"
        repo = _make_repo(tmp_path, {"zz_strong.py": strong,
                                     "b_weak.py": weak_b,
                                     "a_weak.py": weak_a})
        result = select_files(str(repo), "clean up the parser")
        assert [path for path, _ in result] == [
            "zz_strong.py", "a_weak.py", "b_weak.py"]

    def test_skips_overflowing_file_and_keeps_smaller_match(self, tmp_path):
        big = ("def parser_main(x):\n    return x\n\n\n"
               "def parser_extra(y):\n    return y\n"
               + "PADDING_VALUE = 'x'\n" * 200)
        small = "def parser_tiny():\n    pass\n"
        repo = _make_repo(tmp_path, {"big.py": big, "tiny.py": small})
        budget = len(small.encode("utf-8")) + 10
        result = select_files(str(repo), "refactor the parser",
                              budget_bytes=budget)
        assert result == [("tiny.py", small)]

    def test_skips_binary_files(self, tmp_path):
        repo = _make_repo(tmp_path, {"parser.bin": b"\x00\xffparser\x00",
                                     "parser.py": "P = 1\n"})
        result = select_files(str(repo), "the parser")
        assert result == [("parser.py", "P = 1\n")]

    def test_non_repo_returns_empty_list(self, tmp_path):
        assert select_files(str(tmp_path), "anything parser") == []


class TestAssembleContext:
    def test_map_and_files_render_with_the_update_instruction(self):
        result = assemble_context("foo.py\n  def bar():",
                                  [("old.py", "OLD = 1\n")])
        expected = ("REPO MAP:\nfoo.py\n  def bar():\n\n"
                    "EXISTING FILES:\n--- old.py ---\nOLD = 1\n\n\n"
                    + UPDATE_INSTRUCTION)
        assert result == expected

    def test_files_only_still_carries_the_instruction(self):
        result = assemble_context("", [("old.py", "OLD = 1\n")])
        assert result.startswith("EXISTING FILES:\n--- old.py ---\n")
        assert result.endswith(UPDATE_INSTRUCTION)
        assert "REPO MAP:" not in result

    def test_map_only_still_carries_the_instruction(self):
        result = assemble_context("foo.py", [])
        assert result == "REPO MAP:\nfoo.py\n\n" + UPDATE_INSTRUCTION

    def test_instruction_is_the_locked_sentence(self):
        assert UPDATE_INSTRUCTION == (
            "these files EXIST — emit the complete UPDATED file for any you "
            "change; do not drop existing behavior")

    def test_nothing_to_assemble_returns_empty_string(self):
        assert assemble_context("", []) == ""
