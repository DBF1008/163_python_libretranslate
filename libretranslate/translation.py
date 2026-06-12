"""Unified translation service — consolidates the pre/post processing
logic that was previously scattered across route handlers in app.py.

Every translation entry point (/translate, /translate_file, /detect)
should go through this module so that auto-detection, emoji handling,
formatting, caching and response building stay consistent.
"""

from html import unescape

from argostranslatefiles.translatehtml import translate_html

from libretranslate.language import (
    detect_languages,
    get_language_with_fallback,
    improve_translation_formatting,
    model2iso,
)


# ---------------------------------------------------------------------------
# Emoji character set (moved from app.py closure scope)
# ---------------------------------------------------------------------------

_EMOJIS = {e: True for e in
    [ord(' ')] +                    # Spaces
    list(range(0x1F600, 0x1F64F)) + # Emoticons
    list(range(0x1F300, 0x1F5FF)) + # Misc Symbols and Pictographs
    list(range(0x1F680, 0x1F6FF)) + # Transport and Map
    list(range(0x2600, 0x26FF)) +   # Misc symbols
    list(range(0x2700, 0x27BF)) +   # Dingbats
    list(range(0xFE00, 0xFE0F)) +   # Variation Selectors
    list(range(0x1F900, 0x1F9FF)) + # Supplemental Symbols and Pictographs
    list(range(0x1F1E6, 0x1F1FF)) + # Flags
    list(range(0x20D0, 0x20FF))     # Combining Diacritical Marks for Symbols
}


# ---------------------------------------------------------------------------
# Pure helper functions (moved from app.py closure scope)
# ---------------------------------------------------------------------------

def detect_translatable(src_texts):
    """Return True if any text in *src_texts* contains at least one
    non-emoji character (i.e. is actually translatable)."""
    if isinstance(src_texts, list):
        return any(detect_translatable(t) for t in src_texts)
    for ch in src_texts:
        if ord(ch) not in _EMOJIS:
            return True
    return False


def filter_unique(seq, extra):
    """Return *seq* with duplicates, empty strings and *extra* removed."""
    seen = set({extra, ""})
    seen_add = seen.add
    return [x for x in seq if not (x in seen or seen_add(x))]


# ---------------------------------------------------------------------------
# Domain exception — keeps Flask out of the service layer
# ---------------------------------------------------------------------------

class TranslationError(Exception):
    """Raised by :class:`TranslationService` for expected, user-facing
    error conditions.  Route handlers should catch this and translate
    it into the appropriate HTTP response."""

    def __init__(self, message, reason="unsupported"):
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Translation service
# ---------------------------------------------------------------------------

