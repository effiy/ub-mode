"""UB-Tools — Quality gate tools for the ub-mode software engineering discipline.

Provides three CLI tools:

* ``crapper``  — CRAP score (complexity + coverage analysis)
* ``dryer``    — structural duplicate code detection
* ``mutator``  — mutation testing

All tools are accessible as ``python -m ub_tools.<tool>`` or through the
console_scripts entry points defined in pyproject.toml.
"""

__version__ = "0.1.0"
__all__ = ["crapper", "dryer", "mutator", "shared"]