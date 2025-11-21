# Strategy: Vietnamese Report Support

**Goal**: Enable the chatbot to read, analyze, and answer questions about Vietnamese PDF reports with the same quality as English reports, while returning answers in English.

---

## 1. Overview

### Current System
- **Text Extraction**: PyMuPDF (fitz) for text extraction
- **Embedding Model**: `sentence-transformers/all-MiniLM-L6-v2` (English-focused, 384-dim)
- **OCR**: pytesseract (Tesseract OCR) - optional, English-focused
- **Translation**: Not implemented
- **Language Detection**: Not implemented

### Requirements
1. ✅ Read and analyze Vietnamese PDFs with same quality as English
2. ✅ Answers always in English
3. ✅ Citations in English (translate cited PDF sections)
4. ✅ Preserve original Vietnamese text for reference
5. ✅ Support Vietnamese stock broker PDFs

---

## 2. Architecture Strategy

### 2.1 Language Detection & Handling

**Approach**: Detect language during PDF upload and handle accordingly.

```python
# Pseudo-code structure
def detect_pdf_language(pdf_path: str) -> str:
    """
    Detect if PDF is Vietnamese or English (or mixed).
    Returns: 'vi', 'en', or 'mixed'
    """
    # Sample first few pages
    # Use language detection library (langdetect, fasttext, etc.)
    # Return dominant language
```

**Implementation Options**:
- **Option A**: `langdetect` library (lightweight, fast)
- **Option B**: `fasttext` (more accurate, multilingual)
- **Option C**: Simple heuristic (Vietnamese character detection)

**Recommendation**: Start with `langdetect` for speed, upgrade to `fasttext` if accuracy issues.

---

### 2.2 Text Extraction for Vietnamese

#### Current Flow
1. PyMuPDF extracts text directly
2. Optional OCR for empty pages (pytesseract)

#### New Flow for Vietnamese
1. **Primary**: PyMuPDF extraction (works for Vietnamese text if PDF has text layer)
2. **Fallback**: Enhanced OCR with Vietnamese support
   - **Option 1**: Tesseract with Vietnamese language pack (`vie` model)
   - **Option 2**: VietOCR (specialized for Vietnamese)
   - **Option 3**: Google Cloud Vision API (high accuracy, paid)
   - **Option 4**: EasyOCR with Vietnamese support (good balance)

**Recommendation**: 
- **Phase 1**: Tesseract with `vie` language pack (free, already have pytesseract)
- **Phase 2**: Evaluate EasyOCR if Tesseract accuracy insufficient

**Code Changes**:
```python
# In app/rag.py
def _ocr_pages_if_needed(path: str, page_idxs: List[int], language: str = 'eng') -> Dict[int, str]:
    """
    OCR with language support.
    language: 'eng', 'vie', or auto-detect
    """
    if language == 'vie':
        # Use Vietnamese Tesseract model
        pytesseract.image_to_string(image, lang='vie')
    else:
        # Default English
        pytesseract.image_to_string(image, lang='eng')
```

---

### 2.3 Embedding Model for Vietnamese

#### Current Model
- `all-MiniLM-L6-v2`: English-focused, 384-dim
- Works for English but may struggle with Vietnamese semantic meaning

#### Multilingual Embedding Options

**Option 1: multilingual-e5-base (Recommended for Start)**
- Model: `intfloat/multilingual-e5-base`
- Supports: 100+ languages including Vietnamese
- Dimensions: 768
- Performance: Excellent for multilingual retrieval
- Size: ~560MB

**Option 2: paraphrase-multilingual-mpnet-base-v2**
- Model: `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`
- Supports: 50+ languages including Vietnamese
- Dimensions: 768
- Performance: Very good multilingual
- Size: ~420MB

**Option 3: Keep Current + Add Vietnamese Model**
- Use `all-MiniLM-L6-v2` for English
- Use `keepitreal/vietnamese-sbert` for Vietnamese
- Requires dual indexing or language routing

**Recommendation**: 
- **Phase 1**: Switch to `multilingual-e5-base` (single model, supports both languages)
- **Phase 2**: If performance issues, consider fine-tuning on financial Vietnamese text