class TranslationService:
    """Stateless-ish service that holds references to the loaded
    languages and the translation cache.  All translation entry points
    should call methods on this object instead of duplicating logic."""

    def __init__(self, languages, trans_cache):
        self.languages = languages
        self.trans_cache = trans_cache

    # -- limits -------------------------------------------------------------

    def enforce_char_limit(self, texts, char_limit):
        """Raise :class:`TranslationError` if any text exceeds *char_limit*.

        A *char_limit* of -1 means unlimited.
        """
        if char_limit == -1:
            return
        for text in texts:
            if len(text) > char_limit:
                raise TranslationError(
                    "Invalid request: request (%d) exceeds text limit (%d)"
                    % (len(text), char_limit),
                    reason="char_limit",
                )

    # -- language resolution ------------------------------------------------

    def resolve_source_language(self, source_lang_code, src_texts):
        """Resolve the source language, running auto-detection when
        *source_lang_code* is ``"auto"``.

        Returns a ``(src_lang, detected_info, is_translatable)`` tuple:

        * *src_lang* — the argostranslate language object to translate from.
        * *detected_info* — a ``{"confidence": float, "language": str}`` dict
          (always in **model** code, the caller converts with ``model2iso``).
        * *is_translatable* — ``False`` when the text is emoji-only.
        """
        is_translatable = detect_translatable(src_texts)

        if not is_translatable:
            detected_info = {"confidence": 0.0, "language": "en"}
            src_lang = next(
                (l for l in self.languages if l.code == "en"), None
            )
            return src_lang, detected_info, False

        if source_lang_code == "auto":
            candidate_langs = detect_languages(src_texts)
            detected_info = candidate_langs[0]
            src_lang = get_language_with_fallback(
                detected_info["language"], self.languages
            )
        else:
            detected_info = {"confidence": 100.0, "language": source_lang_code}
            src_lang = next(
                (l for l in self.languages if l.code == source_lang_code),
                None,
            )

        if src_lang is None:
            raise TranslationError(
                "%s is not supported" % source_lang_code,
                reason="unsupported",
            )

        return src_lang, detected_info, True

    def resolve_target_language(self, target_lang_code):
        """Look up the target language object.

        Raises :class:`TranslationError` if the code is not available.
        """
        tgt_lang = next(
            (l for l in self.languages if l.code == target_lang_code), None
        )
        if tgt_lang is None:
            raise TranslationError(
                "%s is not supported" % target_lang_code,
                reason="unsupported",
            )
        return tgt_lang

    # -- translation execution ----------------------------------------------

    def translate_single(self, text, src_lang, tgt_lang, text_format,
                         num_alternatives, is_translatable):
        """Translate a single text string.

        Returns ``{"translated_text": str, "alternatives": list}``.
        """
        if not is_translatable:
            return {"translated_text": text, "alternatives": []}

        translator = src_lang.get_translation(tgt_lang)
        if translator is None:
            raise TranslationError(
                "%s (%s) is not available as a target language from %s (%s)"
                % (tgt_lang.name, tgt_lang.code, src_lang.name, src_lang.code),
                reason="unavailable_pair",
            )

        if text_format == "html":
            translated_text = unescape(str(translate_html(translator, text)))
            alternatives = []
        else:
            hypotheses = translator.hypotheses(text, num_alternatives + 1)
            translated_text = unescape(
                improve_translation_formatting(text, hypotheses[0].value)
            )
            alternatives = filter_unique(
                [
                    unescape(
                        improve_translation_formatting(
                            text, hypotheses[i].value
                        )
                    )
                    for i in range(1, len(hypotheses))
                ],
                translated_text,
            )

        return {"translated_text": translated_text, "alternatives": alternatives}

    # -- response building --------------------------------------------------

    def build_response(self, texts, source_lang_code, target_lang_code,
                       text_format=None, num_alternatives=0, is_batch=False):
        """End-to-end translation that produces the API response dict.

        *texts* is always a list.  *is_batch* must be ``True`` when the
        caller received an array input so the response wraps results in
        lists even for a single-element array.

        *source_lang_code* / *target_lang_code* are in **model** format
        (already converted by ``iso2model``).

        Returns the response dict.
        """
        if not text_format:
            text_format = "text"
        if text_format not in ("text", "html"):
            raise TranslationError(
                "%s format is not supported" % text_format,
                reason="bad_format",
            )

        src_lang, detected_info, is_translatable = self.resolve_source_language(
            source_lang_code, texts
        )
        tgt_lang = self.resolve_target_language(target_lang_code)

        translated_texts = []
        all_alternatives = []
        for text in texts:
            entry = self.translate_single(
                text, src_lang, tgt_lang, text_format,
                num_alternatives, is_translatable,
            )
            translated_texts.append(entry["translated_text"])
            all_alternatives.append(entry["alternatives"])

        result = {}

        if is_batch:
            result["translatedText"] = translated_texts
            if source_lang_code == "auto":
                result["detectedLanguage"] = [
                    model2iso(detected_info)
                ] * len(texts)
            if num_alternatives > 0:
                result["alternatives"] = all_alternatives
        else:
            result["translatedText"] = translated_texts[0]
            if source_lang_code == "auto":
                result["detectedLanguage"] = model2iso(detected_info)
            if num_alternatives > 0:
                result["alternatives"] = all_alternatives[0]

        return result

    # -- cache helpers ------------------------------------------------------

    def check_cache(self, api_key, src_texts, source_lang, target_lang,
                    text_format, num_alternatives):
        """Return ``(cache_key, cached_json_or_None)``."""
        cache_key = None
        if self.trans_cache.should_check(api_key):
            cache_key, hit = self.trans_cache.hit(
                src_texts, source_lang, target_lang,
                text_format, num_alternatives,
            )
            return cache_key, hit
        return None, None

    def store_cache(self, cache_key, result):
        """Persist *result* in the translation cache."""
        if cache_key is not None:
            self.trans_cache.cache(cache_key, result)

    # -- detect endpoint ----------------------------------------------------

    def detect_language(self, texts):
        """Run language detection and return ISO-formatted results.

        Used by the ``/detect`` endpoint.
        """
        return model2iso(detect_languages(texts))
