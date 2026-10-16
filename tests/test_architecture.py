"""Enforces the module boundaries from docs/plans/2026-10-16-module-structure.md."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

MODULES = {
    "api",
    "conversation",
    "booking",
    "providers",
    "offers",
    "events",
    "persistence",
    "security",
    "observability",
}
# Logic modules may import logic from these modules (and any data module).
ALLOWED = {
    "observability": set(),
    "security": {"observability"},
    "providers": {"observability"},
    "booking": {"providers", "observability"},
    "offers": {"booking", "providers", "observability"},
    "events": {"booking", "offers", "providers", "persistence", "observability"},
    "conversation": {"booking", "offers", "providers", "observability"},
    "persistence": {"observability"},
    "api": MODULES,
}
DATA_OK = {"app.config", "app.errors", "app.schemas"}  # always importable
TOP_LEVEL_COMPOSITION = {"main.py", "contract"}  # may wire everything / re-export models


def files():
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def module_name(path: Path) -> str:
    return ".".join(("app", *path.relative_to(APP).with_suffix("").parts)).removesuffix(".__init__")


def package_of(path: Path) -> str | None:
    parts = path.relative_to(APP).parts
    return parts[0] if parts[0] in MODULES else None


def imports(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.startswith("app"):
                # `from app.x import y` where y is a module counts as app.x.y
                found.append(node.module)
                found += [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.startswith("app")]
    return found


def resolve(name: str) -> Path | None:
    """The file an `app.…` name refers to, if it is a module (not a symbol)."""
    base = APP.joinpath(*name.split(".")[1:])
    if base.with_suffix(".py").exists():
        return base.with_suffix(".py")
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    return None


def is_data(path: Path) -> bool:
    return path.name == "models.py" or module_name(path) in DATA_OK


def targets(path: Path) -> set[Path]:
    return {p for n in imports(path) if (p := resolve(n)) and p != path}


def test_data_modules_import_only_data() -> None:
    for path in files():
        if path.name != "models.py":
            continue
        for target in targets(path):
            assert is_data(target) or target.name == "__init__.py", (
                f"{module_name(path)} (data) imports logic module {module_name(target)}"
            )


def test_logic_modules_respect_the_allowed_dependencies() -> None:
    for path in files():
        package = package_of(path)
        if package is None or is_data(path):
            continue
        for target in targets(path):
            other = package_of(target)
            if other is None or other == package or is_data(target) or target.name == "__init__.py":
                continue
            assert other in ALLOWED[package], (
                f"{module_name(path)} may not depend on {module_name(target)} "
                f"({package} -> {other} is not allowed)"
            )


def test_no_cycles_between_logic_modules() -> None:
    graph: dict[str, set[str]] = {m: set() for m in MODULES}
    for path in files():
        package = package_of(path)
        if package is None or is_data(path):
            continue
        for target in targets(path):
            other = package_of(target)
            if other and other != package and not is_data(target) and target.name != "__init__.py":
                graph[package].add(other)

    def visit(node: str, trail: tuple[str, ...]) -> None:
        assert node not in trail, f"import cycle: {' -> '.join((*trail, node))}"
        for nxt in graph[node]:
            visit(nxt, (*trail, node))

    for module in graph:
        visit(module, ())


def test_package_init_files_do_not_import_logic() -> None:
    for module in MODULES - {"api"}:  # api/v1/__init__ assembles the routers
        for init in (APP / module).rglob("__init__.py"):
            assert not imports(init), f"{module_name(init)} must stay free of app imports"


def test_the_extraction_boundary_cannot_reach_side_effects() -> None:
    """A model-backed extractor lives in conversation/extract.py. It may produce
    data only: no workflow, offer, operation or provider execution paths."""
    extract = APP / "conversation" / "extract.py"
    for target in targets(extract):
        assert is_data(target), f"extract.py imports non-data module {module_name(target)}"


def test_every_module_exists_with_a_purpose_docstring() -> None:
    for module in MODULES:
        init = APP / module / "__init__.py"
        assert init.exists(), f"app/{module}/ is missing"
        assert ast.get_docstring(ast.parse(init.read_text())), (
            f"app/{module}/__init__.py needs a docstring"
        )