**Code Changes**:
```python
# In app/rag.py
EMBED_MODEL = os.getenv(
    "EMBED_MODEL", 
    "intfloat/multilingual-e5-base"  # Changed from all-MiniLM-L6-v2
)
```

**Migration Strategy**:
- Existing English indexes remain compatible (may need re-indexing for optimal performance)
- New Vietnamese PDFs indexed with multilingual model
- Hybrid search works across both languages

---

### 2.4 Translation Strategy

#### Requirements
1. **Answers**: Always in English
2. **Citations**: Show translated text in English
3. **Original Text**: Preserved for reference (optional display)

#### Translation Approaches (Cost Comparison)

**Option A: OpenAI GPT-4o-mini (Recommended - Best Value) ✅**
- **Highest quality**, best contextual understanding
- Excellent for financial terminology
- **Cost**: Input ~$0.15/1M tokens, Output ~$0.60/1M tokens
- **Per PDF** (100K chars ≈ 25K input + 20K output tokens): **~$0.02**
- **100 PDFs**: **~$2.00** (cheapest option!)
- Best accuracy AND best cost - you were absolutely right! 🎯

**Option B: Yandex Translate API**
- Good quality for general Vietnamese → English
- Fast batch processing
- Cost: **$3.94 per 1M characters**
- Cost per PDF: **~$0.40** (20x more expensive than OpenAI!)
- **100 PDFs**: **~$40**

**Option C: Google Cloud Translation API**
- Good quality, reliable
- Cost: **$20 per 1M characters**
- Cost per PDF: **~$2.00** (100x more expensive than OpenAI!)
- **100 PDFs**: **~$200**

**Option D: LibreTranslate (Self-hosted, Free)**
- Free, open-source
- Lower quality, may struggle with financial terms
- Good for testing/development
- Requires self-hosting infrastructure

**Recommendation**: 
- **Primary**: **OpenAI GPT-4o-mini** ✅ (cheapest AND best quality - corrected!)
- **Fallback**: Yandex (if OpenAI API unavailable)
- **Testing**: LibreTranslate (for development)

**Cost Comparison Table** (Corrected):
| Service | Cost per PDF | Cost per 100 PDFs | Quality | Financial Terms |
|---------|--------------|-------------------|---------|-----------------|
| **OpenAI GPT-4o-mini** | **~$0.02** ✅ | **~$2.00** ✅ | Excellent | Excellent |
| Yandex | ~$0.40 | ~$40 (20x more!) | Good | Moderate |
| Google Cloud | ~$2.00 | ~$200 (100x more!) | Very Good | Good |
| LibreTranslate | Free | Free | Fair | Poor |

**Why OpenAI is Cheapest**:
- Pricing by token (not character) is more efficient
- Vietnamese → English translation: ~25K input + 20K output tokens per PDF
- Token pricing: $0.015 input + $0.012 output = **~$0.027 per PDF**
- Character-based APIs charge for every character, including spaces/punctuation
- **OpenAI is ~20x cheaper than Yandex and ~100x cheaper than Google Cloud!**

**Implementation Strategy**:
1. **Use OpenAI GPT-4o-mini** - Best quality AND cheapest option ✅ (corrected!)
2. **Batch translate during indexing** - Store both original and translated text
3. **Fallback to Yandex** only if OpenAI API unavailable
4. **Optimize prompts** for financial terminology translation

#### Implementation Flow

**During Indexing** (Optional - Batch Translation):
```python
def index_vietnamese_pdf(pdf_path: str):
    # 1. Extract Vietnamese text
    vietnamese_chunks = extract_and_chunk(pdf_path)
    
    # 2. Translate to English
    english_chunks = translate_chunks(vietnamese_chunks)
    
    # 3. Index both (store mapping)
    index_chunks(english_chunks, meta={'original_vi': vietnamese_chunks})
```

**During Retrieval** (On-demand Translation):
```python
def retrieve_and_translate(query: str):
    # 1. Search in Vietnamese index (multilingual model handles this)
    results = search(query)  # Model understands both languages
    
    # 2. Translate retrieved chunks to English
    english_context = translate_chunks(results['text'])
    
    # 3. Pass to LLM for answer generation
    answer = generate_answer(query, english_context)
    
    # 4. Return answer + translated citations
    return answer, english_context
```

