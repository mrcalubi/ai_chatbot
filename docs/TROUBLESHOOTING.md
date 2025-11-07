# Troubleshooting Guide

Common issues, diagnostic steps, fixes, and prevention.

---

## Issue 1: Missing Environment Variable (OPENAI_API_KEY)

### Symptom
- Server fails to start with: `RuntimeError: OPENAI_API_KEY not found`
- `/health` endpoint returns 500 error

### Quick Checks
```bash
# Check if variable is set
echo $OPENAI_API_KEY

# Check .env file exists
ls -la .env

# Check .env content (don't print full key)
grep -o "OPENAI_API_KEY=sk-.*" .env | head -c 20
```

### Likely Causes
1. `.env` file missing or not loaded
2. Variable not exported in shell/environment
3. Wrong variable name (typo)

### Fix
```bash
# Create .env file
echo "OPENAI_API_KEY=sk-..." > .env

# For production (set in environment)
export OPENAI_API_KEY=sk-...

# For systemd (add to service file)
Environment="OPENAI_API_KEY=sk-..."
```

### Prevention
- Document required env vars in `TECH_GUIDE.md` ✅
- Add startup validation (already implemented ✅)
- Use secret management (AWS Secrets Manager, 1Password, etc.)

---

## Issue 2: Invalid or Expired OpenAI API Key

### Symptom
- Server starts but `/chat` returns 401 Unauthorized
- Error message: `Incorrect API key provided` or `Invalid API key`

### Quick Checks
```bash
# Test API key directly
curl https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY"

# Check health endpoint
curl https://api.mwanwo.com/health
```

### Likely Causes
1. API key expired or revoked
2. Typo in API key
3. Wrong key format (missing `sk-` prefix)
4. Rate limit exceeded (429 error, not 401)

### Fix
1. **Regenerate API key** in OpenAI dashboard
2. **Update environment variable**:
   ```bash
   # Update .env
   sed -i 's/OPENAI_API_KEY=.*/OPENAI_API_KEY=sk-new-key/' .env
   
   # Restart server
   systemctl restart mw-pdf-qa  # or your process manager
   ```
3. **Check OpenAI dashboard** for usage/quota issues

### Prevention
- Monitor OpenAI dashboard for quota warnings
- Set up alerts for 401/429 errors
- Rotate keys periodically

---

## Issue 3: API Rate Limit (429 Error)

### Symptom
- `/chat` requests fail with HTTP 429
- Error: `Rate limit exceeded` or `You exceeded your current quota`

### Quick Checks
```bash
# Check OpenAI dashboard for usage
# https://platform.openai.com/usage

# Check error logs
tail -f /var/log/mw-pdf-qa/error.log | grep 429
```

### Likely Causes
1. Too many concurrent requests
2. Exceeded monthly quota/billing limit
3. Rate limit per minute/hour exceeded

### Fix
1. **Wait and retry**: Rate limits reset per minute/hour
2. **Reduce concurrency**: Lower number of workers or add request queuing
3. **Upgrade OpenAI plan**: Increase rate limits in OpenAI dashboard
4. **Add retry logic** (with exponential backoff):
   ```python
   # In app/main.py (if not already implemented)
   from openai import RateLimitError
   # Add retry decorator or middleware
   ```

### Prevention
- Monitor OpenAI usage dashboard
- Implement request queuing/rate limiting on server side
- Set up alerts for 429 errors
- Use caching for common queries

---

## Issue 4: Large PDF Upload Timeout

### Symptom
- Upload progress bar stuck
- Request times out (504 Gateway Timeout)
- Large PDFs (>50MB) fail to upload

### Quick Checks
```bash
# Check file size
ls -lh data/*.pdf | sort -k5 -hr | head -5

# Check server timeout settings
# In uvicorn/gunicorn config
```

### Likely Causes
1. Server timeout too short (< 120s)
2. PDF processing too slow (extraction + chunking + embedding)
3. Network timeout (reverse proxy, load balancer)

### Fix
1. **Increase server timeout**:
   ```bash
   # Uvicorn
   uvicorn app.main:app --timeout-keep-alive 300
   
   # Gunicorn
   gunicorn ... --timeout 300
   ```
2. **Increase reverse proxy timeout** (nginx):
   ```nginx
   proxy_read_timeout 300s;
   proxy_connect_timeout 300s;
   ```
3. **Split large PDFs**: Pre-process to split into smaller files if possible

