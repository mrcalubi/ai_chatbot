# Vietnamese Reports Strategy - Executive Summary

**Last Updated**: 2025-01-07  
**Status**: Ready for Implementation

---

## ✅ Key Decisions Made

### 1. Translation Provider: **OpenAI GPT-4o-mini** (Recommended) ✅
- **Cost**: ~$0.02 per PDF (cheapest option!)
- **Quality**: Excellent, best for financial terminology
- **100 PDFs**: **~$2.00** (best value!)
- **Fallback**: Yandex if OpenAI unavailable

**Cost Comparison** (100 PDFs):
- OpenAI GPT-4o-mini: **~$2.00** ✅ Recommended (cheapest AND best quality!)
- Yandex: ~$40 (20x more expensive)
- Google Cloud: ~$200 (100x more expensive)

### 2. Translation Timing: **Batch During Indexing**
- Translate all chunks when PDF is uploaded
- Store both original Vietnamese and English translation
- **Zero cost per query** (already translated)
- Supports instant toggle between languages

### 3. Citation Display: **Toggle Feature** ✅
- **Default**: Show English translation
- **Toggle button**: Switch to Vietnamese original
- **Visual indicator**: Language badge
- Perfect for impressing bosses and Vietnamese analyst

### 4. Storage: **Single Index with Both Languages**
- Store translated English text in main index (for search)
- Store original Vietnamese in metadata
- ~2x storage (negligible cost)
- Fast retrieval, supports instant toggle

---

## 🎯 Implementation Phases

### Phase 1: Foundation (Week 1)
1. Add language detection (`langdetect`)
2. Switch to multilingual embedding model (`intfloat/multilingual-e5-base`)
3. Add Vietnamese OCR support (Tesseract `vie` model)
4. Set up Yandex Translate API

### Phase 2: Translation & Indexing (Week 2)
5. Implement batch translation during indexing
6. Store both original and translated text
7. Update metadata schema
8. Test translation quality

### Phase 3: UI & Integration (Week 3)
9. Implement citation toggle (Vietnamese ↔ English)
10. Update prompts for multilingual context
11. Testing with Vietnamese broker reports
12. Performance optimization

---

## 💰 Cost Summary

| Item | Cost | Notes |
|------|------|-------|
| **Translation (OpenAI)** | ~$0.02/PDF | One-time, during indexing (cheapest!) |
| **Storage** | <$0.01/100 PDFs | Negligible |
| **Per Query** | **$0** | Already translated ✅ |
| **Toggle Feature** | **$0** | Just displays stored text ✅ |

**100 Vietnamese PDFs**: ~$2.00 total (one-time cost) ✅ Best value!

---

## 🎨 Features

### For Bosses
- ✅ Professional bilingual toggle
- ✅ Seamless English answers
- ✅ Polished, impressive UI

### For Vietnamese Analyst
- ✅ Can view original Vietnamese text
- ✅ Toggle between languages instantly
- ✅ No quality loss in original text

### For System
- ✅ Batch translation (efficient)
- ✅ Zero query-time translation cost
- ✅ Fast retrieval (pre-translated)
- ✅ Scalable architecture

---

## 📋 Next Steps

1. **Review strategy document**: `docs/VIETNAMESE_REPORTS_STRATEGY.md`
2. **Get Yandex API key**: https://translate.yandex.com/developers
3. **Test with sample Vietnamese PDFs** (you mentioned you have them)
4. **Begin Phase 1 implementation**

---

## 🚀 Ready to Proceed?

All decisions made, strategy finalized. Ready to start implementation when you give the green light! 🟢

