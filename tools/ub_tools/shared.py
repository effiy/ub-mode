"""UB-Tools shared utilities for tree-sitter parsing, file discovery, and CRAP formula.

Provides common infrastructure for crapper, dryer, and mutator tools in the
ub-mode quality gate pipeline. All tools use tree-sitter for language-aware
AST parsing and share the same file discovery and complexity metrics logic.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Language detection and file discovery
# ---------------------------------------------------------------------------

PYTHON_EXTENSIONS: tuple[str, ...] = (".py",)
TS_JS_EXTENSIONS: tuple[str, ...] = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
SOURCE_EXTENSIONS: tuple[str, ...] = PYTHON_EXTENSIONS + TS_JS_EXTENSIONS

SKIP_DIRS: set[str] = {
    "__pycache__", ".git", ".hg", ".svn", ".tox", ".venv", ".env",
    "node_modules", ".next", "dist", "build", ".cache", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".metrics",
    "target",     # Java/Maven
    "vendor",     # Go
    "coverage", ".coverage", "htmlcov",
}

TEST_DIRS: set[str] = {"tests", "test", "__tests__", "spec"}

TEST_PATTERNS: list[str] = [
    r"^test_",
    r"_test\.(py|ts|tsx|js|jsx|mjs|cjs|mts|cts)$",
    r"^conftest\.py$",
    r"\.test\.(ts|tsx|js|jsx|mjs|cjs|mts|cts)$",
    r"\.spec\.(ts|tsx|js|jsx|mjs|cjs|mts|cts)$",
]

_test_re = re.compile("|".join(TEST_PATTERNS))


def is_test_file(path: Path) -> bool:
    """Return True when *path* looks like a test file by name."""
    name = path.name
    return bool(_test_re.search(name))


def language_of(path: Path) -> str | None:
    """Return the language name for *path*, or None when it is not a source file."""
    suffix = path.suffix
    if suffix in PYTHON_EXTENSIONS:
        return "Python"
    if suffix in TS_JS_EXTENSIONS:
        return "TypeScript" if suffix in (".ts", ".tsx", ".mts", ".cts") else "JavaScript"
    return None


def iter_source_files(roots: list[Path]) -> list[Path]:
    """Walk *roots* recursively and return all non-test source files, sorted."""
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in root.walk():
            # Skip excluded directories
            dirnames[:] = sorted(set(dirnames) - SKIP_DIRS - TEST_DIRS)

            for filename in sorted(filenames):
                path = dirpath / filename
                if language_of(path) is not None and not is_test_file(path):
                    found.append(path.resolve())
    found.sort(key=lambda p: p.as_posix())
    return found


def module_namespace(path: Path, root: Path | None = None) -> str:
    """Derive a dotted namespace from *path* relative to *root*.

    e.g. ``src/foo/bar.py`` → ``foo.bar`` (Python) or
         ``src/foo/bar.ts`` → ``foo.bar`` (TypeScript).
    When *root* is None, use the current working directory.
    """
    if root is None:
        root = Path.cwd()
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = path
    parts = list(rel.parts)
    # Strip "src" or equivalent top-level source dir
    if parts and parts[0] in ("src", "lib", "app", "packages"):
        parts = parts[1:]
    # Drop the file extension and use dots
    if parts:
        parts[-1] = Path(parts[-1]).stem
    return ".".join(parts)


# ---------------------------------------------------------------------------
# Cyclomatic complexity via tree-sitter
# ---------------------------------------------------------------------------

# Decision points that each add 1 to complexity (baseline is 1).
_COMPLEXITY_NODE_TYPES: set[str] = {
    "if_statement", "elif_clause", "else_clause",
    "for_statement", "while_statement", "do_statement",
    "case_clause", "default_clause",
    "except_clause", "finally_clause",
    "and_operator", "or_operator",
    "ternary_expression", "conditional_expression",
    "match_statement",  # Python 3.10+ match/case
    "switch_statement",
    "try_statement", "with_statement",
    "async_for_statement", "async_with_statement",
    "for_in_statement",        # TypeScript
    "switch_case", "switch_default",  # TypeScript/JS
    "catch_clause",            # TypeScript/JS try/catch
}

_FUNCTION_NODE_TYPES: set[str] = {
    "function_definition", "function_declaration",
    "method_definition", "method_declaration",
    "arrow_function",
    "function_expression",
    "generator_function", "generator_function_declaration",
    "async_function_declaration", "async_function_expression",
    "async_generator_function_declaration",
}

# Python‑specific tree‑sitter node type names differ.
_PY_FUNCTION_TYPES: set[str] = {
    "function_definition",
    "lambda",
}

_PY_COMPLEXITY_TYPES: set[str] = {
    "if_statement", "elif_clause", "else_clause",
    "for_statement", "while_statement",
    "try_statement", "except_clause", "finally_clause",
    "with_statement",
    "match_statement", "case_clause",
    "and_operator", "or_operator",
    "conditional_expression",
    "list_comprehension", "set_comprehension",
    "dictionary_comprehension", "generator_expression",
}


def _init_tree_sitter():
    """Lazily import tree-sitter (heavy dependency)."""
    import os as _os
    # Ensure writable cache directory so tree-sitter-language-pack can
    # download and cache language grammars on first use.
    if not _os.environ.get("TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"):
        _default_cache = Path(__file__).resolve().parent.parent.parent / ".cache"
        _os.environ["TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"] = str(_default_cache)
    import tree_sitter
    import tree_sitter_language_pack as tslp
    return tree_sitter, tslp


def _get_language(lang_name: str) -> tuple:
    """Return (Language, function_node_types, complexity_node_types) for a language.

    Returns ``(None, (), ())`` when the language is not supported.
    """
    if lang_name == "Python":
        return None, _PY_FUNCTION_TYPES, _PY_COMPLEXITY_TYPES  # use built-in ast
    if lang_name in ("TypeScript", "JavaScript"):
        try:
            ts, tslp = _init_tree_sitter()
            parser = tslp.get_parser("typescript" if lang_name == "TypeScript" else "javascript")
            ts_lang = parser.language
            return ts_lang, _FUNCTION_NODE_TYPES, _COMPLEXITY_NODE_TYPES
        except Exception:
            return None, (), ()
    return None, (), ()


def _python_ast_functions(source: str, path: Path) -> list[dict]:
    """Use Python's built-in ``ast`` module (no tree-sitter needed)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    results: list[dict] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            results.append(_python_func_info(node, source, path, "def"))
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node):
            results.append(_python_func_info(node, source, path, "async def"))
            self.generic_visit(node)

    Visitor().visit(tree)
    return results


