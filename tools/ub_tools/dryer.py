"""dryer — Detect structural code duplication.

Usage::

    dryer [options] [path-or-filter ...]

Extracts logical code blocks from each source file, normalizes them to a
canonical structural form, and finds pairs with Jaccard similarity ≥ *threshold*.
Output is sorted by similarity, highest first.

Options:
  -h, --help                Print this help and exit.
  --root <path>             Project root (default: current directory).
  -s, --source-root <path>  Walk this tree (may repeat).
  --threshold <float>       Minimum similarity to report (default: 0.8).
  --min-lines <int>         Minimum block size in lines (default: 5).
  --json                    Output as JSON instead of terminal report.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ub_tools.shared import (
    extract_code_blocks,
    jaccard_similarity,
    language_of,
    normalize_code,
    structural_hash,
    token_set,
)

HELP = __doc__ or ""


@dataclass
class DuplicatePair:
    file_a: str
    lines_a: str  # e.g. "12-25"
    block_a: str
    file_b: str
    lines_b: str
    block_b: str
    similarity: float
    rank: float  # 0-1 aggregated similarity score


def parse_args(argv: list[str] | None = None) -> dict:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        return {"action": "help"}

    opts: dict = {
        "action": "analyze",
        "root": Path("."),
        "source_roots": [],
        "positionals": [],
        "threshold": 0.8,
        "min_lines": 5,
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
                opts["threshold"] = float(args[i])
        elif a == "--min-lines":
            i += 1
            if i < len(args):
                opts["min_lines"] = int(args[i])
        elif a == "--json":
            opts["json_output"] = True
        elif not a.startswith("-"):
            opts["positionals"].append(a)
        i += 1

    return opts


def collect_blocks(opts: dict) -> list[dict]:
    """Walk source files and extract all code blocks."""
    from ub_tools.shared import iter_source_files

    root = Path(opts["root"]).resolve()

    if opts["source_roots"]:
        files = iter_source_files([(root / r).resolve() for r in opts["source_roots"]])
    elif opts["positionals"]:
        files: list[Path] = []
        for arg in opts["positionals"]:
            p = (root / arg).resolve() if not Path(arg).is_absolute() else Path(arg)
            if p.is_dir():
                files.extend(iter_source_files([p]))
            elif language_of(p):
                files.append(p)
    else:
        files = iter_source_files([root])

    blocks: list[dict] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        raw_blocks = extract_code_blocks(source, min_lines=opts["min_lines"])
        for start, end, text in raw_blocks:
            blocks.append({
                "path": str(path),
                "start_line": start,
                "end_line": end,
                "text": text,
                "normalized": normalize_code(text),
                "tokens": token_set(text),
                "struct_hash": structural_hash(text),
            })

    return blocks


def find_duplicates(blocks: list[dict], threshold: float) -> list[DuplicatePair]:
    """Compare all block pairs and find duplicates above threshold."""
    pairs: list[DuplicatePair] = []
    n = len(blocks)
    seen_hashes: dict[int, int] = {}  # struct_hash → first occurrence index

    for i in range(n):
        bh = blocks[i]["struct_hash"]
        for j in range(i + 1, n):
            # Fast path: structural hash match
            if bh == blocks[j]["struct_hash"]:
                sim = 1.0
            else:
                sim = jaccard_similarity(blocks[i]["tokens"], blocks[j]["tokens"])

            if sim >= threshold:
                a, b = blocks[i], blocks[j]
                pairs.append(DuplicatePair(
                    file_a=a["path"],
                    lines_a=f"{a['start_line']}-{a['end_line']}",
                    block_a=a["text"][:200],
                    file_b=b["path"],
                    lines_b=f"{b['start_line']}-{b['end_line']}",
                    block_b=b["text"][:200],
                    similarity=round(sim, 4),
                    rank=sim,
                ))

    # Sort by similarity descending
    pairs.sort(key=lambda p: (-p.similarity, p.file_a, p.file_b))
    return pairs


def format_report(pairs: list[DuplicatePair]) -> str:
    """Format a human-readable terminal report."""
    if not pairs:
        return "No duplicates found above threshold.\n"

    # Group by file
    by_file: dict[str, list[DuplicatePair]] = defaultdict(list)
    for p in pairs:
        by_file[p.file_a].append(p)

    lines = [f"Found {len(pairs)} duplicate block(s):\n"]
    for i, p in enumerate(pairs, 1):
        lines.append(
            f"  {i}. Similarity: {p.similarity:.2f}\n"
            f"     A: {p.file_a}  lines {p.lines_a}\n"
            f"     B: {p.file_b}  lines {p.lines_b}\n"
        )

    # Summary
    total_files = len(by_file)
    high = sum(1 for p in pairs if p.similarity >= 0.95)
    medium = sum(1 for p in pairs if 0.85 <= p.similarity < 0.95)

    lines.append(
        f"\nSummary: {len(pairs)} duplicates across {total_files} files "
        f"(high ≥0.95: {high}, medium 0.85-0.95: {medium})\n"
    )
    return "\n".join(lines)


def format_json_output(pairs: list[DuplicatePair]) -> str:
    """Format as JSON."""
    data = [
        {
            "file_a": p.file_a,
            "lines_a": p.lines_a,
            "file_b": p.file_b,
            "lines_b": p.lines_b,
            "similarity": p.similarity,
        }
        for p in pairs
    ]
    return json.dumps(data, indent=2, ensure_ascii=False)


def run(argv: list[str] | None = None) -> int:
    opts = parse_args(argv)
    if opts.get("action") == "help":
        print(HELP)
        return 0

    threshold = opts["threshold"]
    print(f"Scanning for duplicates (threshold ≥ {threshold})...", file=sys.stderr)

    blocks = collect_blocks(opts)
    if not blocks:
        print("No source files to scan.", file=sys.stderr)
        return 0

    print(f"Extracted {len(blocks)} code blocks from source files.", file=sys.stderr)

    pairs = find_duplicates(blocks, threshold)

    if opts.get("json_output"):
        print(format_json_output(pairs))
    else:
        print(format_report(pairs), end="", flush=True)

    return 0


def main() -> None:
    sys.exit(run())