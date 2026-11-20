"""Lightweight report: checks the size, module and dependency targets.

Standard library only.

    uv run python scripts/lightweight_report.py
    python3 scripts/lightweight_report.py --src path/to/package --pyproject path/to/pyproject.toml

Exit code 1 if a hard target is exceeded (product lines, adapter lines, runtime dependencies).
Module and function outliers are reported as warnings: the limits are review guidelines.
"""

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path


def code_lines(path: Path) -> int:
    """Count non-blank physical lines (comments included: they are part of what is read)."""
    return sum(1 for line in path.read_text("utf-8").splitlines() if line.strip())


def long_functions(path: Path, limit: int, unparsable: list[str]) -> list[tuple[str, int]]:
    """Return (name, length) for each function or method longer than `limit` lines.

    A file that does not parse under the running Python (for example newer syntax) is
    recorded in `unparsable` and skipped; the report continues.
    """
    found: list[tuple[str, int]] = []
    try:
        tree = ast.parse(path.read_text("utf-8"))
    except SyntaxError as exc:
        unparsable.append(f"{path.name}:{exc.lineno} {exc.msg}")
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.end_lineno:
            length = node.end_lineno - node.lineno + 1
            if length > limit:
                found.append((f"{path.name}:{node.lineno} {node.name}", length))
    return found


def _name(requirement: str) -> str:
    """Normalised distribution name of a requirement string such as 'httpx>=0.27'."""
    return re.split(r"[\s<>=!~;\[(]", requirement.strip(), maxsplit=1)[0].lower().replace("_", "-")


def transitive_dependencies(pyproject: Path, project: dict[str, object]) -> int | None:
    """Count distinct packages reachable from the base dependencies, read from a sibling uv.lock.

    Returns None when no lock file (or no root package in it) is available: the count is
    informational, so we never resolve or install anything to get it.
    """
    lock_path = pyproject.with_name("uv.lock")
    if not lock_path.exists():
        return None
    packages = tomllib.loads(lock_path.read_text("utf-8")).get("package", [])
    graph = {
        _name(str(p["name"])): [_name(str(d["name"])) for d in p.get("dependencies", [])]
        for p in packages
    }
    root = _name(str(project.get("name", "")))
    if root not in graph:
        return None
    seen: set[str] = set()
    todo = list(graph[root])
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo.extend(graph.get(name, []))
    return len(seen)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report product size against the lightweight targets."
    )
    parser.add_argument("--src", type=Path, default=Path("src/spendgate"), help="package directory")
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--providers", default="providers", help="adapter sub-directory of --src")
    parser.add_argument("--max-adapter-lines", type=int, default=400)
    parser.add_argument("--max-module-lines", type=int, default=400)
    parser.add_argument("--max-function-lines", type=int, default=50)
    parser.add_argument("--max-dependencies", type=int, default=6)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    files = sorted(p for p in args.src.rglob("*.py") if "__pycache__" not in p.parts)
    adapters = [p for p in files if args.providers in p.relative_to(args.src).parts[:-1]]
    product = [p for p in files if p not in adapters]
    failures: list[str] = []

    product_lines = sum(code_lines(p) for p in product)
    # Reported for information only: the total-size limit was dropped (ADR-0007).
    print(f"Product lines (no {args.providers}/): {product_lines}")

    print("Provider adapters:")
    for path in adapters:
        lines = code_lines(path)
        print(f"  {path.name}: {lines} (max {args.max_adapter_lines})")
        if lines > args.max_adapter_lines:
            failures.append(f"adapter {path.name}")
    if not adapters:
        print("  none found")

    print(f"Modules over {args.max_module_lines} lines (warning):")
    big = [(p.relative_to(args.src), code_lines(p)) for p in files]
    big = [(name, n) for name, n in big if n > args.max_module_lines]
    for name, n in big:
        print(f"  {name}: {n}")
    print(f"  total: {len(big)}")

    print(f"Functions over {args.max_function_lines} lines (warning):")
    unparsable: list[str] = []
    outliers = [
        item for p in files for item in long_functions(p, args.max_function_lines, unparsable)
    ]
    for name, n in outliers:
        print(f"  {name}: {n}")
    print(f"  total: {len(outliers)}")
    if unparsable:
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        print(f"Files not parsed by Python {version} (skipped):")
        for item in unparsable:
            print(f"  {item}")

    project = tomllib.loads(args.pyproject.read_text("utf-8")).get("project", {})
    deps = len(project.get("dependencies", []))
    print(f"Runtime dependencies (base install): {deps} (max {args.max_dependencies})")
    if deps > args.max_dependencies:
        failures.append("runtime dependencies")
    total = transitive_dependencies(args.pyproject, project)
    if total is None:
        print("Transitive dependencies (info): skipped, no uv.lock with the root package")
    else:
        print(f"Transitive dependencies (info, from uv.lock): {total}")

    print("FAIL: " + ", ".join(failures) if failures else "OK: hard targets hold")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
