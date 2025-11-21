# app/language_detection.py
"""
Language detection utilities for PDF processing.
Detects if a PDF contains Vietnamese, English, or mixed content.
"""

import os
from typing import Optional, List
from pathlib import Path

try:
    from langdetect import detect, detect_langs, LangDetectException
    LANGDETECT_AVAILABLE = True
except ImportError:
    LANGDETECT_AVAILABLE = False
    print("[language_detection] Warning: langdetect not available. Install with: pip install langdetect")


def detect_pdf_language(pdf_path: str, sample_pages: int = 3) -> str:
    """
    Detect the primary language of a PDF by sampling text from first few pages.
    
    Args:
        pdf_path: Path to PDF file
        sample_pages: Number of pages to sample (default: 3)
    
    Returns:
        Language code: 'vi' (Vietnamese), 'en' (English), or 'mixed'
    """
    if not LANGDETECT_AVAILABLE:
        # Fallback: simple heuristic based on Vietnamese characters
        return _detect_vietnamese_heuristic(pdf_path, sample_pages)
    
    try:
        import fitz  # PyMuPDF
        
        texts: List[str] = []
        with fitz.open(pdf_path) as doc:
            # Sample first few pages
            num_pages = min(sample_pages, len(doc))
            for i in range(num_pages):
                page = doc[i]
                page_text = page.get_text("text") or ""
                if len(page_text.strip()) > 50:  # Only use pages with substantial text
                    texts.append(page_text[:2000])  # Limit to first 2000 chars per page
        
        if not texts:
            return 'en'  # Default to English if no text found
        
        # Combine sampled text
        combined_text = " ".join(texts)
        if len(combined_text.strip()) < 50:
            return 'en'  # Not enough text to detect
        
        try:
            # Detect language
            detected_lang = detect(combined_text)
            
            # Get confidence scores for all languages
            lang_probs = detect_langs(combined_text)
            
            # Check if Vietnamese is detected
            vi_prob = next((lp.prob for lp in lang_probs if lp.lang == 'vi'), 0.0)
            en_prob = next((lp.prob for lp in lang_probs if lp.lang == 'en'), 0.0)
            
            # Determine primary language
            if detected_lang == 'vi':
                if vi_prob > 0.7:  # High confidence Vietnamese
                    return 'vi'
                elif vi_prob > 0.3 and en_prob > 0.3:  # Mixed
                    return 'mixed'
                else:
                    return 'vi' if vi_prob > en_prob else 'en'
            elif detected_lang == 'en':
                if vi_prob > 0.3:  # Significant Vietnamese content
                    return 'mixed' if en_prob > 0.4 else 'vi'
                else:
                    return 'en'
            else:
                # Other language detected - check for Vietnamese characters as fallback
                if _has_vietnamese_chars(combined_text):
                    return 'vi' if vi_prob > 0.3 else 'mixed'
                return 'en'  # Default to English for other languages
        
        except LangDetectException:
            # Fallback to heuristic if langdetect fails
            return _detect_vietnamese_heuristic(pdf_path, sample_pages)
    
    except Exception as e:
        print(f"[language_detection] Error detecting language: {e}")
        # Fallback to heuristic
        return _detect_vietnamese_heuristic(pdf_path, sample_pages)


def _detect_vietnamese_heuristic(pdf_path: str, sample_pages: int = 3) -> str:
    """
    Simple heuristic to detect Vietnamese based on character patterns.
    Vietnamese has distinctive diacritics (ă, â, ê, ô, ơ, ư, đ, etc.)
    """
    try:
        import fitz  # PyMuPDF
        
        vietnamese_chars = set('ăăââêêôôơơưưữưỂắắằằẳẳẵẵặặầầấấẩẩẫẫậậềềếếểểễễệệồồốốổổỗỗộộờờớớởởỡỡợợừừứứửửữữựựđĐ')
        vietnamese_count = 0
        total_chars = 0
        
        with fitz.open(pdf_path) as doc:
            num_pages = min(sample_pages, len(doc))
            for i in range(num_pages):
                page = doc[i]
                page_text = page.get_text("text") or ""
                if len(page_text.strip()) > 50:
                    # Check for Vietnamese characters
                    for char in page_text:
                        if char.isalpha():
                            total_chars += 1
                            if char in vietnamese_chars or ord(char) in range(0x1EA0, 0x1EF9):  # Vietnamese Unicode range
                                vietnamese_count += 1
        
        if total_chars == 0:
            return 'en'  # Default
        
        vietnamese_ratio = vietnamese_count / total_chars if total_chars > 0 else 0
        
        if vietnamese_ratio > 0.05:  # More than 5% Vietnamese characters
            return 'vi' if vietnamese_ratio > 0.2 else 'mixed'
        else:
            return 'en'
    
    except Exception as e:
        print(f"[language_detection] Heuristic detection failed: {e}")
        return 'en'  # Default to English


def _has_vietnamese_chars(text: str) -> bool:
    """Quick check if text contains Vietnamese characters."""
    vietnamese_chars = set('ăăââêêôôơơưưữưỂắắằằẳẳẵẵặặầầấấẩẩẫẫậậềềếếểểễễệệồồốốổổỗỗộộờờớớởởỡỡợợừừứứửửữữựựđĐ')
    # Check for Vietnamese Unicode range
    for char in text:
        if char in vietnamese_chars or (0x1EA0 <= ord(char) <= 0x1EF9):
            return True
    return False


def detect_text_language(text: str) -> str:
    """
    Detect language of a text string.
    
    Args:
        text: Text to detect language for
    
    Returns:
        Language code: 'vi', 'en', or 'mixed'
    """
    if not text or len(text.strip()) < 20:
        return 'en'  # Default for short text
    
    if not LANGDETECT_AVAILABLE:
        # Fallback heuristic
        if _has_vietnamese_chars(text):
            return 'vi'
        return 'en'
    
    try:
        detected = detect(text)
        
        # Get probabilities
        lang_probs = detect_langs(text)
        vi_prob = next((lp.prob for lp in lang_probs if lp.lang == 'vi'), 0.0)
        en_prob = next((lp.prob for lp in lang_probs if lp.lang == 'en'), 0.0)
        
        if detected == 'vi':
            return 'vi' if vi_prob > 0.6 else ('mixed' if en_prob > 0.3 else 'vi')
        elif detected == 'en':
            return 'mixed' if vi_prob > 0.3 else 'en'
        else:
            return 'vi' if vi_prob > 0.3 else 'en'
    
    except Exception:
        # Fallback
        return 'vi' if _has_vietnamese_chars(text) else 'en'


