"""A gate never runs stale bytecode.

Python matches ``__pycache__`` entries by the source's mtime and size, so a same-size rewrite within one mtime
tick would import the old bytecode: the gate would grade code that is no longer there. Proof-of-test reverts and
restores files in quick succession, which is exactly that pattern.
"""

import os

from kickass_loop_engineer.gates import DEFAULT_GATE_CMD
from kickass_loop_engineer.guardrails import VerificationPolicy


def _write(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")


def test_a_same_size_rewrite_with_the_same_mtime_is_graded_on_the_new_code(tmp_path):
    module = tmp_path / "answer.py"
    _write(module, "def answer():\n    return 42\n")
    _write(tmp_path / "test_answer.py", "from answer import answer\n\n\ndef test_answer():\n    assert answer() == 42\n")
    stamp = os.stat(module).st_mtime_ns
    policy = VerificationPolicy()
    gate = f"{DEFAULT_GATE_CMD} -p no:cacheprovider"

    assert policy.run(gate, cwd=str(tmp_path)).passed

    _write(module, "def answer():\n    return 41\n")
    os.utime(module, ns=(stamp, stamp))

    assert not policy.run(gate, cwd=str(tmp_path)).passed


def test_the_gate_leaves_no_bytecode_cache_behind(tmp_path):
    _write(tmp_path / "answer.py", "def answer():\n    return 42\n")
    _write(tmp_path / "test_answer.py", "from answer import answer\n\n\ndef test_answer():\n    assert answer() == 42\n")

    assert VerificationPolicy().run(f"{DEFAULT_GATE_CMD} -p no:cacheprovider", cwd=str(tmp_path)).passed
    assert not (tmp_path / "__pycache__").exists()
