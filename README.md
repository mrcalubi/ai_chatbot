# 💬 MWA AI Chatbot

Ask questions about PDF reports and get precise answers with page-level citations. The chatbot only uses information present in your uploaded documents—if something isn't found, it will tell you.

---

## 🚦 Quick Start

| Tool | What it does | How to use |
|------|--------------|------------|
| **💬 Chatbot** | Answers questions from PDF reports with citations | [Open https://mwanwo.com](https://mwanwo.com) → Login → Upload PDF → Ask your question |

---

## 📊 System Overview

The chatbot lets you upload PDF reports and ask natural-language questions. It searches through the documents, finds relevant sections, and provides answers with exact page citations.

**How it works:**
1. You upload PDF reports through the web interface
2. The system indexes the content for fast searching
3. You ask questions in plain English
4. The AI finds relevant passages and generates answers
5. Each answer includes clickable citations showing exactly where the information came from

**Data Flow:**
```
Your Browser (mwanwo.com)
    ↓
Backend API (api.mwanwo.com)
    ↓
PDF Storage (Dropbox /Apps/Mwanwo/Reports)
    ↓
AI Search & Answer Engine
```

> **Note:** A separate PDF scraper service automatically adds new reports to the Dropbox folder. You don't need to manage this—just check that new files appear in the file list.

See [Architecture Diagram](docs/ARCHITECTURE.md) for technical details.

---

## 📖 How to Use

### Step 1: Open the Chatbot
Visit [https://mwanwo.com](https://mwanwo.com) and log in (company SSO or email authentication).

### Step 2: Upload or Select a PDF
- **Upload**: Drag and drop PDF files into the upload area, or click "Pick files"
- **Wait**: The system processes the file (watch the progress indicator)
- **Verify**: Once indexed, the file appears in your "Attached files" list

![Upload Interface](docs/images/upload.png)

### Step 3: Ask a Question
Type your question in the search box. Examples:
- "What is the 2025 capex plan?"
- "Summarize the revenue guidance for 2024-2026"
- "DGC capex 2024 2026"

### Step 4: Review the Answer
- **Answer**: The AI provides a natural-language response
- **Citations**: Click "Evidence" to see which pages were used
- **PDF Viewer**: Click any citation link (e.g., `Report.pdf, p.5`) to open the PDF viewer at that exact page

![Answer with Citations](docs/images/answer-citation.png)

### Common Use Cases

#### 📈 Financial Metrics
Ask about specific numbers, trends, or forecasts:
- "What was the revenue growth in 2024?"
- "Show me the EBITDA margin for the last 3 years"
- "What is the guidance for 2026?"

#### 📊 Summary Requests
Get high-level overviews:
- "Summarize the key points of this report"
- "What are the main investment highlights?"
- "Give me an overview of the company's outlook"

#### 🔍 Specific Details
Find exact quotes or page numbers:
- "Quote the exact wording about capex on page 10"
- "What does it say about dividends?"
- "Find the table showing capacity expansion"

#### 🔄 Follow-up Questions
The chatbot remembers context:
- Ask: "What is DGC's capex?"
- Follow up: "How is it funded?" (it knows you're still talking about DGC)

---

## ✅ Is it Working?

| Check | Expected Result | If Not Working |
|-------|----------------|----------------|
| **Site loads** | Visit [mwanwo.com](https://mwanwo.com) → page opens, login works | Refresh browser; contact IT if login fails |
| **API health** | Visit [api.mwanwo.com/health](https://api.mwanwo.com/health) → shows `{"status":"ok","model":"gpt-4o-mini",...}` | Contact IT/Ops team |
| **Files available** | In chatbot UI → "Attached files" shows your PDFs | Confirm PDFs exist in Dropbox `/Apps/Mwanwo/Reports`; ask ops to check scraper |
| **Answer quality** | Answers include citations and make sense | Try a more specific question; ensure correct PDF is uploaded |

---

## 🧯 Troubleshooting

### Chatbot Not Loading
1. **Refresh the page** (Ctrl+R or Cmd+R)
2. **Check your internet connection**
3. **Clear browser cache** if the page looks broken
4. **Contact IT** if login doesn't work or you see error messages

### Answer Looks Wrong or Incomplete
1. **Check the uploaded PDF**: Make sure the correct file is indexed (see "Attached files" list)
2. **Try a more specific question**: Instead of "tell me about revenue," ask "What was the revenue in 2024?"
3. **Include years in your question**: Questions like "capex 2024 2026" work better than just "capex"
4. **Check citations**: Click "Evidence" to see what pages were found—the answer is only as good as what's in the documents

### No Files Listed
1. **Refresh files**: Click the "Refresh" button in the chatbot UI
2. **Check Dropbox**: Confirm PDFs exist at `/Apps/Mwanwo/Reports` in your Dropbox Team folder
3. **Wait**: If files were just added, indexing may take 30–60 seconds
4. **Ask ops**: If files are in Dropbox but not appearing, the scraper/indexer may need attention

### Citations Don't Open PDF Viewer
1. **Allow pop-ups**: Some browsers block PDF viewers—check your pop-up settings
2. **Try a different browser**: Chrome or Firefox usually work best
3. **Check file name**: Ensure the PDF filename matches exactly (case-sensitive)

### Upload Stuck or Fails
1. **Check file size**: Very large PDFs (>50MB) may timeout—split if possible
2. **Verify PDF format**: Ensure it's a valid PDF (not corrupted)
3. **Try again**: Network issues can cause temporary failures
4. **Contact IT**: If uploads consistently fail

For detailed technical fixes, see [Troubleshooting Guide](docs/TROUBLESHOOTING.md).

---

## 🔑 Access & Ownership

### Domain Management
- **Public URL**: [https://mwanwo.com](https://mwanwo.com)
- **API URL**: [https://api.mwanwo.com](https://api.mwanwo.com)
- **Registrar**: *[Update with actual registrar/DNS provider]*
- **DNS**: Managed by *[Update with DNS provider]* team

### Secrets & Configuration
- **Environment variables**: Stored in *[Update with secret vault name, e.g., "AWS Secrets Manager" or "1Password vault"]*
- **OpenAI API key**: Managed in *[Update with location]*
- **Dropbox credentials**: Managed in *[Update with location]*

### Platform Accounts
- **Company org/team**: *[Update with actual org name]*
- **Hosting provider**: *[Update with provider, e.g., "AWS EC2" or "Google Cloud Run"]*
- **Monitoring**: *[Update if status page exists, e.g., "status.mwanwo.com"]*

### Contacts

| Role | Responsibility | Contact |
|------|----------------|---------|
| **Project Lead** | Product decisions, feature requests | *[Update with contact]* |
| **IT/Ops** | Deployment, infrastructure, outages | *[Update with contact]* |
| **Support** | User questions, access issues | *[Update with contact]* |

---

## 💡 Notes for Power Users

### Citing Sources
Every answer includes citations showing document name and page number. Click any citation to open the PDF viewer at that exact page.

### Comparing Multiple PDFs
Upload several PDFs and ask questions that span documents:
- "Compare the revenue guidance across all uploaded reports"
- "Which report mentions the highest capex?"

### Exporting Answers
Currently, you can copy answers using the "Copy Answer" button in each response card. For bulk exports or structured data, contact IT about the `/export/*` API endpoints (requires API access).

### Advanced Queries
- **Exact quotes**: Ask "Quote the exact wording about..." to get verbatim text
- **Table detection**: The chatbot automatically detects tables and shows 📊 badges—click to preview as an image
- **Year-based filtering**: Including years (2024, 2025) improves search accuracy
- **Company codes**: Mentioning company codes (VGC, DGC) helps filter to relevant documents

### Keyboard Shortcuts
- **Enter**: Submit question
- **Esc**: Close modals/dialogs

---

---

## 💻 Local Development

### Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/mrcalubi/ai_chatbot.git
cd ai_chatbot

# 2. Create virtual environment (Python 3.11 recommended)
python3.11 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create .env file with your OpenAI API key
echo "OPENAI_API_KEY=sk-your-key-here" > .env

# 5. Create required directories
mkdir -p data index static/tables

# 6. Start the server
./start_server.sh
# OR
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# 7. Open in browser
# http://localhost:8000/ui/
```

### Development Server
- **UI**: http://localhost:8000/ui/
- **API Docs**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health

### Requirements
- Python 3.11+ (3.13 may have compatibility issues)
- OpenAI API key
- See [requirements.txt](requirements.txt) for full dependencies

---

## 🚀 Deployment Status

### Current Status
- ✅ **Local Development**: Fully functional
- ⚠️ **Cloud Run Deployment**: Currently **disabled** (see [CLOUD_RUN_EXPLANATION.md](CLOUD_RUN_EXPLANATION.md))
- 📦 **Docker**: Dockerfile available in git history (can be restored if needed)

### Why Cloud Run is Disabled
Cloud Run automatic deployment has been disabled to focus on local development. The deployment trigger can be re-enabled and configured when ready for production deployment.

**To re-enable Cloud Run:**
1. See [CLOUD_RUN_EXPLANATION.md](CLOUD_RUN_EXPLANATION.md) for detailed instructions
2. Fix the build logs configuration in Google Cloud Console
3. Re-enable the Cloud Build trigger

**For now:** All development and testing is done locally, which works perfectly ✅

---

## 📚 More Information

- **Technical Guide**: [docs/TECH_GUIDE.md](docs/TECH_GUIDE.md) — For IT staff (deployment, logs, env vars)
- **Architecture**: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — System diagram and component notes
- **Troubleshooting**: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — Detailed fixes for common issues
- **Cloud Run Info**: [CLOUD_RUN_EXPLANATION.md](CLOUD_RUN_EXPLANATION.md) — Cloud Run deployment details and configuration

---

## 📋 Version History

- **v2.0** (Current) - Improved scope detection, citation rendering, answer synthesis
  - Enhanced company name filtering (METROBRA, DGC, etc.)
  - Beautiful clickable citation badges with PDF viewer integration
  - Improved answer generation with better synthesis prompts
  - Local development optimized (Python 3.11)

**Stable Version:** `pdf-viewer-and-answers-satisfactory` tag
- All features working and tested locally
- Ready for production deployment when Cloud Run is configured

---

*Last updated: 2025-01-07*  
*Version: 2.0*
