"""Optional document converter providers for Phase 16 ingestion."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path


class MissingDocumentConverter(RuntimeError):
    """Raised when neither Docling nor MarkItDown is installed."""


@dataclass(frozen=True)
class ConversionResult:
    markdown: str
    provider: str


_INSTALL_HINT = "Document ingest needs MarkItDown. Install with: mastisk[ingest]"


def document_converter_available() -> bool:
    return _module_available("docling.document_converter") or _module_available("markitdown")


def convert_document(path: Path) -> ConversionResult:
    """Convert a local document to Markdown using Docling or MarkItDown."""
    try:
        return _convert_with_docling(path)
    except ImportError:
        pass

    try:
        return _convert_with_markitdown(path)
    except ImportError as exc:
        raise MissingDocumentConverter(_INSTALL_HINT) from exc


def _module_available(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


def _convert_with_docling(path: Path) -> ConversionResult:
    module = importlib.import_module("docling.document_converter")
    result = module.DocumentConverter().convert(str(path))
    document = getattr(result, "document", result)
    if not hasattr(document, "export_to_markdown"):
        raise RuntimeError("Docling result does not expose export_to_markdown()")
    markdown = str(document.export_to_markdown()).strip()
    if not markdown:
        raise RuntimeError("Docling returned empty markdown")
    return ConversionResult(markdown=markdown, provider="docling")


def _convert_with_markitdown(path: Path) -> ConversionResult:
    module = importlib.import_module("markitdown")
    result = module.MarkItDown().convert(str(path))
    markdown = str(getattr(result, "text_content", "") or "").strip()
    if not markdown:
        raise RuntimeError("MarkItDown returned empty markdown")
    return ConversionResult(markdown=markdown, provider="markitdown")
