"""Shared translation request flow.

This module holds the common pre-/post-translation logic used by the
``/translate`` and ``/translate_file`` endpoints so they stay consistent on
character/batch limits, source auto-detection, result shape and cache keys.

It intentionally imports only the standard library: every heavy dependency
(the argostranslate language objects and translator, ``detect_languages``,
``improve_translation_formatting``, ``translate_html``, ``model2iso`` and the
``gettext`` callable) is passed in by the caller. That keeps the flow decoupled
from Flask/argostranslate and lets it be unit-tested without loading any models.
"""

import hashlib
from html import unescape

# Rough map of emoji characters used to decide whether a text is worth
# translating at all (a string of only emoji/spaces is sent back untouched).
emojis = {e: True for e in \
  [ord(' ')] +                    # Spaces
  list(range(0x1F600,0x1F64F)) +  # Emoticons
  list(range(0x1F300,0x1F5FF)) +  # Misc Symbols and Pictographs
  list(range(0x1F680,0x1F6FF)) +  # Transport and Map
  list(range(0x2600,0x26FF)) +    # Misc symbols
  list(range(0x2700,0x27BF)) +    # Dingbats
  list(range(0xFE00,0xFE0F)) +    # Variation Selectors
  list(range(0x1F900,0x1F9FF)) +  # Supplemental Symbols and Pictographs
  list(range(0x1F1E6,0x1F1FF)) +  # Flags
  list(range(0x20D0,0x20FF))      # Combining Diacritical Marks for Symbols
}


class RequestValidationError(ValueError):
    """Raised for invalid translation requests.

    The caller is expected to translate this into an HTTP 400 response using
    the carried message (already passed through gettext by the helper).
    """


def filter_unique(seq, extra):
    seen = set({extra, ""})
    seen_add = seen.add
    return [x for x in seq if not (x in seen or seen_add(x))]


def detect_translatable(src_texts):
    if isinstance(src_texts, list):
        return any(detect_translatable(t) for t in src_texts)

    for ch in src_texts:
        if not (ord(ch) in emojis):
            return True

    # All emojis
    return False


# ---------------------------------------------------------------------------
# Request parsing / normalization
# ---------------------------------------------------------------------------

def normalize_text_format(text_format, _):
    """Default an empty format to ``text`` and reject anything unsupported."""
    if not text_format:
        return "text"
    if text_format not in ("text", "html"):
        raise RequestValidationError(
            _("%(format)s format is not supported", format=text_format)
        )
    return text_format


def parse_num_alternatives(raw, limit, _):
    """Parse the ``alternatives`` parameter the same way for JSON and form input."""
    if raw is None:
        raw = 0
    try:
        num_alternatives = max(0, int(raw))
    except (ValueError, TypeError):
        raise RequestValidationError(
            _("Invalid request: %(name)s parameter is not a number", name='alternatives')
        )

    if limit != -1 and num_alternatives > limit:
        raise RequestValidationError(
            _("Invalid request: %(name)s parameter must be <= %(value)s", name='alternatives', value=limit)
        )

    return num_alternatives


def _normalize_line_endings(text):
    # Normalize line endings to UNIX style (LF) only so character limits and
    # cache keys are computed consistently regardless of input.
    # https://www.rfc-editor.org/rfc/rfc2046#section-4.1.1
    if not isinstance(text, str):
        return text
    return "\n".join(text.splitlines())


def normalize_src_texts(q):
    """Return ``(batch, src_texts)`` with line endings normalized uniformly.

    A single string becomes a one-element list so callers can treat single and
    batch input identically; ``batch`` records the original shape.
    """
    batch = isinstance(q, list)
    if batch:
        src_texts = [_normalize_line_endings(t) for t in q]
    else:
        src_texts = [_normalize_line_endings(q)]
    return batch, src_texts


