"""Extract plain text from uploaded files (pdf/txt/md/csv) for context ingestion."""

from __future__ import annotations

import io

_TEXT_EXTS = (".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".sql")


class UnsupportedFile(ValueError):
    pass


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        return _extract_pdf(data)
    if name.endswith(_TEXT_EXTS):
        return data.decode("utf-8", errors="replace")
    raise UnsupportedFile(f"unsupported file type: {filename}")


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages).strip()
