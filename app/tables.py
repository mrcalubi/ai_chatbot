from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import hashlib
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

def detect_tables_text_only(pdf_path: Path) -> List[TableMeta]:
    metas: List[TableMeta] = []
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
