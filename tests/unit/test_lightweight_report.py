"""scripts/lightweight_report.py: sizes on formatted text, `# fmt: skip` only on tables."""

import ast
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/lightweight_report.py"
RUFF = shutil.which("ruff") or str(Path(sys.executable).with_name("ruff"))


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lightweight_report", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["lightweight_report"] = module
    spec.loader.exec_module(module)
    return module


lr = load_script()


def first_statement(source: str) -> ast.stmt:
    return ast.parse(source).body[0]


@pytest.mark.parametrize(
    "source",
    [
        'KEYS = (\n    "a", "b",\n    "c",\n)\n',
        'KEYS: frozenset[str] = frozenset(\n    {"a", "b",\n     "c"}\n)\n',
        "__all__ = [\n    'x', 'y',\n]\n",
        "BUCKETS = (\n    1, 2.5, -3,\n    None, True,\n)\n",
        "ALLOWED = {\n    CHAT: _CHAT_KEYS, SPEECH: keys.SPEECH,\n    **EXTRA,\n}\n",
        "KEYS = frozenset(\n    {'model', *_COMMON,\n     ('nested', 1)}\n)\n",
    ],
)
def test_a_multi_line_assignment_of_literals_is_a_table(source: str) -> None:
    assert lr.is_table(first_statement(source))


@pytest.mark.parametrize(
    "source",
    [
        'KEYS = ("a", "b")\n',  # one line: nothing to keep packed
        "x = f(\n    a, b,\n)\n",  # a call
        "x = [\n    f(1), 2,\n]\n",  # an item that is a call
        "x = [\n    a + b, 2,\n]\n",  # an item that is an expression
        "x = [\n    i for i in range(3)\n]\n",  # a comprehension
        "x = tuple(\n    [1, 2,\n     3]\n)\n",  # only frozenset wraps a table
        "x = frozenset(\n    {1, 2,\n     3}, key=1\n)\n",
        "x += [\n    1, 2,\n]\n",  # not a plain assignment
        "f(\n    a, b,\n)\n",
        "x: (\n    Literal['a', 'b']\n    | None\n) = None\n",  # a type, not a table
        "REG[compute(a, b,\n    c)] = [1,\n    2]\n",  # a subscript target holding a call
        "x, y = [\n    1, 2,\n]\n",  # a tuple target
        "x: Annotated[int, f(a,\n    b)] = [1,\n    2]\n",  # a call in the annotation
        "x: int | None = [\n    1, 2,\n]\n",  # an annotation that is an expression
    ],
)
def test_anything_else_is_not_a_table(source: str) -> None:
    assert not lr.is_table(first_statement(source))


def test_a_table_keeps_its_skip() -> None:
    source = 'KEYS = (\n    "a", "b",\n    "c",\n)  # fmt: skip\n'
    cleaned, misused = lr.honest_source(Path("m.py"), source)
    assert cleaned == source
    assert misused == []


@pytest.mark.parametrize(
    "source",
    [
        'class C:\n    KEYS: tuple[str, ...] = (\n        "a", "b",\n    )  # fmt: skip\n',
        "class A:\n    class B:\n        X = [\n            1, 2,\n        ]  # fmt: skip\n",
        "if True:\n    class A:\n        X = [\n            1, 2,\n        ]  # fmt: skip\n",
        "X = [\n    1, 2,\n]  # fmt: skip\ny = foo(alpha,   beta)\n",
    ],
)
def test_a_table_at_module_or_class_level_keeps_its_skip(source: str) -> None:
    assert lr.honest_source(Path("m.py"), source)[1] == []


@pytest.mark.parametrize(
    "source",
    [
        # Ruff leaves every line of a skipped statement unformatted: nothing may share them.
        "a = 1; b = 2; c = foo(a,  b); X = [1,\n 2]  # fmt: skip\n",
        "foo(alpha, beta,\n epsilon); X = [1,\n2]  # fmt: skip\n",
        "X = [1,\n 2]; foo(alpha,   beta)  # fmt: skip\n",
        "if check(alpha,   beta,\n epsilon): X = [1,\n 2]  # fmt: skip\n",
        "class C: X = [1,\n 2]  # fmt: skip\n",
        # A backslash continuation joins the previous statements to the table's logical line.
        "a = foo(alpha,   beta,  gamma); b = bar(  1  ); \\\nX = [\n    1, 2,\n]  # fmt: skip\n",
        "class A:\n    a = foo(alpha,   beta); \\\n    X = [\n        1, 2,\n    ]  # fmt: skip\n",
        "x = 1; \\\ny = foo(alpha,   beta); \\\nX = [\n    1, 2,\n]  # fmt: skip\n",
        # A class inside a function is still inside the function.
        "def f():\n    class T:\n        X = [\n            1, 2,\n        ]  # fmt: skip\n",
        # Only at module or class level: a table inside a function is formatted.
        "def f():\n    X = [\n        1, 2,\n    ]  # fmt: skip\n",
        "def f(alpha,   beta,\n      epsilon): X = [1,\n     2]  # fmt: skip\n",
        "def f():\n    a = 1; b = foo(a,  b); X = [1,\n     2]  # fmt: skip\n",
        "def f():\n    if check(alpha,   beta,\n      epsilon): X = [1,\n     2]  # fmt: skip\n",
        # A subscript target or a call in the annotation would escape formatting with the table.
        "REG[compute(alpha,   beta,\n  gamma)] = [1,\n 2]  # fmt: skip\n",
        "x: Annotated[int, compute(alpha,   beta,\n   gamma)] = [1,\n 2]  # fmt: skip\n",
    ],
)
def test_a_table_sharing_lines_or_inside_a_function_loses_its_skip(source: str) -> None:
    cleaned, misused = lr.honest_source(Path("m.py"), source)
    assert len(misused) == 1 and misused[0].endswith("fmt: skip on something that is not a table")
    assert "fmt: skip" not in cleaned


