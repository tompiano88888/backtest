import ast
from pathlib import Path

from scripts.check_framework_scope import check


def test_delivery_is_only_explicitly_reviewed_framework_files():
    root = Path(__file__).resolve().parents[1]
    assert check(root) > 0


def test_framework_relative_imports_resolve_without_research():
    package = Path(__file__).resolve().parents[1]/'backtest'
    modules = {p.stem for p in package.glob('*.py')}
    for path in package.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                assert node.module.split('.')[0] in modules, (path, node.module)
            if isinstance(node, ast.Import):
                assert all(n.name.split('.')[0] not in {'research', 'strategies', 'scripts'} for n in node.names)
            if isinstance(node, ast.ImportFrom) and not node.level and node.module:
                assert node.module.split('.')[0] not in {'research', 'strategies', 'scripts'}
