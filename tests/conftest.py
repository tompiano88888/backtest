"""Fail if an editable install elsewhere silently supplies a missing local module."""
from pathlib import Path
import sys

import pytest


@pytest.fixture(scope='session', autouse=True)
def framework_imports_are_self_contained():
    yield
    package = Path(__file__).resolve().parents[1] / 'backtest'
    for name, module in list(sys.modules.items()):
        if name == 'backtest' or name.startswith('backtest.'):
            source = getattr(module, '__file__', None)
            if source:
                assert Path(source).resolve().is_relative_to(package), (name, source)