**Translation Function (Multi-Provider Support)**:
```python
import os
from typing import Optional

TRANSLATION_PROVIDER = os.getenv("TRANSLATION_PROVIDER", "openai")  # openai (recommended), yandex, google

def translate_text(text: str, source_lang: str = 'vi', target_lang: str = 'en') -> str:
    """
    Translate text using configured provider (Yandex, Google Cloud, or OpenAI).
    Falls back to next provider if primary fails.
    """
    provider = TRANSLATION_PROVIDER.lower()
    
    # Try primary provider (OpenAI recommended - cheapest AND best quality)
    if provider == "openai":
        try:
            return translate_openai(text, source_lang, target_lang)
        except Exception as e:
            print(f"OpenAI translation failed: {e}, trying Yandex...")
            provider = "yandex"
    
    if provider == "yandex":
        try:
            return translate_yandex(text, source_lang, target_lang)
        except Exception as e:
            print(f"Yandex translation failed: {e}, trying Google Cloud...")
            provider = "google"
    
    if provider == "google":
        return translate_google(text, source_lang, target_lang)
    
    raise ValueError(f"Unsupported translation provider: {provider}")

def translate_yandex(text: str, source_lang: str, target_lang: str) -> str:
    """Yandex Translate API - fallback option (20x more expensive than OpenAI)"""
    import requests
    api_key = os.getenv("YANDEX_TRANSLATE_API_KEY")
    url = "https://translate.yandex.net/api/v1.5/tr.json/translate"
    params = {
        "key": api_key,
        "text": text,
        "lang": f"{source_lang}-{target_lang}",
        "format": "plain"
    }
    response = requests.post(url, params=params)
    response.raise_for_status()
    return response.json()["text"][0]

def translate_google(text: str, source_lang: str, target_lang: str) -> str:
    """Google Cloud Translation API - balanced option"""
    from google.cloud import translate_v2 as translate
    client = translate.Client()
    result = client.translate(text, source_language=source_lang, target_language=target_lang)
    return result['translatedText']

def translate_openai(text: str, source_lang: str, target_lang: str) -> str:
    """OpenAI GPT-4o-mini - premium quality option"""
    prompt = f"""Translate the following Vietnamese financial report text to English. 
    Preserve:
    - Numbers, dates, percentages
    - Company names, stock codes
    - Financial terminology (translate accurately)
    - Table structures if present
    
    Vietnamese text:
    {text}
    
    English translation:"""
    
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1
    )
    return response.choices[0].message.content
```

---

### 2.5 Storage Strategy

#### Current Storage
- **Texts**: `index/texts.npy` (numpy array)
- **Metadata**: `index/meta.npy` (document, page, chunk info)
- **FAISS Index**: `index/faiss.index` (vector embeddings)

#### New Storage Requirements
1. **Original Vietnamese text** (for reference)
2. **Translated English text** (for indexing and retrieval)
3. **Language tag** (vi/en/mixed)
4. **Translation metadata** (translation timestamp, model used)

#### Proposed Schema

**Enhanced Metadata**:
```python
{
    "doc": "VGC_2024_Q3_Report.pdf",
    "page": 5,
    "chunk_idx": 2,
    "language": "vi",  # NEW
    "text_original": "Báo cáo tài chính quý 3...",  # NEW (Vietnamese)
    "text_translated": "Financial report Q3...",  # NEW (English)
    "translation_model": "gpt-4o-mini",  # NEW
    "translated_at": "2025-01-07T10:30:00Z"  # NEW
}
```

**Storage Options**:

**Option A: Store Both in Same Index (RECOMMENDED)**
- Store translated text in `texts.npy` (used for search)
- Store original Vietnamese text in metadata
- Batch translate during indexing (one-time cost)
- Pros: Simple, single index, fast retrieval, supports toggle
- Cons: Larger metadata files (~2x size)
- **Selected**: ✅ Meets all requirements, supports toggle feature

**Option B: Dual Indexing**
- English index: Translated chunks
- Vietnamese index: Original chunks (optional, for advanced search)
- Pros: Can search in either language
- Cons: More complex, 2x storage, 2x indexing time

**Option C: Lazy Translation with Caching**
- Store Vietnamese text only
- Translate on-demand during retrieval
- Cache translations in Redis/file
- Pros: Efficient storage
- Cons: Translation delay during retrieval, doesn't support instant toggle

