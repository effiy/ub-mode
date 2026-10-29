"""mutator — Mutation testing tool.

Usage::

    mutator [options] [path-or-filter ...]

Applies mutation operators to source code and runs a test command to
determine whether the tests catch each mutation.

Mutation operators:
  * ``invert-condition``  — ``==`` → ``!=``, ``<`` → ``>=``, etc.
  * ``remove-statement``  — delete a non-control statement
  * ``invert-return``     — return ``True`` → return ``False``
  * ``delete-expression`` — replace expression with ``None`` / ``undefined``

Options:
  -h, --help                Print this help and exit.
  --root <path>             Project root (default: current directory).
  -s, --source-root <path>  Walk this tree (may repeat).
  --test-command <cmd>      Shell command that runs tests (e.g. ``pytest`` or ``npm test``).
  --test-dir <path>         Working directory for tests (default: project root).
  --operators <list>        Comma-separated mutation operators (default: all).
  --timeout <seconds>       Test timeout per mutation (default: 60).
  --json                    Output as JSON.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HELP = __doc__ or ""


# ---------------------------------------------------------------------------
# Mutation operators for Python source code
# ---------------------------------------------------------------------------

@dataclass
class Mutation:
    """A single mutation at a specific source location."""

    file: str
    line: int
    column: int
    operator: str
    description: str
    original: str
    mutated: str

    # Result fields (filled after test run)
    killed: bool = False
    test_output: str = ""
    status: str = "unknown"  # unknown | killed | survived | error | timeout


# ---------------------------------------------------------------------------
# Python mutation operators (using ast + line-level replacement)
# ---------------------------------------------------------------------------

_COMPARISON_INVERSE = {
    ast.Eq: "!=",
    ast.NotEq: "==",
    ast.Lt: ">=",
    ast.LtE: ">",
    ast.Gt: "<=",
    ast.GtE: "<",
    ast.Is: "is not",
    ast.IsNot: "is",
    ast.In: "not in",
    ast.NotIn: "in",
}

_BOOL_INVERSE = {"True": "False", "False": "True"}


def discover_mutations(source_path: Path, operators: set[str]) -> list[Mutation]:
    """Find all possible mutation sites in a source file."""
    lang = _detect_lang(source_path)
    if lang == "python":
        return _python_mutations(source_path, operators)
    elif lang in ("typescript", "javascript"):
        return _ts_mutations(source_path, operators)
    return []


def _detect_lang(path: Path) -> str | None:
    suffix = path.suffix
    if suffix == ".py":
        return "python"
    if suffix in (".ts", ".tsx", ".mts", ".cts"):
        return "typescript"
    if suffix in (".js", ".jsx", ".mjs", ".cjs"):
        return "javascript"
    return None


def _python_mutations(path: Path, operators: set[str]) -> list[Mutation]:
    """Discover mutation sites in Python source code."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []

    mutations: list[Mutation] = []
    lines = source.splitlines()

    class MutationFinder(ast.NodeVisitor):
        def visit_Compare(self, node):
            if "invert-condition" not in operators:
                self.generic_visit(node)
                return
            for i, op in enumerate(node.ops):
                if type(op) in _COMPARISON_INVERSE:
                    lineno = node.lineno
                    mutations.append(Mutation(
                        file=str(path),
                        line=lineno,
                        column=getattr(node, "col_offset", 0),
                        operator="invert-condition",
                        description=f"Invert {ast.unparse(op)} to {_COMPARISON_INVERSE[type(op)]}",
                        original=ast.unparse(op),
                        mutated=_COMPARISON_INVERSE[type(op)],
                    ))
            self.generic_visit(node)

        def visit_Expr(self, node):
            if "remove-statement" in operators:
                if not _is_docstring(node, source):
                    mutations.append(Mutation(
                        file=str(path),
                        line=node.lineno,
                        column=getattr(node, "col_offset", 0),
                        operator="remove-statement",
                        description=f"Remove expression: {ast.unparse(node.value)[:60]}",
                        original=ast.get_source_segment(source, node) or "",
                        mutated="",
                    ))
            self.generic_visit(node)

        def visit_Return(self, node):
            if "invert-return" in operators and node.value is not None:
                val_source = ast.get_source_segment(source, node.value) or ""
                mutations.append(Mutation(
                    file=str(path),
                    line=node.lineno,
                    column=getattr(node, "col_offset", 0),
                    operator="invert-return",
                    description=f"Invert return: {val_source[:40]}",
                    original=f"return {val_source}",
                    mutated="return None",
                ))
            self.generic_visit(node)

        def visit_Constant(self, node):
            if "invert-return" in operators and isinstance(node.value, bool):
                mutations.append(Mutation(
                    file=str(path),
                    line=node.lineno,
                    column=getattr(node, "col_offset", 0),
                    operator="invert-return",
                    description=f"Invert {node.value}",
                    original=str(node.value),
                    mutated=str(not node.value),
                ))
            self.generic_visit(node)

        def visit_Assign(self, node):
            if "remove-statement" in operators:
                src = ast.get_source_segment(source, node)
                if src:
                    mutations.append(Mutation(
                        file=str(path),
                        line=node.lineno,
                        column=getattr(node, "col_offset", 0),
                        operator="remove-statement",
                        description=f"Remove assignment: {src[:60]}",
                        original=src,
                        mutated="",
                    ))
            self.generic_visit(node)

    MutationFinder().visit(tree)
    return mutations


