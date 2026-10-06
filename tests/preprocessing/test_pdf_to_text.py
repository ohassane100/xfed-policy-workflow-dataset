"""Regression tests for the relocated helper; no new extraction stage."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from xfed.1.pdf_to_text import pdf_to_text


class PdfToTextTests(unittest.TestCase):
    def test_preserves_legacy_output_and_creates_parent_directory(self):
        pages = [Mock(), Mock(), Mock()]
        for page, text in zip(pages, [" First page. ", None, "Second page.\n"]):
            page.extract_text.return_value = text
        reader = Mock(return_value=SimpleNamespace(pages=pages))
        with TemporaryDirectory() as folder:
            source = Path(folder) / "contract.pdf"
            source.touch()
            output = Path(folder) / "nested" / "contract.txt"
            with patch.dict(sys.modules, {"pypdf": SimpleNamespace(PdfReader=reader)}):
                pdf_to_text(source, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "First page.\n\nSecond page.\n")
            reader.assert_called_once_with(str(source))

    def test_empty_extraction_does_not_overwrite_existing_output(self):
        reader = Mock(return_value=SimpleNamespace(pages=[]))
        with TemporaryDirectory() as folder:
            source = Path(folder) / "contract.pdf"
            source.touch()
            output = Path(folder) / "contract.txt"
            output.write_text("existing", encoding="utf-8")
            with patch.dict(sys.modules, {"pypdf": SimpleNamespace(PdfReader=reader)}):
                with self.assertRaisesRegex(ValueError, "No text"):
                    pdf_to_text(source, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "existing")

    def test_missing_pdf_does_not_call_reader(self):
        reader = Mock()
        with TemporaryDirectory() as folder:
            with patch.dict(sys.modules, {"pypdf": SimpleNamespace(PdfReader=reader)}):
                with self.assertRaises(FileNotFoundError):
                    pdf_to_text(Path(folder) / "missing.pdf", Path(folder) / "out.txt")
            reader.assert_not_called()
