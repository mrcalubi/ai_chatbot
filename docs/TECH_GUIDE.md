# Technical Guide

**For IT/Ops staff** — Quick reference for deployment, maintenance, and debugging.

---

## Components

### Backend
- **Framework**: FastAPI 0.115.0 (Python)
- **Server**: Uvicorn (ASGI)
- **Main entry**: `app/main.py`

### Frontend
- **Type**: Static HTML/JavaScript (no build step)
- **Location**: `web/index.html`
- **Served at**: `/ui/` endpoint (FastAPI StaticFiles)

### Core Libraries
- **Vector search**: FAISS (CPU version 1.8.0)
- **Text search**: rank-bm25 (BM25 algorithm)
- **Embeddings**: sentence-transformers (all-MiniLM-L6-v2, 384-dim)
- **LLM**: OpenAI API (gpt-4o-mini, configurable)
- **PDF processing**: PyMuPDF (fitz), pdfplumber
- **Optional OCR**: pytesseract (when `OCR_ENABLED=true`)

---

## API Endpoints

| Endpoint | Method | Purpose | Response |
|----------|--------|---------|----------|
| `/health` | GET | Health check | `{"ok": true, "model": "...", "docs_indexed": N}` |
| `/chat` | POST | Non-streaming Q&A | `ChatResponse` (answer, citations, found) |
| `/chat/stream` | POST | Streaming Q&A (preferred) | SSE text stream (meta header + tokens) |
| `/upload` | POST | Upload & index PDFs | `{"ok": true, "chunks_added": N}` |
| `/files` | GET | List indexed documents | `{"files": [...], "count": N}` |
| `/files/delete` | POST | Remove document from index | `{"ok": true, "removed_chunks": N}` |
| `/reset` | POST | Clear entire index | `{"ok": true, "message": "..."}` |
| `/pdf/view` | GET | Serve PDF for viewing | PDF file response |
| `/tables/png` | GET | Generate table PNG (lazy) | `{"png": "/static/tables/..."}` |

### Debug Endpoints
- `/debug/grep` — Search with literal matching
- `/debug/pages` — List pages for a document
- `/debug/chunks` — List chunks for a document
- `/debug/tables` — Table detection debug

### Export Endpoints
- `/export/text` — Full text dump (one doc)
- `/export/json` — JSON page dump
- `/export/full` — All documents as text stream
- `/export/matches` — Top-K raw chunks for query (no LLM)

---

## Environment Variables

### Required
- `OPENAI_API_KEY` — OpenAI API key (starts with `sk-...`)
  - **Where to set**: Secret vault / environment config
  - **Validation**: App fails to start if missing

### Optional (with defaults)
- `MODEL` — OpenAI model name (default: `gpt-4o-mini`)
- `EMBED_MODEL` — Embedding model (default: `sentence-transformers/all-MiniLM-L6-v2`)
- `TOP_K` — Default retrieval chunks (default: `6`)
- `INDEX_DIR` — FAISS index storage (default: `./index/`)
- `DATA_DIR` — PDF storage (default: `./data/`)

### OCR (Optional)
- `OCR_ENABLED` — Enable OCR fallback (default: `false`)
- `OCR_DPI` — OCR resolution (default: `220`)
- `TABLE_SKIP_HEADER_PX` — Skip header band (default: `72`)
- `TABLE_SKIP_FOOTER_PX` — Skip footer band (default: `60`)

### Example `.env` file
```bash
OPENAI_API_KEY=sk-...
MODEL=gpt-4o-mini
EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2
TOP_K=6
INDEX_DIR=/app/index
DATA_DIR=/app/data
OCR_ENABLED=false
```

---

## Running Locally

### Prerequisites
- Python 3.9+ (3.10+ recommended)
- Virtual environment (recommended)

### Setup
```bash
# Clone repository
cd /path/to/mw-pdf-qa

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create .env file
echo "OPENAI_API_KEY=sk-..." > .env

# Create data directories
mkdir -p data index static/tables
```

### Run Development Server
```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- **UI**: http://localhost:8000/ui/
- **API docs**: http://localhost:8000/docs
- **Health**: http://localhost:8000/health

---

## Deployment

### Production Command
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

### With Gunicorn (recommended for production)
```bash
gunicorn app.main:app -k uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --workers 4 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
```

### Environment Setup (Production)
1. Set all required environment variables (see above)
2. Ensure `DATA_DIR` and `INDEX_DIR` are writable
3. Mount persistent volumes for `data/` and `index/` (if using containers)
4. Configure reverse proxy (nginx, Cloudflare) for HTTPS
5. Set `CORS` origins appropriately (currently `allow_origins=["*"]` — restrict in production)

### Docker (if using)
```dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Frontend (Static)
- Frontend is served by FastAPI at `/ui/`
- No build step needed
- Cache-busting headers are set (no-cache)
- Update: Edit `web/index.html` and restart server

