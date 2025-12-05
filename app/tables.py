from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import re
from typing import List, Tuple, Optional
import pdfplumber
import fitz  # PyMuPDF

TABLES_DIR = Path(__file__).resolve().parents[1] / "static" / "tables"
TABLES_DIR.mkdir(parents=True, exist_ok=True)

@dataclass
class TableMeta:
    doc: str
    page: int                # 0-based page
    bbox: Tuple[float,float,float,float]  # (x0,y0,x1,y1)
    text: str                # flattened (headers + cells)
    table_id: str            # stable ID

def _tid(doc: str, page: int, bbox: Tuple[float,float,float,float]) -> str:
    s = f"{doc}|{page}|{bbox}"
    return hashlib.sha1(s.encode()).hexdigest()[:16]

def _detect_tables_from_ocr_text(text: str, page_num: int) -> List[Tuple[str, Tuple[float, float, float, float]]]:
    """
    Heuristic to detect table-like structures in OCR text.
    Returns list of (table_text, bbox) where bbox is estimated.
    """
    if not text or len(text.strip()) < 50:
        return []
    
    # Look for patterns that suggest tables:
    # - Multiple lines with consistent separators (|, tabs, multiple spaces)
    # - Lines with numbers/percentages in columns
    # - Repeated patterns (like ship names + capacity)
    
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if len(lines) < 3:  # Need at least 3 lines for a table
        return []
    
    # Check if lines have consistent separators (table-like)
    separator_counts = []
    for line in lines[:20]:  # Sample more lines for better detection
        # Count common table separators
        pipe_count = line.count('|')
        tab_count = line.count('\t')
        # Count multiple spaces (column separators)
        multi_space = len(re.findall(r' {2,}', line))
        separator_counts.append(pipe_count + tab_count + (multi_space // 2))
    
    avg_separators = sum(separator_counts) / len(separator_counts) if separator_counts else 0
    
    # Check for multiple numbers in sequence (suggests columns)
    has_number_columns = len(re.findall(r'\d+\s+\d+\s+\d+', text)) > 2
    
    # Check for table-like structure: multiple lines with similar patterns (word + number)
    # Look for lines that have a word followed by numbers (suggests structured data)
    lines_with_word_and_number = sum(1 for line in lines if re.search(r'\b[a-zA-Z]+\b.*\d+', line))
    has_structured_rows = lines_with_word_and_number >= 3  # At least 3 rows with word + number pattern
    
    # Check for repeated patterns: if multiple lines start with similar patterns (capitalized words + numbers)
    capitalized_word_patterns = len(re.findall(r'^[A-Z][a-z]+\s+\d+', '\n'.join(lines[:10]), re.MULTILINE))
    has_repeated_patterns = capitalized_word_patterns >= 3
    
    # If average separators > 1 OR has number columns OR structured rows OR repeated patterns, likely a table
    if avg_separators > 1 or has_number_columns or has_structured_rows or has_repeated_patterns:
        # Extract table text (join lines with | separator for consistency)
        table_text = " | ".join(lines)
        # Estimate bbox (full page width, reasonable height)
        # This is approximate; actual bbox would need image analysis
        bbox = (50.0, 100.0, 550.0, 100.0 + len(lines) * 15.0)
        return [(table_text, bbox)]
    
    return []


def detect_tables_text_only(pdf_path: Path) -> List[TableMeta]:
    """
    Detect tables using pdfplumber (for native PDF tables) and
    OCR-based detection (for scanned/image tables).
    """
    metas: List[TableMeta] = []
    OCR_ENABLED = os.getenv("OCR_ENABLED", "").lower() != "false"
    
    # Method 1: pdfplumber for native PDF tables
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pidx, page in enumerate(pdf.pages):
            try:
                tables = page.find_tables(table_settings={
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                    "intersection_x_tolerance": 2,
                    "intersection_y_tolerance": 2,
                    "keep_blank_chars": False,
                }) or []
            except Exception:
                tables = []
            for t in tables:
                bbox = tuple(map(float, t.bbox))  # (x0, top, x1, bottom)
                rows = t.extract() or []
                flat = []
                for row in rows:
                    flat.extend([c for c in row if c])
                text = " | ".join(flat).strip()
                if not text:
                    continue
                metas.append(TableMeta(
                    doc=pdf_path.name,
                    page=pidx,
                    bbox=(bbox[0], bbox[1], bbox[2], bbox[3]),
                    text=text,
                    table_id=_tid(pdf_path.name, pidx, (bbox[0], bbox[1], bbox[2], bbox[3]))
                ))
    
    # Method 2: OCR-based table detection for scanned pages
    OCR_ENABLED = os.getenv("OCR_ENABLED", "").lower() != "false"
    if OCR_ENABLED:
        try:
            from pdf2image import convert_from_path
            import pytesseract
            
            OCR_DPI = int(os.getenv("OCR_DPI", "300"))
            AUTO_DETECT_LANGUAGE = (os.getenv("AUTO_DETECT_LANGUAGE", "true").lower() == "true")
            
            # Check if PDF has Vietnamese content (affects OCR language)
            ocr_lang = 'eng'
            if AUTO_DETECT_LANGUAGE:
                try:
                    from app.language_detection import detect_pdf_language
                    detected = detect_pdf_language(str(pdf_path), sample_pages=2)
                    if detected == 'vi':
                        ocr_lang = 'vie'
                    elif detected == 'mixed':
                        ocr_lang = 'vie+eng'
                except Exception:
                    pass
            
            # Check each page for low text (likely scanned)
            with fitz.open(str(pdf_path)) as doc:
                for pidx in range(len(doc)):
                    page = doc[pidx]
                    blocks = page.get_text("blocks") or []
                    text_blocks = [b for b in blocks if len(b) >= 5 and isinstance(b[4], str)]
                    raw_text = " ".join(b[4] for b in text_blocks if b[4] if b[4])
                    
                    # If page has little text but has images, try OCR
                    image_list = page.get_images()
                    if len(raw_text.strip()) < 100 and image_list:
                        try:
                            images = convert_from_path(str(pdf_path), dpi=OCR_DPI, first_page=pidx+1, last_page=pidx+1)
                            if images:
                                # OCR the page
                                ocr_text = pytesseract.image_to_string(images[0], lang=ocr_lang, config='--psm 6') or ""
                                # Try to detect tables in OCR text
                                ocr_tables = _detect_tables_from_ocr_text(ocr_text, pidx)
                                for table_text, bbox in ocr_tables:
                                    metas.append(TableMeta(
                                        doc=pdf_path.name,
                                        page=pidx,
                                        bbox=bbox,
                                        text=table_text,
                                        table_id=_tid(pdf_path.name, pidx, bbox)
                                    ))
                        except Exception as e:
                            print(f"[table_detection] OCR table detection failed for page {pidx}: {e}")
        except ImportError:
            pass  # OCR dependencies not available
        except Exception as e:
            print(f"[table_detection] OCR-based table detection error: {e}")
    
    return metas

def render_table_png(pdf_path: Path, page: int, bbox: Tuple[float,float,float,float]) -> str:
    """Crop the PDF region lazily to a PNG and return its /static URL."""
    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{_tid(pdf_path.name, page, bbox)}.png"
    out = TABLES_DIR / name
    if out.exists():
        return f"/static/tables/{name}"
    doc = fitz.open(str(pdf_path))
    pg = doc[page]
    clip = fitz.Rect(*bbox)
    pix = pg.get_pixmap(clip=clip, dpi=200)
    pix.save(out.as_posix())
    return f"/static/tables/{name}"