def enforce_limits(src_texts, batch, char_limit, batch_limit, _):
    """Enforce batch-size and per-text character limits (``-1`` disables each)."""
    if batch and batch_limit != -1:
        batch_size = len(src_texts)
        if batch_limit < batch_size:
            raise RequestValidationError(
                _("Invalid request: request (%(size)s) exceeds text limit (%(limit)s)", size=batch_size, limit=batch_limit)
            )

    if char_limit != -1:
        for text in src_texts:
            if len(text) > char_limit:
                raise RequestValidationError(
                    _("Invalid request: request (%(size)s) exceeds text limit (%(limit)s)", size=len(text), limit=char_limit)
                )


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def cache_fingerprint(src_texts, source_lang, target_lang, text_format, num_alternatives):
    """Build the cache key. Callers must pass already-normalized values so that
    e.g. an omitted format and ``format=text`` map to the same entry."""
    text_blob = "|".join(src_texts) if isinstance(src_texts, list) else src_texts
    fingerprint = f"{text_blob}:{source_lang}:{target_lang}:{text_format}:{num_alternatives}"
    return "tcache_" + hashlib.md5(fingerprint.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Language resolution (auto-detection shared by both endpoints)
# ---------------------------------------------------------------------------

def resolve_source_language(src_texts, source_lang, translatable, languages,
                            detect_languages, get_language_with_fallback, _):
    """Resolve the source language object and the reported detection result.

    Returns ``(src_lang, detected_src_lang)``. Raises ``RequestValidationError``
    if the (detected or requested) source language is not supported.
    """
    if translatable:
        if source_lang == "auto":
            candidate_langs = detect_languages(src_texts)
            detected_src_lang = candidate_langs[0]
            src_lang = get_language_with_fallback(detected_src_lang["language"], languages)
        else:
            detected_src_lang = {"confidence": 100.0, "language": source_lang}
            src_lang = next((l for l in languages if l.code == source_lang), None)
    else:
        # Nothing translatable (e.g. only emoji): report English with no confidence.
        detected_src_lang = {"confidence": 0.0, "language": "en"}
        src_lang = next((l for l in languages if l.code == "en"), None)

    if src_lang is None:
        unsupported = detected_src_lang["language"] if source_lang == "auto" else source_lang
        raise RequestValidationError(_("%(lang)s is not supported", lang=unsupported))

    return src_lang, detected_src_lang


# ---------------------------------------------------------------------------
# Translation core + result assembly
# ---------------------------------------------------------------------------

def run_translation(text, translator, text_format, num_alternatives, translatable,
                    *, translate_html, improve_translation_formatting):
    """Translate a single text, returning ``(translated_text, alternatives)``.

    Single and batch requests both go through here (batch is just a loop), which
    keeps the text/html and translatable/untranslatable handling in one place.
    """
    if not translatable:
        return text, []  # Cannot translate, send the original text back

    if text_format == "html":
        translated_text = unescape(str(translate_html(translator, text)))
        return translated_text, []  # Alternatives not supported for html yet

    hypotheses = translator.hypotheses(text, num_alternatives + 1)
    translated_text = unescape(improve_translation_formatting(text, hypotheses[0].value))
    alternatives = filter_unique(
        [unescape(improve_translation_formatting(text, hypotheses[i].value)) for i in range(1, len(hypotheses))],
        translated_text,
    )
    return translated_text, alternatives


def build_translate_result(translated_texts, alternatives_lists, batch, source_lang,
                           detected_src_lang, num_alternatives, model2iso):
    """Assemble the response dict for single or batch requests.

    ``detectedLanguage`` is only included when the source was ``auto`` and
    ``alternatives`` only when some were requested, matching the documented API.
    """
    if batch:
        result = {"translatedText": translated_texts}
        if source_lang == "auto":
            result["detectedLanguage"] = [model2iso(detected_src_lang)] * len(translated_texts)
        if num_alternatives > 0:
            result["alternatives"] = alternatives_lists
    else:
        result = {"translatedText": translated_texts[0]}
        if source_lang == "auto":
            result["detectedLanguage"] = model2iso(detected_src_lang)
        if num_alternatives > 0:
            result["alternatives"] = alternatives_lists[0]

    return result