def _is_docstring(node: ast.Expr, source: str) -> bool:
    """Check if an expression statement is actually a docstring."""
    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
        return True
    return False


def _ts_mutations(path: Path, operators: set[str]) -> list[Mutation]:
    """Discover mutation sites in TypeScript/JavaScript source.

    Uses regex-based approach since we don't have tree-sitter loaded here.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    mutations: list[Mutation] = []
    lines = source.splitlines()

    # Comparison operators
    if "invert-condition" in operators:
        patterns = [
            (r"(===\s)", "!==", "=== → !=="),
            (r"(!==\s)", "===", "!== → ==="),
            (r"(<=\s)", ">", "<= → >"),
            (r"(>=\s)", "<", ">= → <"),
            (r"(<\s)", ">=", "< → >="),
            (r"(>\s)", "<=", "> → <="),
        ]
        for lineno, line in enumerate(lines, 1):
            for pat, replacement, desc in patterns:
                if re.search(pat, line):
                    mutations.append(Mutation(
                        file=str(path),
                        line=lineno,
                        column=0,
                        operator="invert-condition",
                        description=desc,
                        original=pat.strip("()\\s"),
                        mutated=replacement,
                    ))

    # Boolean inversions
    if "invert-return" in operators:
        for lineno, line in enumerate(lines, 1):
            for orig, repl in _BOOL_INVERSE.items():
                if re.search(rf'\b{orig}\b', line):
                    mutations.append(Mutation(
                        file=str(path),
                        line=lineno,
                        column=0,
                        operator="invert-return",
                        description=f"Invert {orig} → {repl}",
                        original=orig,
                        mutated=repl,
                    ))

    return mutations


def apply_mutation(mutation: Mutation) -> str | None:
    """Apply a mutation to the source file and return the mutated content.

    Returns None if the mutation cannot be applied.
    """
    try:
        source = Path(mutation.file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    lines = source.splitlines()
    if mutation.line < 1 or mutation.line > len(lines):
        return None

    target_line = lines[mutation.line - 1]

    if mutation.operator == "remove-statement":
        lines[mutation.line - 1] = f"# [MUTATED] removed: {mutation.original[:50]}"
        return "\n".join(lines) + "\n"

    if mutation.operator == "invert-condition":
        # Replace the comparison operator
        orig = mutation.original.strip()
        repl = mutation.mutated.strip()
        lines[mutation.line - 1] = target_line.replace(orig, repl, 1)
        lines[mutation.line - 1] += f"  # [MUTATED: {orig} → {repl}]"
        return "\n".join(lines) + "\n"

    if mutation.operator == "invert-return":
        # Replace return value
        orig = mutation.original.strip()
        repl = mutation.mutated.strip()
        lines[mutation.line - 1] = target_line.replace(orig, repl, 1)
        lines[mutation.line - 1] += f"  # [MUTATED: {orig} → {repl}]"
        return "\n".join(lines) + "\n"

    # Generic: try line-level replacement
    if mutation.original in target_line:
        lines[mutation.line - 1] = target_line.replace(mutation.original, mutation.mutated, 1)
        return "\n".join(lines) + "\n"

    return None


def run_tests(test_command: str, cwd: Path, timeout: int = 60) -> tuple[int, str]:
    """Run the test command and return (exit_code, combined_output)."""
    try:
        result = subprocess.run(
            test_command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "CI": "true"},
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except Exception as e:
        return -1, f"ERROR: {e}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> dict:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        return {"action": "help"}

    opts: dict = {
        "action": "analyze",
        "root": Path("."),
        "source_roots": [],
        "positionals": [],
        "test_command": "pytest",
        "test_dir": None,
        "operators": {"invert-condition", "remove-statement", "invert-return"},
        "timeout": 60,
        "json_output": False,
    }

    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-s", "--source-root"):
            i += 1
            if i < len(args):
                opts["source_roots"].append(args[i])
        elif a == "--root":
            i += 1
            if i < len(args):
                opts["root"] = Path(args[i])
        elif a == "--test-command":
            i += 1
            if i < len(args):
                opts["test_command"] = args[i]
        elif a == "--test-dir":
            i += 1
            if i < len(args):
                opts["test_dir"] = Path(args[i])
        elif a == "--operators":
            i += 1
            if i < len(args):
                opts["operators"] = set(args[i].split(","))
        elif a == "--timeout":
            i += 1
            if i < len(args):
                opts["timeout"] = int(args[i])
        elif a == "--json":
            opts["json_output"] = True
        elif not a.startswith("-"):
            opts["positionals"].append(a)
        i += 1

    return opts


def run(argv: list[str] | None = None) -> int:
    opts = parse_args(argv)
    if opts.get("action") == "help":
        print(HELP)
        return 0

    from ub_tools.shared import iter_source_files, language_of

    root = Path(opts["root"]).resolve()
    test_dir = opts.get("test_dir", root)
    if test_dir is None:
        test_dir = root

    # Select source files
    if opts["source_roots"]:
        files = iter_source_files([(root / r).resolve() for r in opts["source_roots"]])
    elif opts["positionals"]:
        files = []
        for arg in opts["positionals"]:
            p = (root / arg).resolve() if not Path(arg).is_absolute() else Path(arg)
            if p.is_dir():
                files.extend(iter_source_files([p]))
            elif language_of(p):
                files.append(p)
    else:
        files = iter_source_files([root])

    if not files:
        print("No source files to mutate.", file=sys.stderr)
        return 0

    # Discover mutations
    all_mutations: list[Mutation] = []
    for path in files:
        mutations = discover_mutations(path, opts["operators"])
        all_mutations.extend(mutations)

    if not all_mutations:
        print("No mutation sites found.", file=sys.stderr)
        return 0

    print(f"Found {len(all_mutations)} mutation sites in {len(files)} file(s).", file=sys.stderr)
    print(f"Running test command: {opts['test_command']}", file=sys.stderr)

    # First, run tests without mutations to establish baseline
    print("Running baseline tests...", file=sys.stderr)
    baseline_code, baseline_output = run_tests(opts["test_command"], test_dir, opts["timeout"])
    if baseline_code != 0:
        print(f"⚠ Baseline tests failed (exit {baseline_code}). Mutation results may be unreliable.",
              file=sys.stderr)

    # Run each mutation
    killed = 0
    survived = 0
    errors = 0
    total = len(all_mutations)

    for i, mutation in enumerate(all_mutations):
        print(f"  [{i + 1}/{total}] {mutation.file}:{mutation.line} — {mutation.description}",
              file=sys.stderr, end=" ")

        mutated_source = apply_mutation(mutation)
        if mutated_source is None:
            mutation.status = "error"
            errors += 1
            print("✗ (failed to apply)", file=sys.stderr)
            continue

        # Write mutated source to temp file, swap with original
        orig_path = Path(mutation.file)
        backup = orig_path.read_bytes()
        try:
            orig_path.write_text(mutated_source, encoding="utf-8")
            exit_code, output = run_tests(test_command=str(opts["test_command"]),
                                          cwd=test_dir,
                                          timeout=opts["timeout"])
        finally:
            # Always restore original
            orig_path.write_bytes(backup)

        mutation.test_output = output[:500]  # truncate

        if exit_code == -1:
            mutation.status = "timeout"
            errors += 1
            print("⏱ timeout", file=sys.stderr)
        elif exit_code != 0:
            # Tests failed → mutation was caught → killed
            mutation.killed = True
            mutation.status = "killed"
            killed += 1
            print("✓ killed", file=sys.stderr)
        else:
            # Tests passed → mutation survived → not covered
            mutation.status = "survived"
            survived += 1
            print("✗ SURVIVED", file=sys.stderr)

    # Report
    mutation_score = killed / total * 100 if total > 0 else 0

    if opts.get("json_output"):
        data = {
            "total": total,
            "killed": killed,
            "survived": survived,
            "errors": errors,
            "mutation_score": round(mutation_score, 1),
            "survivors": [
                {
                    "file": m.file,
                    "line": m.line,
                    "operator": m.operator,
                    "description": m.description,
                }
                for m in all_mutations if m.status == "survived"
            ],
        }
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(f"\n{'='*60}")
        print(f"Mutation Test Results")
        print(f"{'='*60}")
        print(f"  Total mutations:  {total}")
        print(f"  Killed:           {killed} ✓")
        print(f"  Survived:         {survived} ✗")
        print(f"  Errors/Timeouts:  {errors}")
        print(f"  Mutation score:   {mutation_score:.1f}%")
        print(f"{'='*60}")

        if survived > 0:
            print(f"\nSurvivors (tests did not catch these mutations):")
            for m in all_mutations:
                if m.status == "survived":
                    print(f"  • {m.file}:{m.line} — {m.description}")
            print()

    return 0 if survived == 0 else 1


def main() -> None:
    sys.exit(run())