### Prevention
- Set reasonable timeout defaults (300s)
- Add file size limits in UI (warn users)
- Show progress indicators during upload
- Pre-process PDFs if consistently large

---

## Issue 5: Dropbox Path Mismatch

### Symptom
- Files exist in Dropbox but don't appear in chatbot UI
- `/files` endpoint returns empty list
- Error: `File not found in data/`

### Quick Checks
```bash
# Check data directory
ls -la data/

# Check if scraper is writing to correct path
# (If you have access to scraper logs)

# Check DATA_DIR env var
echo $DATA_DIR
```

### Likely Causes
1. Scraper writing to different path than `DATA_DIR`
2. Manual sync needed (scraper not integrated yet)
3. Permissions issue (can't read Dropbox folder)

### Fix
1. **Manual sync**: Copy PDFs from Dropbox to `data/` directory
2. **Update DATA_DIR**: Point to actual Dropbox path (if mounted):
   ```bash
   export DATA_DIR=/path/to/Dropbox/Apps/Mwanwo/Reports
   ```
3. **Check scraper**: Ensure scraper is writing to correct location
4. **Implement Dropbox sync**: Add Dropbox API integration (future work)

### Prevention
- Document Dropbox path in deployment docs
- Automate sync between Dropbox and `data/` directory
- Add health check that verifies file count matches expected

---

## Issue 6: CORS Errors in Browser

### Symptom
- Browser console shows: `Access to fetch at '...' from origin '...' has been blocked by CORS policy`
- Frontend can't communicate with API

### Quick Checks
```bash
# Check CORS settings in app/main.py
grep -A 5 "CORSMiddleware" app/main.py

# Test from browser console
fetch('https://api.mwanwo.com/health').then(r=>r.json())
```

### Likely Causes
1. Frontend origin not allowed in CORS config
2. Production CORS still set to `allow_origins=["*"]` (should be restricted)
3. Missing CORS headers in response

### Fix
1. **Update CORS origins** in `app/main.py`:
   ```python
   app.add_middleware(
       CORSMiddleware,
       allow_origins=["https://mwanwo.com"],  # Restrict in production
       allow_methods=["*"],
       allow_headers=["*"],
   )
   ```
2. **Restart server** after changes

### Prevention
- Set CORS origins via environment variable
- Use separate configs for dev/prod
- Test CORS in staging before production

---

## Issue 7: Health Endpoint Returns Error

### Symptom
- `/health` returns 500 error or `{"ok": false}`
- Monitoring alerts fire

### Quick Checks
```bash
# Test health endpoint
curl https://api.mwanwo.com/health

# Check server logs
tail -f /var/log/mw-pdf-qa/error.log

# Check if index is corrupted
ls -la index/
```

### Likely Causes
1. Missing environment variable (see Issue 1)
2. Corrupted FAISS index
3. OpenAI API key invalid (see Issue 2)
4. Index files missing or unreadable

### Fix
1. **Check logs** for specific error message
2. **Rebuild index** if corrupted:
   ```bash
   # Backup old index
   mv index/ index.backup/
   mkdir index/
   
   # Restart server and re-upload PDFs
   ```
3. **Verify environment variables**:
   ```bash
   env | grep -E "(OPENAI|MODEL|EMBED)"
   ```

### Prevention
- Regular index backups
- Health check validation in startup
- Monitor health endpoint with alerts

---

## Issue 8: FAISS Index Dimension Mismatch

### Symptom
- Server starts but search fails
- Error: `Index dimension mismatch` or `faiss.IndexFlatIP: dimension mismatch`

### Quick Checks
```bash
# Check index files
ls -la index/

# Check embedding model in config
grep EMBED_MODEL .env
```

### Likely Causes
1. Embedding model changed but index not rebuilt
2. Index created with different model
3. Index files corrupted

### Fix
1. **Delete and rebuild index**:
   ```bash
   rm -rf index/
   mkdir index/
   # Restart server, re-upload PDFs
   ```
2. **Verify embedding model** matches index:
   ```bash
   # Ensure EMBED_MODEL is consistent
   echo $EMBED_MODEL
   ```

### Prevention
- Document embedding model version
- Version index files (include model name in path)
- Validate model on startup

---

## Issue 9: Slow Query Performance

### Symptom
- `/chat/stream` takes >10 seconds
- Users report slow responses

### Quick Checks
```bash
# Time a query
time curl -X POST https://api.mwanwo.com/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"query": "test", "k": 6}'

# Check index size
du -sh index/
```

### Likely Causes
1. Large index (IndexFlatIP is O(n))
2. Too many sub-queries (HyDE expansion)
3. OpenAI API latency
4. Network issues

### Fix
1. **Upgrade to HNSW index** (see `QUICKSTART.md` Priority 1):
   ```python
   # In app/rag.py
   self.index = faiss.IndexHNSWFlat(self.dim, 32)
   ```
2. **Reduce TOP_K** if queries are too broad
3. **Cache common queries** (implement Redis/memory cache)
4. **Check OpenAI API status**: https://status.openai.com

### Prevention
- Monitor query latency (p50, p95)
- Set up performance alerts
- Consider HNSW index for production
- Use query result caching

---

## Issue 10: PDF Viewer Not Opening

### Symptom
- Citations don't open PDF viewer
- Error: `PDF not found` or blank modal

### Quick Checks
```bash
# Check if PDF exists
ls -la data/Report.pdf

# Test PDF endpoint
curl -I https://api.mwanwo.com/pdf/view?doc=Report.pdf
```

### Likely Causes
1. PDF filename mismatch (case-sensitive)
2. PDF deleted but still in index
3. Browser pop-up blocker
4. Path traversal issue (if filename contains special chars)

### Fix
1. **Check filename** matches exactly (case-sensitive)
2. **Re-upload PDF** if missing:
   ```bash
   # Upload via UI or API
   curl -X POST https://api.mwanwo.com/upload \
     -F "files=@Report.pdf"
   ```
3. **Allow pop-ups** in browser settings
4. **Check browser console** for JavaScript errors

### Prevention
- Normalize filenames on upload
- Validate PDF exists before showing citation
- Add error handling in frontend

---

## Issue 11: Table Badges Not Showing

### Symptom
- Tables detected but no 📊 badges appear
- Table PNG generation fails

### Quick Checks
```bash
# Check static/tables directory
ls -la static/tables/

# Test table endpoint
curl "https://api.mwanwo.com/tables/png?doc=Report.pdf&page=0&bbox=100,100,500,300"
```

### Likely Causes
1. Table detection failed (no tables found)
2. PNG generation error (permissions, dependencies)
3. Frontend JavaScript error

### Fix
1. **Check table detection**:
   ```bash
   curl "https://api.mwanwo.com/debug/tables?doc=Report.pdf"
   ```
2. **Verify permissions**:
   ```bash
   chmod 755 static/tables/
   ```
3. **Check dependencies**: Ensure pdfplumber, PIL installed

### Prevention
- Test table detection on sample PDFs
- Add error logging for table generation
- Validate table metadata in response

---

## Issue 12: Deployment Failed / Server Won't Start

### Symptom
- Deployment script fails
- Server process crashes immediately
- Port already in use

### Quick Checks
```bash
# Check if port is in use
lsof -i :8000
netstat -tuln | grep 8000

# Check Python version
python --version  # Need 3.9+

# Check dependencies
pip list | grep -E "(fastapi|uvicorn|openai)"
```

### Likely Causes
1. Port conflict (another process using 8000)
2. Missing dependencies
3. Python version mismatch
4. Permission issues

### Fix
1. **Kill existing process**:
   ```bash
   kill $(lsof -t -i:8000)
   ```
2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Use different port**:
   ```bash
   uvicorn app.main:app --port 8001
   ```
4. **Check permissions**:
   ```bash
   chmod +x app/main.py
   ```

### Prevention
- Use process manager (systemd, supervisord)
- Document deployment steps
- Test deployment in staging first

---

## Quick Reference: Diagnostic Commands

```bash
# Health check
curl https://api.mwanwo.com/health

# List files
curl https://api.mwanwo.com/files

# Check environment
env | grep -E "(OPENAI|MODEL|EMBED|INDEX|DATA)"

# Check logs (adjust path)
tail -f /var/log/mw-pdf-qa/*.log

# Check disk usage
du -sh data/ index/ static/

# Check process
ps aux | grep uvicorn

# Test OpenAI API
curl https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY"
```

---

## Getting Help

1. **Check logs first**: Most issues show errors in server logs
2. **Review this guide**: Common issues covered above
3. **Check [TECH_GUIDE.md](TECH_GUIDE.md)**: For deployment/config details
4. **Contact IT/Ops**: For infrastructure issues or access problems

---

*Last updated: [Date]*
