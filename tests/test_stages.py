"""Discover tests in the numbered stage folders, which are not Python packages."""

import importlib.util
from pathlib import Path


def load_tests(loader, suite, pattern):
    for path in sorted((Path(__file__).parent / '1 preprocessing').rglob('test_*.py')):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite
