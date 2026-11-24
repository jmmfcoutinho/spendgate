"""Lightweight report: checks the size, module and dependency targets.

Standard library only, plus the `ruff` executable the project already formats with.

    uv run python scripts/lightweight_report.py
    python3 scripts/lightweight_report.py --src path/to/package --pyproject path/to/pyproject.toml

Sizes are measured on the ruff-formatted text, so packing lines cannot meet a limit: each file is
formatted in memory (the sources are never modified) after removing every `# fmt: skip` that does
not mark a table standing on its own logical lines at module or class level (see `is_table`
and `placed_tables`), with the root project's ruff line length. Such a directive, and any
`# fmt: off`/`# fmt: on` region, is itself a failure.

Exit code 1 if a target is exceeded: module, function or adapter lines, runtime dependencies or
formatter directives (ADR-0007). Exit code 2 if ruff cannot be run, or a `ruff.toml` under the
sources would format them by other rules: the sizes would not be honest.
"""

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_SKIP = re.compile(r"\s*#\s*fmt:\s*skip\b")
_REGION = re.compile(r"#\s*(fmt:\s*(off|on)|yapf:\s*(disable|enable))\b")
_TABLE_WRAPPERS = frozenset({"frozenset"})


@dataclass
class Measured:
    """A file's formatted text and the formatter directives it may not carry."""

    path: Path
    text: str
    misused: list[str] = field(default_factory=list)


def _is_reference(node: ast.expr) -> bool:
    """A name or dotted name, such as `_CHAT_KEYS` or `ErrorCode.BUDGET_EXCEEDED`."""
    if isinstance(node, ast.Attribute):
        return _is_reference(node.value)
    return isinstance(node, ast.Name)


def _is_literal(node: ast.expr) -> bool:
    """A constant, a signed number, a reference, `*reference`, or a display of literals."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        return isinstance(node.operand, ast.Constant)
    if isinstance(node, ast.Starred):
        return _is_reference(node.value)
    return _is_reference(node) or _is_display(node)


def _is_display(node: ast.expr) -> bool:
    """A list, tuple, set or dict display whose items are all literals (`**name` allowed)."""
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return all(_is_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        keys = [k for k in node.keys if k is not None]
        return all(_is_literal(item) for item in [*keys, *node.values])
    return False


def _is_annotation(node: ast.expr) -> bool:
    """Names, dotted names and subscripts of them, such as `Mapping[str, frozenset[str]]`."""
    if isinstance(node, ast.Subscript):
        inner = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        return _is_reference(node.value) and all(_is_annotation(i) for i in inner)
    if isinstance(node, ast.Constant):
        return node.value is Ellipsis  # `tuple[float, ...]`
    return _is_reference(node)


def is_table(statement: ast.stmt) -> bool:
    """True for the only kind of statement a `# fmt: skip` may mark: a table.

    A table is an assignment (`=` or annotated) spanning more than one line, to names or dotted
    names, under an annotation of names, dotted names and subscripts of them, whose value is a
    list, tuple, set or dict display, or `frozenset(<display>)`, made only of constants, signed
    numbers, names, dotted names, `*name`, `**name` and nested displays of the same. Packing such
    items several to a line keeps a long allowlist readable; anything else is formatted normally.
    Where the table may stand is checked by `placed_tables`.
    """
    if isinstance(statement, ast.Assign):
        targets_ok = all(_is_reference(t) for t in statement.targets)
    elif isinstance(statement, ast.AnnAssign):
        targets_ok = _is_reference(statement.target) and _is_annotation(statement.annotation)
    else:
        return False
    value = statement.value
    if not targets_ok or value is None or statement.end_lineno == statement.lineno:
        return False
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in _TABLE_WRAPPERS
        and len(value.args) == 1
        and not value.keywords
    ):
        value = value.args[0]
    return _is_display(value)


@dataclass
class _Tokens:
    """What the tokens of one file say about formatter directives and logical lines."""

    skips: list[int] = field(default_factory=list)  # lines with a `# fmt: skip` comment
    regions: list[int] = field(default_factory=list)  # lines with a `fmt: off/on` comment
    starts: set[tuple[int, int]] = field(default_factory=set)  # where logical lines begin


def _tokens(source: str) -> _Tokens:
    """Directive comments, and the (line, column) of the first token of every logical line.

    A token after `;` or after a backslash continuation does not start a logical line.
    """
    found = _Tokens()
    at_start = True
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            if _SKIP.search(token.string):
                found.skips.append(token.start[0])
            if _REGION.search(token.string):
                found.regions.append(token.start[0])
        elif token.type in (tokenize.NEWLINE, tokenize.NL):
            at_start = True
        elif token.type not in (tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
            if at_start:
                found.starts.add(token.start)
            at_start = False
    return found


def _table_bodies(body: list[ast.stmt]) -> list[list[ast.stmt]]:
    """`body` and the body of every class under it; never a function's body."""
    bodies = [body]
    for node in body:
        bodies += _class_bodies(node)
    return bodies


def _class_bodies(node: ast.stmt) -> list[list[ast.stmt]]:
    """Class bodies in or under `node`, looking through `if`, `try`, `with` and the like."""
    if isinstance(node, ast.ClassDef):
        return _table_bodies(node.body)
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return []
    found: list[list[ast.stmt]] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.stmt):
            found += _class_bodies(child)
        elif isinstance(child, ast.ExceptHandler | ast.match_case):
            for inner in child.body:
                found += _class_bodies(inner)
    return found


def placed_tables(tree: ast.Module, starts: set[tuple[int, int]]) -> set[int]:
    """Last lines of the tables a skip may mark: at module or class level, alone on their lines.

    Ruff leaves every line a skipped statement touches unformatted, so a table may share none
    with another statement: it starts a logical line (not after `;` or a backslash
    continuation), and no sibling begins or ends on its lines. A table in a function body, or in
    a class defined inside one, is formatted like any other code.
    """
    ends: set[int] = set()
    for body in _table_bodies(tree.body):
        for index, node in enumerate(body):
            before = (body[index - 1].end_lineno or 0) if index > 0 else 0
            after = body[index + 1].lineno if index + 1 < len(body) else None
            alone = before < node.lineno and (after is None or after > (node.end_lineno or 0))
            starts_line = (node.lineno, node.col_offset) in starts
            if alone and starts_line and is_table(node) and node.end_lineno is not None:
                ends.add(node.end_lineno)
    return ends


def honest_source(path: Path, source: str) -> tuple[str, list[str]]:
    """Return `source` without the skips that do not mark a table, and where each misuse is.

    Raises SyntaxError when the file does not parse under the running Python.
    """
    tree = ast.parse(source)
    tokens = _tokens(source)
    misused = [f"{path}:{line} fmt: off/on region" for line in tokens.regions]
    lines = source.splitlines(keepends=True)
    tables = placed_tables(tree, tokens.starts)
    for line in tokens.skips:
        if line in tables:
            continue
        misused.append(f"{path}:{line} fmt: skip on something that is not a table")
        lines[line - 1] = _SKIP.sub("", lines[line - 1], count=1)
    return "".join(lines), misused


def ruff_format(path: Path, source: str, ruff: str, line_length: int | None = None) -> str:
    """Format `source` as ruff would format `path` (the project's configuration applies).

    `--no-force-exclude`: a file the configuration excludes from formatting is still measured.
    `line_length` is the root project's, so no nearer configuration can widen the lines.
    """
    width = ["--line-length", str(line_length)] if line_length is not None else []
    command = [ruff, "format", "--quiet", "--no-force-exclude", *width]
    done = subprocess.run(  # noqa: S603 - a fixed executable; the text goes on stdin
        [*command, "--stdin-filename", str(path), "-"],
        input=source,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"ruff could not format {path}: {done.stderr.strip()}")
    return done.stdout


def measure(
    path: Path, ruff: str, unparsable: list[str], line_length: int | None = None
) -> Measured:
    """Format one file honestly; an unparsable file is recorded and measured as it stands."""
    source = path.read_text("utf-8")
    try:
        cleaned, misused = honest_source(path, source)
    except SyntaxError as exc:
        unparsable.append(f"{path.name}:{exc.lineno} {exc.msg}")
        return Measured(path, source)
    return Measured(path, ruff_format(path, cleaned, ruff, line_length), misused)


def code_lines(text: str) -> int:
    """Count non-blank physical lines (comments included: they are part of what is read)."""
    return sum(1 for line in text.splitlines() if line.strip())


def long_functions(measured: Measured, limit: int) -> list[tuple[str, int]]:
    """Return (name, length) for each function or method longer than `limit` formatted lines."""
    found: list[tuple[str, int]] = []
    try:
        tree = ast.parse(measured.text)
    except SyntaxError:
        return found  # already listed as not parsed
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.end_lineno:
            length = node.end_lineno - node.lineno + 1
            if length > limit:
                found.append((f"{measured.path.name}:{node.lineno} {node.name}", length))
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
    parser.add_argument("--ruff", default="ruff", help="ruff executable used to format")
    parser.add_argument("--max-adapter-lines", type=int, default=400)
    parser.add_argument("--max-module-lines", type=int, default=400)
    parser.add_argument("--max-function-lines", type=int, default=50)
    parser.add_argument("--max-dependencies", type=int, default=6)
    return parser.parse_args()


def report_sizes(args: argparse.Namespace, files: list[Measured], failures: list[str]) -> None:
    """Print the formatted line counts; a module, function or adapter over its limit fails."""
    adapters = [m for m in files if args.providers in m.path.relative_to(args.src).parts[:-1]]
    product_lines = sum(code_lines(m.text) for m in files if m not in adapters)
    # Reported for information only: the total-size limit was dropped (ADR-0007).
    print(f"Product lines (no {args.providers}/): {product_lines}")

    print("Provider adapters:")
    for m in adapters:
        lines = code_lines(m.text)
        print(f"  {m.path.name}: {lines} (max {args.max_adapter_lines})")
        if lines > args.max_adapter_lines:
            failures.append(f"adapter {m.path.name}")
    if not adapters:
        print("  none found")

    print(f"Modules over {args.max_module_lines} lines (hard: none):")
    big = [(m.path.relative_to(args.src), code_lines(m.text)) for m in files]
    big = [(name, n) for name, n in big if n > args.max_module_lines]
    for name, n in big:
        print(f"  {name}: {n}")
    print(f"  total: {len(big)}")
    if big:
        failures.append("module lines")

    print(f"Functions over {args.max_function_lines} lines (hard: none):")
    outliers = [item for m in files for item in long_functions(m, args.max_function_lines)]
    for where, n in outliers:
        print(f"  {where}: {n}")
    print(f"  total: {len(outliers)}")
    if outliers:
        failures.append("function lines")


def report_directives(files: list[Measured], failures: list[str]) -> None:
    """Print each formatter directive that does not mark a table; any one is a hard failure."""
    misused = [item for m in files for item in m.misused]
    print("Formatter directives outside tables (hard: none):")
    for item in misused:
        print(f"  {item}")
    print(f"  total: {len(misused)}")
    if misused:
        failures.append("formatter directives")


def report_dependencies(args: argparse.Namespace, failures: list[str]) -> None:
    """Print the base and transitive dependency counts; too many base ones is a hard failure."""
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


def line_length(pyproject: Path, src: Path) -> int | None:
    """The root project's `[tool.ruff] line-length`; a ruff file under `src` is refused.

    A `ruff.toml` or `.ruff.toml` beside the sources would format them under other rules than
    the project's, so the sizes would not be the ones `ruff format --check` holds them to.
    """
    nested = sorted(p for name in ("ruff.toml", ".ruff.toml") for p in src.rglob(name))
    if nested:
        raise RuntimeError(f"a ruff configuration under the sources: {nested[0]}")
    tool = tomllib.loads(pyproject.read_text("utf-8")).get("tool", {})
    width = tool.get("ruff", {}).get("line-length")
    return width if isinstance(width, int) else None


def main() -> int:
    args = parse_args()
    paths = sorted(p for p in args.src.rglob("*.py") if "__pycache__" not in p.parts)
    unparsable: list[str] = []
    try:
        width = line_length(args.pyproject, args.src)
        files = [measure(p, args.ruff, unparsable, width) for p in paths]
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: cannot measure formatted sizes: {exc}", file=sys.stderr)
        return 2
    failures: list[str] = []
    report_sizes(args, files, failures)
    if unparsable:
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        print(f"Files not parsed by Python {version} (measured as they stand):")
        for item in unparsable:
            print(f"  {item}")
    report_directives(files, failures)
    report_dependencies(args, failures)
    print("FAIL: " + ", ".join(failures) if failures else "OK: hard targets hold")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