**Recommendation**: **Option A** (store both, single index)
- ✅ Simpler implementation
- ✅ Fast retrieval (no translation delay)
- ✅ Supports instant toggle between languages
- ✅ Batch translation during indexing (cheaper, one-time cost)
- ✅ Perfect for impressing bosses with bilingual toggle feature

---

### 2.6 Citation Display Strategy

#### Current Citation Format
```
(Filename.pdf, p.5)
```

#### Enhanced Citation for Vietnamese PDFs (with Toggle)

**Recommended: Toggle Between English and Vietnamese**
- **Default**: Show English translation
- **Toggle button**: Allow user to switch to Vietnamese original
- **Visual indicator**: Show language badge when viewing translated content

**Citation Display Options**:

**Default View (English)**:
```
📄 VGC 2024 Q3 Report.pdf, p.5
Financial report Q3 shows revenue of...
[View in Vietnamese] ← Toggle button
```

**Vietnamese View (Toggle)**:
```
📄 VGC 2024 Q3 Report.pdf, p.5
Báo cáo tài chính quý 3 cho thấy doanh thu...
[View in English] ← Toggle button
[Translated] ← Badge indicator
```

**Implementation**:
```python
# In citation rendering (web/index.html)
def format_citation(citation: Citation, show_original: bool = False):
    base = f"({citation.doc}, p.{citation.page})"
    
    if citation.language == 'vi':
        # Vietnamese PDF - show toggle
        if show_original:
            # Show Vietnamese original
            text = citation.original_text
            toggle_text = "View in English"
            badge = '<span class="lang-badge">[Vietnamese Original]</span>'
        else:
            # Show English translation (default)
            text = citation.translated_text
            toggle_text = "View in Vietnamese"
            badge = '<span class="lang-badge translated">[Translated]</span>'
        
        return f"""
        <span class="citation-with-toggle">
            {base}
            <span class="citation-text">{text}</span>
            {badge}
            <button class="toggle-lang-btn" onclick="toggleCitationLang('{citation.id}')">
                {toggle_text}
            </button>
        </span>
        """
    else:
        # English PDF - no toggle needed
        return f"{base} {citation.text}"

# In frontend (web/index.html)
function toggleCitationLang(citationId) {
    // Toggle between Vietnamese original and English translation
    const citation = document.getElementById(`citation-${citationId}`);
    citation.classList.toggle('showing-original');
    // Update button text and badge
}
```

**UI/UX Design**:
- **Toggle button**: Small, unobtrusive button next to citation
- **Language badge**: Subtle indicator showing "[Translated]" or "[Vietnamese Original]"
- **Smooth transition**: Fade between languages
- **Persist preference**: Remember user's language preference per session

**Recommendation**: **Toggle Implementation**
- Impresses bosses with bilingual capability ✅
- Allows Vietnamese analyst to see original text ✅
- Defaults to English (meets requirement) ✅
- Professional, polished feature ✅

---

## 3. Implementation Plan

### Phase 1: Foundation (Week 1)
1. ✅ Add language detection
   - Install `langdetect` or `fasttext`
   - Detect language during PDF upload
   - Store language in metadata

2. ✅ Switch to multilingual embedding model
   - Update `EMBED_MODEL` to `intfloat/multilingual-e5-base`
   - Re-index existing English PDFs (optional, for optimal performance)
   - Test with Vietnamese sample PDFs

3. ✅ Enhanced OCR for Vietnamese
   - Install Tesseract Vietnamese language pack (`vie`)
   - Update `_ocr_pages_if_needed()` to support Vietnamese
   - Test on scanned Vietnamese PDFs

### Phase 2: Translation (Week 2)
4. ✅ Implement translation function
   - Create `translate_text()` using OpenAI
   - Add translation caching (file-based or Redis)
   - Optimize for financial terminology

5. ✅ Update indexing pipeline
   - Detect Vietnamese PDFs
   - Extract Vietnamese text
   - Translate to English
   - Index both original and translated text

6. ✅ Update retrieval pipeline
   - Search using multilingual model (handles both languages)
   - Return translated text in context
   - Preserve original text in metadata

### Phase 3: Integration (Week 3)
7. ✅ Update prompts for translation
   - Ensure LLM knows context is translated
   - Maintain citation format
   - Keep answers in English

