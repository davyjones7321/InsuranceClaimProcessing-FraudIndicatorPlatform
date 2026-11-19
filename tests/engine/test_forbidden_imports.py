"""AST verification enforcing purity and forbidden imports in app/engine."""

import ast
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent.parent.parent / "app" / "engine"

FORBIDDEN_MODULES = {
    "random",
    "time",
    "uuid",
    "os",
    "sys",
    "requests",
    "urllib",
    "httpx",
    "http",
    "aiohttp",
    "sqlalchemy",
    "psycopg2",
    "asyncpg",
    "sqlite3",
    "fastapi",
    "starlette",
    "pydantic",
}

FORBIDDEN_CALLS = {
    "now",
    "utcnow",
    "today",
}


class PurityASTVisitor(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root_module = alias.name.split(".")[0]
            if root_module in FORBIDDEN_MODULES:
                self.violations.append(
                    f"{self.filename}:{node.lineno}: Forbidden module import '{alias.name}'"
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            root_module = node.module.split(".")[0]
            if root_module in FORBIDDEN_MODULES:
                self.violations.append(
                    f"{self.filename}:{node.lineno}: Forbidden module import '{node.module}'"
                )
            if node.module in ("datetime", "time"):
                for alias in node.names:
                    if alias.name in FORBIDDEN_CALLS:
                        self.violations.append(
                            f"{self.filename}:{node.lineno}: Forbidden temporal import '{alias.name}' from '{node.module}'"
                        )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Check calls like datetime.now(), date.today()
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in FORBIDDEN_CALLS:
                self.violations.append(
                    f"{self.filename}:{node.lineno}: Forbidden non-deterministic call to '.{node.func.attr}()'"
                )
        elif isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                self.violations.append(
                    f"{self.filename}:{node.lineno}: Forbidden non-deterministic function call '{node.func.id}()'"
                )
        self.generic_visit(node)


def test_engine_has_zero_forbidden_imports_or_calls() -> None:
    """Ensure all files in app/engine adhere to strict functional purity constraints."""
    assert ENGINE_DIR.is_dir(), f"Engine directory '{ENGINE_DIR}' not found"

    engine_files = list(ENGINE_DIR.glob("*.py"))
    assert len(engine_files) > 0, "No python files found in app/engine"

    all_violations: list[str] = []
    for file_path in engine_files:
        code = file_path.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(file_path))
        visitor = PurityASTVisitor(filename=file_path.name)
        visitor.visit(tree)
        all_violations.extend(visitor.violations)

    assert not all_violations, "Purity violations found in app/engine:\n" + "\n".join(
        all_violations
    )
