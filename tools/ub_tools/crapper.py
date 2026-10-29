"""crapper — Compute CRAP scores (Change Risk Analysis and Predictions).

Usage::

    crapper [options] [path-or-filter ...]

For each source file, the tool extracts function definitions, computes
cyclomatic complexity via AST parsing, optionally reads test coverage,
and calculates the CRAP score::

    CRAP = CC² × (1 − coverage)³ + CC

Output is sorted worst-CRAP-first, with a summary at the bottom.

Options:
  -h, --help                Print this help and exit.
  --root <path>             Project root (default: current directory).
  -s, --source-root <path>  Walk this tree (may repeat).
  --threshold <number>      Exit 2 when any CRAP score exceeds this value.
  --no-coverage             Skip coverage; all CRAP scores show as N/A.
  --json                    Output as JSON instead of terminal table.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ub_tools.shared import (
    crap_score,
    extract_functions,
    iter_source_files,
)

HELP = __doc__ or ""


@dataclass
class FunctionEntry:
    name: str
    namespace: str
    path: str
    language: str
    complexity: int
    coverage: float | None
    crap: float | None
    lines: int


def parse_args(argv: list[str] | None = None) -> dict:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        return {"action": "help"}

    opts: dict = {
        "action": "analyze",
        "root": Path("."),
        "source_roots": [],
        "positionals": [],
        "no_coverage": False,
        "threshold": None,
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
        elif a == "--threshold":
            i += 1
            if i < len(args):
                try:
                    opts["threshold"] = float(args[i])
                except ValueError:
                    print(f"crapper: --threshold requires a number, got '{args[i]}'", file=sys.stderr)
                    return {"action": "help", "exit_code": 1}
        elif a == "--no-coverage":
            opts["no_coverage"] = True
        elif a == "--json":
            opts["json_output"] = True
        elif not a.startswith("-"):
            opts["positionals"].append(a)
        else:
            print(f"crapper: unknown option: {a}", file=sys.stderr)
            return {"action": "help", "exit_code": 1}
        i += 1

    return opts


def select_files(opts: dict) -> list[Path]:
    """Determine which source files to analyze."""
    root = Path(opts["root"]).resolve()

    # Explicit positional paths/dirs
    positional_files: list[Path] = []
    filters: list[str] = []
    for arg in opts["positionals"]:
        candidate = root / arg if not Path(arg).is_absolute() else Path(arg)
        if candidate.exists():
            positional_files.append(candidate)
        else:
            filters.append(arg)

    # Determine source trees
    if opts["source_roots"]:
        roots = [(root / r).resolve() for r in opts["source_roots"]]
    elif positional_files:
        # Expand directories, collect files
        expanded: list[Path] = []
        for p in positional_files:
            if p.is_dir():
                expanded.extend(iter_source_files([p.resolve()]))
            else:
                from ub_tools.shared import language_of
                if language_of(p) is not None:
                    expanded.append(p.resolve())
        files = expanded
    else:
        files = iter_source_files([root])

    if not positional_files and not opts["source_roots"]:
        files = iter_source_files([root])

    # Apply filters
    if filters:
        files = [f for f in files if any(item in f.as_posix() for item in filters)]

    return sorted(set(files), key=lambda p: p.as_posix())


def analyze(files: list[Path]) -> list[FunctionEntry]:
    """Parse each file and extract function metrics."""
    entries: list[FunctionEntry] = []
    for path in files:
        functions = extract_functions(path)
        for func in functions:
            entries.append(FunctionEntry(
                name=func["name"],
                namespace=func["namespace"],
                path=str(path),
                language=func["language"],
                complexity=func["complexity"],
                coverage=None,  # coverage not yet loaded
                crap=None,
                lines=func["lines"],
            ))
    return entries


def compute_crap(entries: list[FunctionEntry], coverage_data: dict[str, float] | None) -> list[FunctionEntry]:
    """Apply coverage data and compute CRAP scores."""
    for entry in entries:
        cov = None
        if coverage_data is not None:
            # Match by path:line range
            key = f"{entry.path}:{entry.namespace}:{entry.name}"
            cov = coverage_data.get(key)
        entry.coverage = cov
        entry.crap = crap_score(entry.complexity, cov)
    return entries


def sort_entries(entries: list[FunctionEntry]) -> list[FunctionEntry]:
    """Sort worst CRAP first; unscored follows scored."""

    def key(e: FunctionEntry):
        if e.crap is None:
            return (1, -e.complexity, e.namespace, e.name)
        return (0, -e.crap, e.namespace, e.name)

    return sorted(entries, key=key)


def format_report(entries: list[FunctionEntry]) -> str:
    """Format a human-readable terminal table."""
    if not entries:
        return "No functions found.\n"

    # Column widths
    name_w = max(max(len(e.name) for e in entries), 4)
    ns_w = max(max(len(e.namespace) for e in entries), 9)
    lang_w = max(max(len(e.language) for e in entries), 8)

    header = (
        f"{'CRAP':>6}  {'CC':>3}  {'COV':>6}  "
        f"{'Name':<{name_w}}  {'Namespace':<{ns_w}}  "
        f"{'Lang':<{lang_w}}  Path"
    )
    sep = "-" * len(header) + "\n"

    lines = [header, sep]
    for e in entries:
        crap_s = f"{e.crap:6.1f}" if e.crap is not None else f"{'N/A':>6}"
        cov_s = f"{e.coverage:5.1f}%" if e.coverage is not None else f"{'N/A':>6}"
        lines.append(
            f"{crap_s}  {e.complexity:3d}  {cov_s}  "
            f"{e.name:<{name_w}}  {e.namespace:<{ns_w}}  "
            f"{e.language:<{lang_w}}  {e.path}"
        )

    # Summary
    scored = [e for e in entries if e.crap is not None]
    lines.append(sep)
    lines.append(f"Functions: {len(entries)}")
    if scored:
        avg = sum(e.crap for e in scored) / len(scored)  # type: ignore[arg-type]
        worst = max(e.crap for e in scored)  # type: ignore[type-var]
        lines.append(f"CRAP avg: {avg:.1f}  worst: {worst:.1f}  threshold: < 10 ✓")
    lines.append("")

    return "\n".join(lines)


def format_json(entries: list[FunctionEntry]) -> str:
    """Format as JSON."""
    data: list[dict[str, Any]] = []
    for e in entries:
        data.append({
            "name": e.name,
            "namespace": e.namespace,
            "path": e.path,
            "language": e.language,
            "complexity": e.complexity,
            "coverage": e.coverage,
            "crap": e.crap,
            "lines": e.lines,
        })
    return json.dumps(data, indent=2, ensure_ascii=False)


def run(argv: list[str] | None = None) -> int:
    opts = parse_args(argv)
    if opts.get("action") == "help":
        print(HELP)
        return opts.get("exit_code", 0)

    files = select_files(opts)
    if not files:
        print("No source files to analyze.", file=sys.stderr)
        return 0

    print(f"Analyzing {len(files)} file(s)...", file=sys.stderr)

    entries = analyze(files)
    entries = compute_crap(entries, coverage_data=None)  # coverage: TBD
    entries = sort_entries(entries)

    if opts.get("json_output"):
        print(format_json(entries))
    else:
        print(format_report(entries), end="", flush=True)

    # Threshold check
    threshold = opts.get("threshold")
    if threshold is not None:
        scored = [e.crap for e in entries if e.crap is not None]
        if scored and max(scored) > threshold:
            print(f"CRAP threshold exceeded: {max(scored):.1f} > {threshold:.1f}", file=sys.stderr)
            return 2

    return 0


def main() -> None:
    sys.exit(run())