8. ✅ Update citation rendering
   - Show English translations in citations
   - Optional: Add language indicator
   - Preserve original text for reference

9. ✅ Testing & Optimization
   - Test with Vietnamese broker reports
   - Compare answer quality vs English reports
   - Optimize translation caching
   - Performance tuning

---

## 4. Technical Details

### 4.1 Dependencies to Add

```txt
# Language detection
langdetect==1.0.9
# OR
fasttext==0.9.2

# Tesseract Vietnamese support (system package)
# Ubuntu/Debian: sudo apt-get install tesseract-ocr-vie
# macOS: brew install tesseract-lang
# Windows: Download from GitHub

# Optional: EasyOCR for better Vietnamese OCR
easyocr==1.7.0
```

### 4.2 Environment Variables

```bash
# Embedding model (multilingual)
EMBED_MODEL=intfloat/multilingual-e5-base

# Language detection
AUTO_DETECT_LANGUAGE=true

# Translation (Batch during indexing)
TRANSLATION_ENABLED=true
TRANSLATION_PROVIDER=openai  # Options: openai (recommended - cheapest!), yandex, google
TRANSLATION_BATCH_ENABLED=true  # Batch translate during indexing

# Translation API Keys
OPENAI_API_KEY=your_openai_key  # For OpenAI (recommended - cheapest AND best quality)
YANDEX_TRANSLATE_API_KEY=your_yandex_key  # For Yandex (fallback)
GOOGLE_APPLICATION_CREDENTIALS=/path/to/google-credentials.json  # For Google Cloud (fallback)

# Translation caching (optional, for on-demand fallback)
TRANSLATION_CACHE_ENABLED=true
TRANSLATION_CACHE_DIR=./cache/translations

# Citation toggle
CITATION_TOGGLE_ENABLED=true  # Enable Vietnamese/English toggle in citations
DEFAULT_CITATION_LANGUAGE=en  # Default: 'en' (English)

# OCR
OCR_ENABLED=true
OCR_LANGUAGE=vie  # Vietnamese, or 'eng' for English, 'vie+eng' for both
```

### 4.3 Code Structure Changes

**New Files**:
- `app/translation.py` - Translation functions (Yandex/Google/OpenAI)
- `app/language_detection.py` - Language detection utilities
- `app/citation_toggle.py` - Citation toggle logic (optional helper)

**Modified Files**:
- `app/rag.py` - Add language detection, batch translation during indexing
- `app/main.py` - Update upload endpoint for language handling, batch translation
- `app/schemas.py` - Add language field and translation fields to metadata
- `web/index.html` - Add citation toggle UI (Vietnamese ↔ English)
- `requirements.txt` - Add new dependencies (langdetect, yandex-translate, google-cloud-translate)

---

## 5. Testing Strategy

### 5.1 Test Cases

1. **Vietnamese PDF Upload**
   - Upload Vietnamese broker report
   - Verify language detection
   - Verify text extraction (PyMuPDF + OCR fallback)
   - Verify translation and indexing

2. **Vietnamese Query (English Answer)**
   - Query: "Báo cáo doanh thu quý 3" (Revenue report Q3)
   - Expected: Answer in English, citations in English
   - Verify translation quality

3. **English Query on Vietnamese PDF**
   - Query: "What was the revenue in Q3?"
   - Expected: Finds relevant Vietnamese content, answers in English
   - Verify multilingual embedding works

4. **Mixed Language PDFs**
   - PDF with both Vietnamese and English sections
   - Verify language detection handles mixed
   - Verify translation only applies to Vietnamese parts

5. **Citation Accuracy**
   - Verify citations show English translations
   - Verify page numbers are correct
   - Verify original text preserved in metadata

### 5.2 Quality Metrics

- **Translation Quality**: Manual review of 20 sample chunks
- **Retrieval Accuracy**: Compare Vietnamese vs English retrieval for same concepts
- **Answer Quality**: Compare answers from Vietnamese PDFs vs English PDFs
- **Performance**: Translation latency, indexing time

---

## 6. Cost Estimation

### Translation Costs Comparison (Batch During Indexing)

