"""Check that the shared package imports without model or PDF dependencies."""

import subprocess
import sys
import unittest


class PackageTests(unittest.TestCase):
    def test_imports_without_optional_dependencies_or_checkout_cwd(self):
        code = """
import importlib
import sys

class RejectOptionalDependencies:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pypdf', 'fitz', 'pymupdf', 'ollama', 'yaml'}:
            raise AssertionError('Unexpected import: ' + fullname)

sys.meta_path.insert(0, RejectOptionalDependencies())
for name in ('xfed', 'xfed.preprocessing', 'xfed.preprocessing.pdf_to_text',
             'xfed.segmentation', 'xfed.relevance', 'xfed.enrichment',
             'xfed.policy_generation', 'xfed.policy_review', 'xfed.llm',
             'xfed.common'):
    importlib.import_module(name)
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", code], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