def test_a_skip_outside_a_table_is_removed_and_reported() -> None:
    source = (
        "def f(\n"
        "    a: int, b: int,\n"
        ") -> int:  # fmt: skip\n"
        "    return g(\n"
        "        a, b,\n"
        "    )  # noqa: X  # fmt: skip\n"
    )
    cleaned, misused = lr.honest_source(Path("m.py"), source)
    assert "fmt: skip" not in cleaned
    assert ") -> int:\n" in cleaned
    assert "    )  # noqa: X\n" in cleaned
    assert misused == [
        "m.py:3 fmt: skip on something that is not a table",
        "m.py:6 fmt: skip on something that is not a table",
    ]


def test_a_fmt_off_region_is_reported() -> None:
    source = "# fmt: off\nx = [\n    1, 2,\n]\n# fmt: on\n"
    _, misused = lr.honest_source(Path("m.py"), source)
    assert misused == ["m.py:1 fmt: off/on region", "m.py:5 fmt: off/on region"]


def test_sizes_are_counted_on_the_formatted_text(tmp_path: Path) -> None:
    path = tmp_path / "packed.py"
    path.write_text("def f(a, b):\n    return g(\n        a, b,\n    )  # fmt: skip\n")
    measured = lr.measure(path, RUFF, [])
    assert lr.code_lines(measured.text) == 5  # `a,` and `b,` on lines of their own
    assert measured.misused == [f"{path}:4 fmt: skip on something that is not a table"]
    assert path.read_text().endswith("# fmt: skip\n")  # the source is never modified


def test_a_file_the_ruff_configuration_excludes_is_still_formatted(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nextend-exclude = ["gen/*"]\nforce-exclude = true\n'
    )
    (tmp_path / "gen").mkdir()
    path = tmp_path / "gen" / "packed.py"
    path.write_text("x = g(\n    a, b,\n)\n")
    assert lr.code_lines(lr.measure(path, RUFF, []).text) == 4


def run_report(
    tmp_path: Path, source: str, ruff: str = RUFF, *limits: str, tool: str = ""
) -> subprocess.CompletedProcess[str]:
    package = tmp_path / "pkg"
    package.mkdir(exist_ok=True)
    (package / "mod.py").write_text(source)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "pkg"\ndependencies = []\n' + tool)
    command = [sys.executable, str(SCRIPT), "--src", str(package), "--pyproject", str(pyproject)]
    return subprocess.run(  # noqa: S603 - this repository's own script
        [*command, "--ruff", ruff, *limits], capture_output=True, text=True, check=False
    )


def test_the_report_fails_on_a_skip_outside_a_table(tmp_path: Path) -> None:
    done = run_report(tmp_path, "x = f(\n    1, 2,\n)  # fmt: skip\n")
    assert done.returncode == 1
    assert "mod.py:3 fmt: skip on something that is not a table" in done.stdout
    assert "FAIL: formatter directives" in done.stdout


def test_the_report_passes_with_skips_only_on_tables(tmp_path: Path) -> None:
    done = run_report(tmp_path, 'KEYS = (\n    "a", "b",\n)  # fmt: skip\n')
    assert done.returncode == 0, done.stdout + done.stderr
    assert "OK: hard targets hold" in done.stdout


def test_the_report_stops_when_ruff_cannot_run(tmp_path: Path) -> None:
    done = run_report(tmp_path, "x = 1\n", ruff=str(tmp_path / "no-ruff"))
    assert done.returncode == 2
    assert "cannot measure formatted sizes" in done.stderr


def test_a_module_over_its_limit_fails_the_report(tmp_path: Path) -> None:
    done = run_report(tmp_path, "x = f(\n    1, 2,\n)\n", RUFF, "--max-module-lines", "3")
    assert done.returncode == 1
    assert "mod.py: 4" in done.stdout and "FAIL: module lines" in done.stdout


def test_a_function_over_its_limit_fails_the_report(tmp_path: Path) -> None:
    source = "def f(a, b):\n    return g(\n        a, b,\n    )  # fmt: skip\n"
    done = run_report(tmp_path, source, RUFF, "--max-function-lines", "4")
    assert done.returncode == 1
    assert "mod.py:1 f: 5" in done.stdout and "function lines" in done.stdout


def test_the_root_line_length_wins_over_a_nearer_configuration(tmp_path: Path) -> None:
    long_call = "x = f(" + ", ".join(f"a{i}" for i in range(12)) + ")\n"  # 62 characters
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "pyproject.toml").write_text("[tool.ruff]\nline-length = 200\n")
    root = "[tool.ruff]\nline-length = 40\n"
    done = run_report(tmp_path, long_call, RUFF, "--max-module-lines", "1", tool=root)
    assert done.returncode == 1 and "FAIL: module lines" in done.stdout


def test_a_ruff_configuration_under_the_sources_stops_the_report(tmp_path: Path) -> None:
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "ruff.toml").write_text("line-length = 400\n")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "pkg"\ndependencies = []\n')
    command = [sys.executable, str(SCRIPT), "--src", str(tmp_path / "pkg")]
    done = subprocess.run(  # noqa: S603 - this repository's own script
        [*command, "--pyproject", str(pyproject), "--ruff", RUFF],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2
    assert "ruff configuration under the sources" in done.stderr
