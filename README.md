# MW PDF Q&A (v0.1)
Drag-and-drop PDFs → ask natural-language questions → get precise answers with page-level citations. Strictly grounded; if info isn't present, returns "Not found in uploaded PDFs."


## 1) Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env # add your OPENAI_API_KEY