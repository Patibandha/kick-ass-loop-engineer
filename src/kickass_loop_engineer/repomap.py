"""Repo orientation brief + keyword-scored relevant-file selection.

Brownfield primitives: :func:`repo_map` renders a compact skeleton of the
tracked codebase (module paths with ``class``/``def`` signatures, ranked so
the most-imported modules come first) for model ORIENTATION, while
:func:`select_files` returns the FULL contents of the tracked files most
relevant to an objective, scored by simple keyword hits. Standard library
only — no embeddings, no third-party dependencies.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("kickass_loop_engineer.repomap")

TRUNCATION_MARKER = "... [truncated]"
DEFAULT_MAP_BUDGET_CHARS = 8000
DEFAULT_SELECT_BUDGET_BYTES = 24576
UPDATE_INSTRUCTION = ("these files EXIST — emit the complete UPDATED file for "
                      "any you change; do not drop existing behavior")

_ARTIFACT_DIR = ".loop-engineer"
_MIN_KEYWORD_LEN = 3
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_STOP_WORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "when",
    "then", "are", "was", "were", "will", "would", "should", "could", "can",
    "not", "all", "its", "has", "have", "had", "been", "being", "each",
    "also", "than", "them", "they", "there", "their", "our", "your", "you",
    "get", "make", "made", "add", "use", "used", "using", "new", "any",
})


def tracked_files(root: str) -> List[str]:
    """List git-tracked files under *root*, excluding run artifacts.

    Args:
        root: Directory to inspect (any directory inside a git work tree).

    Returns:
        Repo-relative paths from ``git ls-files`` (git's own deterministic
        order) with anything under ``.loop-engineer/`` removed. Empty list
        when *root* is not a git repo or git is unavailable.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True,
            text=True, check=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("repomap: git ls-files failed under %s: %s", root, exc)
        return []
    return [
        line for line in proc.stdout.splitlines()
        if line and line != _ARTIFACT_DIR
        and not line.startswith(_ARTIFACT_DIR + "/")
    ]