### API Base URL (Frontend)
- Frontend auto-detects: `location.origin`
- Fallback: `http://127.0.0.1:8000`
- To override: Edit `web/index.html` line ~320: `let API = 'https://api.mwanwo.com';`

---

## Logs & Monitoring

### Application Logs
- **Stdout/stderr**: Uvicorn logs to console
- **Access logs**: Use `--access-logfile` for request logs
- **Error logs**: Use `--error-logfile` for errors

### Health Check
```bash
curl https://api.mwanwo.com/health
```

Expected response:
```json
{
  "ok": true,
  "model": "gpt-4o-mini",
  "embed_model": "sentence-transformers/all-MiniLM-L6-v2",
  "docs_indexed": 42
}
```

**If `ok: false` or missing fields**: Check environment variables, OpenAI API key, or index corruption.

### Monitoring Points
1. **Health endpoint**: Monitor `/health` every 60s
2. **Response time**: Track `/chat/stream` p50/p95 latency
3. **Error rate**: Monitor 5xx responses
4. **OpenAI quota**: Track API usage in OpenAI dashboard
5. **Disk usage**: Monitor `INDEX_DIR` and `DATA_DIR` growth

### Log Locations
- **Development**: Console (stdout)
- **Production**: Configure via process manager (systemd, supervisord) or container logs
- **Q&A log**: `data/qa_log.jsonl` (append-only, JSONL format)

---

## Backups & State

### Persistent State
1. **FAISS index**: `index/` directory (contains `faiss.index`, `meta.npy`, `texts.npy`)
2. **PDFs**: `data/` directory (original PDFs)
3. **Table images**: `static/tables/` directory (lazy-generated PNGs)
4. **Q&A log**: `data/qa_log.jsonl` (conversation history for context linking)

### Backup Strategy
- **Index**: Back up `index/` directory (can rebuild from PDFs, but slow)
- **PDFs**: Back up `data/*.pdf` (critical—source of truth)
- **Q&A log**: Optional (used for follow-up context, can be rebuilt)

### Restore Process
1. Restore `data/` directory (PDFs)
2. Restore `index/` directory (if available, else rebuild)
3. Restart server — it will load existing index or rebuild on first query

### Rebuilding Index
If index is lost:
```bash
# Delete old index
rm -rf index/

# Restart server
# Upload PDFs via UI or use /upload endpoint
# Index rebuilds automatically on upload
```

---

## Dependencies

### Core (see `requirements.txt`)
- `fastapi==0.115.0`
- `uvicorn==0.30.6`
- `openai==1.109.1`
- `faiss-cpu==1.8.0.post1`
- `sentence-transformers==5.1.1`
- `rank-bm25==0.2.2`
- `PyMuPDF==1.24.14`
- `pdfplumber==0.11.7`
- `numpy==1.26.4`

### Optional (for OCR)
- `pdf2image==1.17.0`
- `pytesseract==0.3.13`
- System: `tesseract-ocr`, `poppler-utils`

---

## Security Notes

1. **CORS**: Currently `allow_origins=["*"]` — restrict in production
2. **Secrets**: Never commit `.env` or API keys (already in `.gitignore`)
3. **PDF access**: `/pdf/view` serves from `DATA_DIR` — validate filenames (already done via `os.path.basename`)
4. **File uploads**: Accepts only PDFs — validate in reverse proxy if needed
5. **Rate limiting**: Not implemented — add via middleware or reverse proxy if needed

---

## Performance

- **Typical latency**: 1–3s for `/chat/stream` (depends on query complexity)
- **Index size**: ~1.5MB per 1000 chunks
- **Memory**: ~500MB base + ~1MB per 1000 chunks
- **Concurrent requests**: Limited by OpenAI API rate limits (default: 3500 RPM for gpt-4o-mini)

---

## Common Issues

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for detailed fixes.

Quick fixes:
- **Import errors**: `pip install -r requirements.txt`
- **FAISS errors**: Delete `index/` and rebuild
- **OpenAI errors**: Check API key and quota
- **Slow queries**: Consider increasing `TOP_K` or upgrading embedding model

---

*For architecture details, see [ARCHITECTURE.md](ARCHITECTURE.md)*
