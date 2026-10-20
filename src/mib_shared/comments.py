"""Static check: no ``#`` comment inside a function body.

Ruff has no rule for it. What a function needs to say goes in its docstring, kept
compact; a comment in the middle of the body drifts from the code it describes
and turns a short function into a long one:

    mib-check-function-comments src/ tests/

Tool directives stay, because they configure a tool rather than describe code:
``# noqa``, ``# type:``, ``# pragma:``, ``# fmt:``, ``# ruff:``, ``# pyright:``,
``# mypy:``, ``# pylint:``, ``# nosec`` and ``# mib: timeout-ok``.
"""
from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from collections.abc import Sequence
from pathlib import Path

from mib_shared.lint import Finding, iter_python_files

_DIRECTIVE = re.compile(
    r"#\s*(noqa|type:|pragma|fmt:|ruff:|pyright:|mypy:|pylint:|nosec|mib:\s*timeout-ok|isort:)"
)


def _function_spans(tree: ast.AST, lines: list[str]) -> list[tuple[int, int]]:
    """Line spans of every function, widened over the comments that end its body."""
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        end = node.end_lineno or node.lineno
        while end < len(lines):
            stripped = lines[end].lstrip()
            indent = len(lines[end]) - len(stripped)
            if stripped == "" or (stripped.startswith("#") and indent > node.col_offset):
                end += 1
            else:
                break
        while end > (node.end_lineno or node.lineno) and not lines[end - 1].strip().startswith("#"):
            end -= 1
        spans.append((node.lineno, end))
    return spans


def check_source(source: str, path: Path) -> list[Finding]:
    """Findings for one file. A syntax error is reported, not swallowed."""
    try:
        tree = ast.parse(source)
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError) as exc:
        return [Finding(path, getattr(exc, "lineno", None) or 1, f"could not parse: {exc}")]

    spans = _function_spans(tree, source.splitlines())
    return [
        Finding(
            path,
            token.start[0],
            "comment inside a function - put it in the docstring, or drop it",
        )
        for token in tokens
        if token.type == tokenize.COMMENT
        and not _DIRECTIVE.match(token.string)
        and any(start <= token.start[0] <= end for start, end in spans)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    targets = argv or ["."]
    findings = []
    for file in iter_python_files(targets):
        findings.extend(check_source(file.read_text(encoding="utf-8"), file))
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"\n{len(findings)} comment(s) inside a function.", file=sys.stderr)
        return 1
    print(f"OK: no comments inside functions in {', '.join(targets)}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