**OpenAI GPT-4o-mini (Recommended - Best Value) ✅**:
- **Cost**: Input ~$0.15/1M tokens, Output ~$0.60/1M tokens
- **Average PDF**: ~50 pages, ~200 chunks, ~100K characters
- **Token conversion**: 100K chars ≈ 25K input + 20K output tokens
- **Per PDF translation**: **~$0.02** (one-time indexing cost)
- **100 PDFs**: **~$2.00** (cheapest option!)
- **Quality**: Excellent, best for financial terminology

**Yandex Translate API**:
- **Cost**: $3.94 per 1M characters
- **Per PDF translation**: **~$0.40** (one-time)
- **100 PDFs**: **~$40** (20x more expensive!)
- **Quality**: Good for general text

**Google Cloud Translation API**:
- **Cost**: $20 per 1M characters
- **Per PDF translation**: **~$2.00** (one-time)
- **100 PDFs**: **~$200** (100x more expensive!)
- **Quality**: Very good

### Storage Costs
- **Additional storage**: ~2x (original + translated text)
- **Current**: ~10MB per 100 PDFs
- **With translation**: ~20MB per 100 PDFs (negligible, <$0.01)

### Total Estimated Cost (100 Vietnamese PDFs)

| Provider | Translation Cost | Storage Cost | Total |
|----------|------------------|--------------|-------|
| **OpenAI GPT-4o-mini** | **~$2.00** ✅ | <$0.01 | **~$2.00** ✅ |
| Yandex | ~$40 | <$0.01 | ~$40 |
| Google Cloud | ~$200 | <$0.01 | ~$200 |

**Recommendation**: Use **OpenAI GPT-4o-mini** - cheapest AND best quality! ✅

### Per-Query Costs
- **With batch translation**: **$0** (already translated during indexing)
- **Toggle feature**: **$0** (just displays stored text)
- **No ongoing translation costs** after indexing ✅

---

## 7. Risks & Mitigations

### Risk 1: Translation Quality
- **Risk**: Financial terminology mistranslated
- **Mitigation**: 
  - Use GPT-4o-mini (better context understanding)
  - Add financial terminology glossary
  - Manual review of sample translations
  - Fine-tune translation prompts

### Risk 2: Multilingual Embedding Performance
- **Risk**: Lower retrieval accuracy vs English-only model
- **Mitigation**:
  - Test with sample Vietnamese PDFs
  - Compare retrieval quality
  - Consider fine-tuning on financial Vietnamese text if needed

### Risk 3: OCR Accuracy for Vietnamese
- **Risk**: Poor text extraction from scanned PDFs
- **Mitigation**:
  - Test multiple OCR engines (Tesseract, EasyOCR)
  - Use higher DPI for Vietnamese (300+)
  - Fallback to manual review if needed

### Risk 4: Cost Overruns
- **Risk**: High translation costs for large document sets
- **Mitigation**:
  - Implement aggressive caching
  - Batch translation during off-peak
  - Monitor costs with usage tracking

---

## 8. Future Enhancements

1. **Fine-tuned Translation Model**: Train on financial Vietnamese → English
2. **Bidirectional Search**: Search in Vietnamese, get English answers (and vice versa)
3. **Multi-language Support**: Extend to other languages (Thai, Indonesian, etc.)
4. **Translation Quality Scoring**: Automatically flag low-confidence translations
5. **Original Text Display**: Toggle to show original Vietnamese in citations

---

## 9. Success Criteria

✅ **Phase 1 Complete When**:
- Vietnamese PDFs can be uploaded and indexed
- Language is automatically detected
- Multilingual embedding model is working
- OCR supports Vietnamese

✅ **Phase 2 Complete When**:
- Vietnamese text is translated to English
- Translations are cached
- Indexing includes both original and translated text

✅ **Phase 3 Complete When**:
- Answers from Vietnamese PDFs are in English
- Citations show English translations
- Quality matches English PDF answers
- Performance is acceptable (<5s query time)

---

## 10. Next Steps

1. **Review this strategy** with team
2. **Get approval** to proceed with Phase 1
3. **Set up test environment** with sample Vietnamese PDFs
4. **Begin Phase 1 implementation** (language detection + multilingual model)
5. **Test and iterate** before moving to Phase 2

---

*Document Version: 1.0*  
*Last Updated: 2025-01-07*  
*Status: Planning Phase*