def _dotted_names(path: str) -> List[str]:
    """Dotted module names a tracked ``.py`` *path* can be imported as.

    ``pkg/mod.py`` yields ``pkg.mod``; ``pkg/__init__.py`` yields ``pkg``;
    ``src/``-layout files also yield the name with the ``src.`` prefix
    stripped. Non-Python paths yield nothing.
    """
    if not path.endswith(".py"):
        return []
    parts = path[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
        if not parts:
            return []
    names = [".".join(parts)]
    if parts[0] == "src" and len(parts) > 1:
        names.append(".".join(parts[1:]))
    return names


def _prefixes(dotted: str) -> List[str]:
    """Every dotted prefix of *dotted*, longest first."""
    parts = dotted.split(".")
    return [".".join(parts[:n]) for n in range(len(parts), 0, -1)]


def _resolve(candidates: List[str], known: Dict[str, str]) -> Optional[str]:
    """Return the tracked file for the first candidate found in *known*."""
    for candidate in candidates:
        if candidate in known:
            return known[candidate]
    return None


def _import_targets(tree: ast.AST, module: str, is_package: bool,
                    known: Dict[str, str]) -> Set[str]:
    """Tracked files that *tree* (module named *module*) imports."""
    anchor = module.split(".") if is_package else module.split(".")[:-1]
    targets: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve(_prefixes(alias.name), known)
                if target:
                    targets.add(target)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = node.level - 1
                if up > len(anchor):
                    continue
                base_parts = list(anchor[: len(anchor) - up])
                if node.module:
                    base_parts += node.module.split(".")
                base = ".".join(base_parts)
                if not base:
                    continue
            else:
                if not node.module:
                    continue
                base = node.module
            for alias in node.names:
                target = _resolve([f"{base}.{alias.name}"] + _prefixes(base),
                                  known)
                if target:
                    targets.add(target)
    return targets


def _parse_python(root: str, path: str) -> Optional[ast.AST]:
    """Parse a tracked Python file; ``None`` when unreadable/unparseable."""
    try:
        with open(f"{root}/{path}", "r", encoding="utf-8",
                  errors="replace") as handle:
            return ast.parse(handle.read(), filename=path)
    except (OSError, SyntaxError, ValueError) as exc:
        logger.debug("repomap: listing %s bare (parse failed: %s)", path, exc)
        return None


def _signatures(tree: ast.AST) -> List[str]:
    """Indented ``class``/``def`` signature lines for a parsed module."""
    def sig(node: ast.AST, indent: str) -> str:
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{indent}{prefix} {node.name}({ast.unparse(node.args)}):"

    lines: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(sig(node, "  "))
        elif isinstance(node, ast.ClassDef):
            lines.append(f"  class {node.name}:")
            lines.extend(
                sig(sub, "    ") for sub in node.body
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return lines


def repo_map(root: str, budget_chars: int = DEFAULT_MAP_BUDGET_CHARS) -> str:
    """Render an orientation brief of the tracked files under *root*.

    Python files are listed with their top-level and class-level signatures
    (bodies omitted); unparseable Python and non-Python files are listed
    bare. Modules imported by more other tracked modules come first
    (import in-degree, ties broken by path).

    Args:
        root: Git repo root (or any directory inside one).
        budget_chars: Hard cap on the returned text; when exceeded the text
            is cut and ``... [truncated]`` is appended within the cap.

    Returns:
        The rendered map, or ``""`` for a non-repo or a repo with no
        tracked files.
    """
    files = tracked_files(root)
    if not files:
        return ""

    known: Dict[str, str] = {}
    for path in files:
        for name in _dotted_names(path):
            known.setdefault(name, path)

    trees: Dict[str, Optional[ast.AST]] = {
        path: _parse_python(root, path)
        for path in files if path.endswith(".py")
    }

    importers: Dict[str, Set[str]] = {}
    for path, tree in trees.items():
        if tree is None:
            continue
        names = _dotted_names(path)
        module = names[0] if names else path
        is_package = path.endswith("/__init__.py") or path == "__init__.py"
        for target in _import_targets(tree, module, is_package, known):
            if target != path:
                importers.setdefault(target, set()).add(path)

    ranked = sorted(files, key=lambda p: (-len(importers.get(p, ())), p))
    lines: List[str] = []
    for path in ranked:
        lines.append(path)
        tree = trees.get(path)
        if tree is not None:
            lines.extend(_signatures(tree))

    text = "\n".join(lines)
    if len(text) > budget_chars:
        keep = budget_chars - len(TRUNCATION_MARKER) - 1
        if keep <= 0:
            return TRUNCATION_MARKER[:budget_chars]
        text = text[:keep].rstrip() + "\n" + TRUNCATION_MARKER
    return text


def assemble_context(map_text: str, files: List[Tuple[str, str]]) -> str:
    """Format the enhance-mode repo context handed to builder briefs.

    Combines an orientation map (:func:`repo_map`) and the full contents of
    the objective-relevant files (:func:`select_files`) into one text block,
    closed by the update instruction that tells the builder these files
    already exist and must be re-emitted whole when changed.

    Args:
        map_text: Rendered repo map (may be empty).
        files: ``(path, content)`` tuples of relevant existing files.

    Returns:
        The assembled context, or ``""`` when there is nothing to show
        (keeping the builder brief byte-identical to a context-free run).
    """
    sections: List[str] = []
    if map_text:
        sections.append(f"REPO MAP:\n{map_text}")
    blocks = [f"--- {path} ---\n{content}" for path, content in files]
    if blocks:
        sections.append("EXISTING FILES:\n" + "\n\n".join(blocks))
    if not sections:
        return ""
    sections.append(UPDATE_INSTRUCTION)
    return "\n\n".join(sections)


def _keywords(objective_text: str) -> Set[str]:
    """Lowercased keyword set from *objective_text* (stop words dropped)."""
    tokens = _TOKEN_SPLIT.split(objective_text.lower())
    return {t for t in tokens
            if len(t) >= _MIN_KEYWORD_LEN and t not in _STOP_WORDS}


def _file_tokens(path: str, content: str) -> List[str]:
    """Scoring tokens for a file: path pieces plus Python symbol names."""
    tokens = [t for t in _TOKEN_SPLIT.split(path.lower()) if t]
    if path.endswith(".py"):
        try:
            tree = ast.parse(content, filename=path)
        except (SyntaxError, ValueError):
            return tokens
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                tokens.extend(
                    t for t in _TOKEN_SPLIT.split(node.name.lower()) if t)
    return tokens


def _read_text(root: str, path: str) -> Optional[str]:
    """Read a tracked file as UTF-8 text; ``None`` for binary/unreadable."""
    try:
        with open(f"{root}/{path}", "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        logger.debug("repomap: skipping unreadable file %s: %s", path, exc)
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def select_files(
    root: str,
    objective_text: str,
    budget_bytes: int = DEFAULT_SELECT_BUDGET_BYTES,
) -> List[Tuple[str, str]]:
    """Pick the tracked files most relevant to *objective_text*.

    Each tracked text file is scored by how many of its path/symbol tokens
    match keywords from the objective. Matching files are taken greedily by
    score (ties by path) and returned with their FULL contents; a file whose
    content would overflow the remaining budget is skipped — never
    truncated — and selection continues with the next candidate.

    Args:
        root: Git repo root (or any directory inside one).
        objective_text: Plain-language objective to score against.
        budget_bytes: Total UTF-8 byte budget across returned contents.

    Returns:
        ``(path, content)`` tuples in selection order (score descending,
        path ascending). Empty for non-repos, empty objectives, or when
        nothing scores above zero. Binary and unreadable files are skipped.
    """
    keywords = _keywords(objective_text)
    if not keywords:
        return []

    scored: List[Tuple[int, str, str]] = []
    for path in tracked_files(root):
        content = _read_text(root, path)
        if content is None:
            continue
        score = sum(1 for token in _file_tokens(path, content)
                    if token in keywords)
        if score > 0:
            scored.append((score, path, content))
    scored.sort(key=lambda item: (-item[0], item[1]))

    selected: List[Tuple[str, str]] = []
    remaining = budget_bytes
    for _score, path, content in scored:
        size = len(content.encode("utf-8"))
        if size > remaining:
            continue
        selected.append((path, content))
        remaining -= size
    return selected
