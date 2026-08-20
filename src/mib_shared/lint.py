"""Static check: no outbound HTTP call without a timeout (FR-BE-21).

Ruff cannot express this, and code review misses it exactly once — which is all
it takes for one slow dependency to hold a worker thread until the process is
restarted. So it is a build failure instead:

    mib-check-timeouts src/ app/

Run it from every service's CI. An intentional exception is marked in the source
with ``# mib: timeout-ok`` on the offending line, which keeps the decision next
to the code rather than in a config file nobody reads.
"""
from __future__ import annotations

import ast
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Module-level functions that perform a request with no client to configure.
_REQUEST_FUNCS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}
)
# Clients whose timeout, once set, applies to every call made through them.
_CLIENT_CLASSES = frozenset({"Client", "AsyncClient"})
_HTTP_MODULES = frozenset({"httpx", "requests"})

SUPPRESSION = "mib: timeout-ok"


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _qualname(node: ast.AST) -> str | None:
    """Dotted name for ``httpx.get`` / ``httpx.AsyncClient`` style attributes."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _has_timeout(call: ast.Call) -> bool:
    if any(kw.arg == "timeout" for kw in call.keywords):
        return True
    # **kwargs forwarding — the caller may be passing a timeout through, and
    # flagging every wrapper would train people to suppress the check.
    return any(kw.arg is None for kw in call.keywords)


def check_source(source: str, path: Path) -> list[Finding]:
    """Findings for one file. A syntax error is reported, not swallowed."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 1, f"could not parse: {exc.msg}")]

    suppressed = {
        i + 1 for i, line in enumerate(source.splitlines()) if SUPPRESSION in line
    }
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _qualname(node.func)
        if not name or "." not in name:
            continue
        module, _, attr = name.rpartition(".")
        if module not in _HTTP_MODULES:
            continue
        if attr in _CLIENT_CLASSES:
            kind = f"{name}(...)"
        elif attr in _REQUEST_FUNCS:
            kind = f"{name}()"
        else:
            continue
        if _has_timeout(node) or node.lineno in suppressed:
            continue
        findings.append(
            Finding(
                path,
                node.lineno,
                f"{kind} has no timeout - every outbound call needs one (FR-BE-21). "
                "Prefer mib_shared.TracedAsyncClient, which also carries the "
                "bounded retry and traceparent propagation.",
            )
        )
    return sorted(findings, key=lambda f: (str(f.path), f.line))


def iter_python_files(targets: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        path = Path(target)
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.py") if ".venv" not in p.parts))
        elif path.suffix == ".py":
            files.append(path)
    return files


def check_paths(targets: Sequence[str]) -> list[Finding]:
    findings: list[Finding] = []
    for file in iter_python_files(targets):
        findings.extend(check_source(file.read_text(encoding="utf-8"), file))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    targets = argv or ["."]
    findings = check_paths(targets)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(
            f"\n{len(findings)} outbound call(s) without a timeout.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: no untimed outbound calls in {', '.join(targets)}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
