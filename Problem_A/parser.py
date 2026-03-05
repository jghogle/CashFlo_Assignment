"""
parser.py — Document parsing module.

Extracts text from PDF (or plain-text) policy documents, segments the content
into sections / sub-sections, and resolves cross-references.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Clause:
    """A single numbered clause within a section."""
    number: str          # e.g. "2.2(c)"
    raw_text: str        # verbatim text
    cross_refs: List[str] = field(default_factory=list)  # e.g. ["Section 2.3(b)"]


@dataclass
class Section:
    """A top-level or nested section of the policy document."""
    number: str          # e.g. "2" or "3"
    title: str           # human-readable title
    raw_text: str        # full text of the section
    clauses: List[Clause] = field(default_factory=list)


@dataclass
class ParsedDocument:
    """Complete parsed representation of the policy document."""
    title: str
    full_text: str
    sections: List[Section]
    cross_reference_map: dict  # clause_ref → Section/Clause


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Matches lines like "### Section 2: Purchase Order Matching"
_SECTION_HEADER_RE = re.compile(
    r"(?:#{1,4}\s*)?(?:Section\s+)?(\d+)\s*[:\-–]\s*(.+)",
    re.IGNORECASE,
)

# Matches numbered clauses: "2.1 Every invoice...", "2.2(b) If the..."
_CLAUSE_RE = re.compile(
    r"^(\d+\.\d+(?:\([a-zA-Z0-9]\))?)\s+(.+)",
)

# Matches cross-references in text: "Refer Section 2.3(b)", "per Section 6"
_CROSS_REF_RE = re.compile(
    r"(?:Refer|refer|per|see|as per|as defined in)\s+Section\s+([\d]+(?:\.[\d]+)?(?:\([a-zA-Z0-9]\))?)",
    re.IGNORECASE,
)

# Also capture bare "Section X.Y(z)" references
_BARE_SECTION_RE = re.compile(
    r"Section\s+([\d]+(?:\.[\d]+)?(?:\([a-zA-Z0-9]\))?)",
    re.IGNORECASE,
)


def _extract_cross_refs(text: str) -> List[str]:
    refs = _CROSS_REF_RE.findall(text) + _BARE_SECTION_RE.findall(text)
    # Deduplicate while preserving order
    seen: set = set()
    result = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            result.append(r)
    return result


def _clean_text(text: str) -> str:
    """Remove PDF artefacts and normalise whitespace."""
    # Remove URL lines (artefact from web-captured PDF)
    text = re.sub(r"https?://\S+", "", text)
    # Collapse multiple spaces / tabs
    text = re.sub(r"[ \t]+", " ", text)
    # Normalise Unicode dashes / curly quotes
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    # Remove page headers injected by PDF viewer (e.g. "3/1/26, 12:58 PM …")
    text = re.sub(r"\d+/\d+/\d+,\s*\d+:\d+\s*[APM]+.*", "", text)
    # Remove "--- PAGE N ---" markers
    text = re.sub(r"---\s*PAGE\s*\d+\s*---", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Section & clause extraction
# ---------------------------------------------------------------------------

def _split_into_sections(text: str) -> List[Section]:
    """Split the document text into Section objects."""
    sections: List[Section] = []
    current_section: Optional[Section] = None
    current_lines: List[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current_lines:
                current_lines.append("")
            continue

        section_match = _SECTION_HEADER_RE.match(line)
        if section_match and not _CLAUSE_RE.match(line):
            # Save previous section
            if current_section is not None:
                current_section.raw_text = "\n".join(current_lines).strip()
                current_section.clauses = _extract_clauses(current_section.raw_text)
                sections.append(current_section)

            num = section_match.group(1)
            title = section_match.group(2).strip().lstrip("#").strip()
            current_section = Section(number=num, title=title, raw_text="", clauses=[])
            current_lines = []
        else:
            current_lines.append(line)

    # Flush last section
    if current_section is not None:
        current_section.raw_text = "\n".join(current_lines).strip()
        current_section.clauses = _extract_clauses(current_section.raw_text)
        sections.append(current_section)

    return sections


def _extract_clauses(section_text: str) -> List[Clause]:
    """Extract individual numbered clauses from section text."""
    clauses: List[Clause] = []
    current_num: Optional[str] = None
    current_lines: List[str] = []

    for line in section_text.splitlines():
        m = _CLAUSE_RE.match(line.strip())
        if m:
            if current_num is not None:
                raw = " ".join(current_lines).strip()
                clauses.append(Clause(
                    number=current_num,
                    raw_text=raw,
                    cross_refs=_extract_cross_refs(raw),
                ))
            current_num = m.group(1)
            current_lines = [m.group(2).strip()]
        elif current_num:
            current_lines.append(line.strip())

    if current_num:
        raw = " ".join(current_lines).strip()
        clauses.append(Clause(
            number=current_num,
            raw_text=raw,
            cross_refs=_extract_cross_refs(raw),
        ))

    return clauses


def _build_cross_ref_map(sections: List[Section]) -> dict:
    """Build a lookup: clause/section ref string → Clause or Section."""
    ref_map: dict = {}
    for sec in sections:
        ref_map[sec.number] = sec
        for clause in sec.clauses:
            ref_map[clause.number] = clause
    return ref_map


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_pdf(pdf_path: str | Path) -> ParsedDocument:
    """Parse a PDF policy document and return a structured ParsedDocument."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError("pdfplumber is required: pip install pdfplumber") from exc

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    raw_pages: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            raw_pages.append(text)

    full_raw = "\n".join(raw_pages)
    return _parse_text(full_raw, source=str(pdf_path))


def parse_text(text_path: str | Path) -> ParsedDocument:
    """Parse a plain-text policy document."""
    text = Path(text_path).read_text(encoding="utf-8")
    return _parse_text(text, source=str(text_path))


def parse_raw_text(text: str, source: str = "inline") -> ParsedDocument:
    """Parse raw text directly (useful for testing)."""
    return _parse_text(text, source=source)


def _parse_text(raw: str, source: str) -> ParsedDocument:
    cleaned = _clean_text(raw)
    lines = [l.strip() for l in cleaned.splitlines() if l.strip()]

    # Extract document title from first heading-like line
    title = "Accounts Payable Policy"
    for line in lines[:5]:
        if re.search(r"(policy|accounts\s+payable|AP)", line, re.IGNORECASE):
            title = line.lstrip("#").strip()
            break

    sections = _split_into_sections(cleaned)
    cross_ref_map = _build_cross_ref_map(sections)

    return ParsedDocument(
        title=title,
        full_text=cleaned,
        sections=sections,
        cross_reference_map=cross_ref_map,
    )


def summarize(doc: ParsedDocument) -> str:
    """Return a human-readable summary of the parsed document structure."""
    lines = [f"Document: {doc.title}", f"Sections: {len(doc.sections)}", ""]
    for sec in doc.sections:
        lines.append(f"  Section {sec.number}: {sec.title} ({len(sec.clauses)} clauses)")
        for clause in sec.clauses:
            xrefs = f"  [xref: {', '.join(clause.cross_refs)}]" if clause.cross_refs else ""
            snippet = clause.raw_text[:80].replace("\n", " ")
            lines.append(f"    {clause.number}: {snippet}…{xrefs}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from config import POLICY_PDF_PATH

    path = sys.argv[1] if len(sys.argv) > 1 else POLICY_PDF_PATH
    doc = parse_pdf(path)
    print(summarize(doc))