def _python_func_info(node: ast.FunctionDef | ast.AsyncFunctionDef, source: str, path: Path, kind: str) -> dict:
    """Extract function metadata + cyclomatic complexity from a Python AST node."""
    complexity = 1  # baseline
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.While, ast.Try,
                               ast.ExceptHandler, ast.With, ast.AsyncWith,
                               ast.BoolOp, ast.IfExp,
                               ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            complexity += 1
        elif isinstance(child, ast.Match):  # Python 3.10+
            # match itself adds 1, each case also adds 1
            complexity += 1 + len(child.cases)

    start_line = node.lineno
    end_line = getattr(node, 'end_lineno', start_line)
    return {
        "name": node.name,
        "namespace": module_namespace(path),
        "path": str(path),
        "language": "Python",
        "kind": kind,
        "complexity": complexity,
        "start_line": start_line,
        "end_line": end_line,
        "lines": end_line - start_line + 1,
    }


def _ts_functions(source_bytes: bytes, path: Path, lang_name: str) -> list[dict]:
    """Extract functions from TypeScript/JS source using tree-sitter."""
    ts_lang, func_types, complexity_types = _get_language(lang_name)
    if ts_lang is None:
        return []

    ts, tslp = _init_tree_sitter()
    parser = ts.Parser(ts_lang)
    tree = parser.parse(source_bytes)
    source_text = source_bytes.decode("utf-8")

    results: list[dict] = []
    _walk_ts_tree(tree.root_node, source_text, path, lang_name, func_types, complexity_types, results)
    return results


def _walk_ts_tree(node, source_text: str, path: Path, lang_name: str,
                  func_types: set[str], complexity_types: set[str],
                  results: list[dict], parent_name: str = ""):
    """Walk a tree-sitter syntax tree, collecting function definitions."""
    if node.type in func_types:
        name = _extract_ts_function_name(node, source_text)
        if name:
            complexity = _compute_ts_complexity(node, complexity_types)
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            results.append({
                "name": name,
                "namespace": module_namespace(path),
                "path": str(path),
                "language": lang_name,
                "kind": node.type,
                "complexity": complexity,
                "start_line": start_line,
                "end_line": end_line,
                "lines": end_line - start_line + 1,
            })
        return

    for child in node.children:
        _walk_ts_tree(child, source_text, path, lang_name, func_types, complexity_types, results, parent_name)


def _extract_ts_function_name(node, source_text: str) -> str:
    """Extract the function name from a tree-sitter function node."""
    for child in node.children:
        if child.type == "identifier":
            return source_text[child.start_byte:child.end_byte]
        if child.type in ("property_identifier", "string"):
            return source_text[child.start_byte:child.end_byte].strip("'\"")
        # Check for nested name node
        if child.type in ("variable_declarator", "public_field_definition"):
            for sub in child.children:
                if sub.type in ("identifier", "property_identifier"):
                    return source_text[sub.start_byte:sub.end_byte]
    # Arrow function assigned to variable: look at parent
    parent = node.parent
    if parent and parent.type == "variable_declarator":
        for child in parent.children:
            if child.type == "identifier":
                return source_text[child.start_byte:child.end_byte]
    # Method definition: check parent class
    return node.type.replace("_", " ")


def _compute_ts_complexity(node, complexity_types: set[str]) -> int:
    """Compute cyclomatic complexity for a tree-sitter function node."""
    complexity = 1
    for child in node.children:
        if child.type in complexity_types:
            complexity += 1
        complexity += _compute_ts_complexity(child, complexity_types) - 1
    return complexity


def extract_functions(path: Path) -> list[dict]:
    """Parse *path* and return a list of function metadata dicts.

    Each dict has keys: name, namespace, path, language, kind, complexity,
    start_line, end_line, lines.
    """
    lang = language_of(path)
    if lang is None:
        return []

    try:
        source = path.read_bytes()
    except (OSError, UnicodeDecodeError):
        return []

    if lang == "Python":
        return _python_ast_functions(source.decode("utf-8"), path)
    if lang in ("TypeScript", "JavaScript"):
        return _ts_functions(source, path, lang)
    return []


# ---------------------------------------------------------------------------
# CRAP formula (compatible with Uncle Bob's crapper)
# ---------------------------------------------------------------------------

def crap_score(complexity: int, coverage_pct: float | None) -> float | None:
    """Compute the CRAP score.

    ``CRAP = CC² × (1 − coverage)³ + CC``

    *complexity* is the cyclomatic complexity (integer ≥ 1).
    *coverage_pct* is the test coverage percentage (0‑100), or None.

    Returns None when coverage is not available (N/A).
    """
    if coverage_pct is None:
        return None
    cc = float(complexity)
    uncovered = 1.0 - (float(coverage_pct) / 100.0)
    return (cc * cc * uncovered * uncovered * uncovered) + cc


# ---------------------------------------------------------------------------
# String hashing for duplicate detection
# ---------------------------------------------------------------------------

def normalize_code(source: str) -> str:
    """Normalize source code for structural comparison.

    Strips comments, normalizes whitespace, removes string literals.
    Returns a canonical form suitable for structural hashing.
    """
    # Remove single-line comments (// and #)
    lines = source.splitlines()
    normalized: list[str] = []
    for line in lines:
        stripped = line.strip()
        # Skip Python comments (but not # inside strings — heuristic)
        if stripped.startswith("#"):
            continue
        # Skip // comments (TS/JS)
        if "//" in stripped:
            # Keep only what comes before // (heuristic)
            idx = stripped.index("//")
            before = stripped[:idx].strip()
            if before:
                normalized.append(before)
            continue
        # Collapse whitespace
        collapsed = " ".join(stripped.split())
        if collapsed:
            normalized.append(collapsed)
    return "\n".join(normalized)


def structural_hash(code: str) -> int:
    """Compute a structural hash of normalized code.

    Uses Python's built-in hash. For cross-run stability, we strip all variable
    names and literal values, leaving only the code skeleton.
    """
    # Strip all identifiers (heuristic: replace words starting with letter)
    import re as _re
    skeleton = _re.sub(r'\b[a-zA-Z_]\w*\b', '_', code)
    # Strip all numeric literals
    skeleton = _re.sub(r'\b\d+(\.\d+)?\b', '0', skeleton)
    # Strip string literals (both " and ')
    skeleton = _re.sub(r'"[^"]*"', '""', skeleton)
    skeleton = _re.sub(r"'[^']*'", "''", skeleton)
    # Normalize whitespace
    skeleton = " ".join(skeleton.split())
    return hash(skeleton)


def extract_code_blocks(source: str, min_lines: int = 5) -> list[tuple[int, int, str]]:
    """Break source into logical code blocks (functions, classes, or paragraph chunks).

    Returns list of (start_line, end_line, block_text) tuples.
    """
    lang = None
    # Try to detect language
    lines = source.splitlines()
    blocks: list[tuple[int, int, str]] = []
    current_block: list[str] = []
    current_start: int = 0

    indent_level = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            # Empty line: end of block or separator
            if current_block and len(current_block) >= min_lines:
                blocks.append((current_start + 1, i, "\n".join(current_block)))
                current_block = []
            continue

        # Heuristic: top-level items start new blocks
        if not line[0].isspace() and current_block:
            if len(current_block) >= min_lines:
                blocks.append((current_start + 1, i, "\n".join(current_block)))
            current_block = [line]
            current_start = i
        else:
            if not current_block:
                current_start = i
            current_block.append(line)

    # Last block
    if current_block and len(current_block) >= min_lines:
        blocks.append((current_start + 1, len(lines), "\n".join(current_block)))

    return blocks


# ---------------------------------------------------------------------------
# Similarity measures
# ---------------------------------------------------------------------------

def jaccard_similarity(a: set, b: set) -> float:
    """Compute Jaccard similarity between two sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def token_set(text: str) -> set[str]:
    """Break text into a set of tokens for comparison."""
    import re as _re
    return set(_re.findall(r'\b\w+\b', text.lower()))