"""Regression tests for the shared translation flow (``libretranslate/translate.py``).

These intentionally exercise ``translate.py`` in isolation: the module is loaded
directly by file path so the test does not import the ``libretranslate`` package
(whose ``__init__`` pulls in argostranslate/Flask and real models). Every heavy
dependency is replaced with a small fake, so this suite runs anywhere pytest does.
It lives at the repo top level (not under ``libretranslate/tests/``) precisely so
collecting it does not import that package.

Run with:  pytest -o addopts="" tests/test_translate_flow.py
"""

import importlib.util
import os

import pytest

# Load translate.py standalone (bypassing libretranslate/__init__.py).
_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "libretranslate", "translate.py")
_spec = importlib.util.spec_from_file_location("lt_translate_flow", _MODULE_PATH)
tf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tf)


# gettext stand-in: returns the message with %()-style interpolation applied.
def _(msg, **kwargs):
    return (msg % kwargs) if kwargs else msg


class FakeLang:
    def __init__(self, code):
        self.code = code


class _Hyp:
    def __init__(self, value):
        self.value = value


class FakeTranslator:
    def __init__(self, values):
        self.values = values

    def hypotheses(self, text, n):
        return [_Hyp(self.values[i]) for i in range(min(n, len(self.values)))]


def fake_translate_html(translator, text):
    # Returns something that still needs unescaping, like the real html path.
    return "&lt;%s&gt;" % text


def fake_improve(source, translation):
    # Identity: keeps the assertions about unescape/filtering unambiguous.
    return translation


# ---------------------------------------------------------------------------
# Parsing / normalization
# ---------------------------------------------------------------------------

def test_normalize_text_format_defaults_and_validates():
    assert tf.normalize_text_format(None, _) == "text"
    assert tf.normalize_text_format("", _) == "text"
    assert tf.normalize_text_format("text", _) == "text"
    assert tf.normalize_text_format("html", _) == "html"

    with pytest.raises(tf.RequestValidationError) as e:
        tf.normalize_text_format("xml", _)
    assert "xml format is not supported" in str(e.value)


def test_parse_num_alternatives_json_and_form_parity():
    # Form sends strings, JSON sends ints/None - both must behave the same.
    assert tf.parse_num_alternatives(None, -1, _) == 0
    assert tf.parse_num_alternatives("3", -1, _) == 3
    assert tf.parse_num_alternatives(3, -1, _) == 3
    assert tf.parse_num_alternatives("-5", -1, _) == 0  # clamped to >= 0


def test_parse_num_alternatives_rejects_non_numeric():
    # The pre-refactor JSON path raised a raw 500 here; now both paths raise 400.
    with pytest.raises(tf.RequestValidationError) as e:
        tf.parse_num_alternatives("abc", -1, _)
    assert "is not a number" in str(e.value)


def test_parse_num_alternatives_enforces_limit():
    with pytest.raises(tf.RequestValidationError) as e:
        tf.parse_num_alternatives(5, 3, _)
    assert "must be <= 3" in str(e.value)
    # limit of -1 disables the check.
    assert tf.parse_num_alternatives(100, -1, _) == 100


def test_normalize_src_texts_uniform_line_endings():
    # Single string and batch are both normalized to LF, JSON or form alike.
    assert tf.normalize_src_texts("a\r\nb\rc") == (False, ["a\nb\nc"])
    assert tf.normalize_src_texts(["a\r\nb", "c\rd"]) == (True, ["a\nb", "c\nd"])
    # Non-string scalars are passed through untouched (no new crash).
    assert tf.normalize_src_texts(123) == (False, [123])


def test_detect_translatable():
    assert tf.detect_translatable(["hello"]) is True
    assert tf.detect_translatable(["😀"]) is False
    assert tf.detect_translatable(["😀", "hello"]) is True
    assert tf.detect_translatable("😀 ") is False
    assert tf.detect_translatable("") is False


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

def test_enforce_limits_batch_size():
    with pytest.raises(tf.RequestValidationError) as e:
        tf.enforce_limits(["a", "b", "c"], batch=True, char_limit=-1, batch_limit=2, _=_)
    assert "exceeds text limit (2)" in str(e.value)
    # Within limit, or disabled, does not raise.
    tf.enforce_limits(["a", "b"], batch=True, char_limit=-1, batch_limit=2, _=_)
    tf.enforce_limits(["a", "b", "c"], batch=True, char_limit=-1, batch_limit=-1, _=_)


def test_enforce_limits_char_limit():
    with pytest.raises(tf.RequestValidationError) as e:
        tf.enforce_limits(["short", "way too long"], batch=False, char_limit=5, batch_limit=-1, _=_)
    assert "exceeds text limit (5)" in str(e.value)
    # Disabled char limit allows anything.
    tf.enforce_limits(["way too long"], batch=False, char_limit=-1, batch_limit=-1, _=_)


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def test_cache_fingerprint_deterministic():
    a = tf.cache_fingerprint(["x"], "en", "es", "text", 0)
    b = tf.cache_fingerprint(["x"], "en", "es", "text", 0)
    assert a == b
    assert a.startswith("tcache_")


def test_cache_fingerprint_omitted_format_matches_text():
    # The bug: an omitted format produced a different key than format=text.
    # With normalization happening before the key is built, they must match.
    fmt_omitted = tf.normalize_text_format(None, _)
    fmt_text = tf.normalize_text_format("text", _)
    key_omitted = tf.cache_fingerprint(["hi"], "en", "es", fmt_omitted, 0)
    key_text = tf.cache_fingerprint(["hi"], "en", "es", fmt_text, 0)
    assert key_omitted == key_text


def test_cache_fingerprint_distinguishes_inputs():
    base = tf.cache_fingerprint(["hi"], "en", "es", "text", 0)
    assert base != tf.cache_fingerprint(["hi"], "en", "es", "html", 0)
    assert base != tf.cache_fingerprint(["hi"], "en", "fr", "text", 0)
    assert base != tf.cache_fingerprint(["hi"], "en", "es", "text", 2)
    assert base != tf.cache_fingerprint(["bye"], "en", "es", "text", 0)


# ---------------------------------------------------------------------------
# Source-language resolution (shared auto-detect)
# ---------------------------------------------------------------------------

def _languages():
    return [FakeLang("en"), FakeLang("es"), FakeLang("pt")]


def test_resolve_source_language_explicit():
    langs = _languages()
    fallback = lambda code, languages: next((l for l in languages if l.code == code), None)
    detect = lambda texts: pytest.fail("detect must not run for an explicit source")

    src_lang, detected = tf.resolve_source_language(
        ["hola"], "es", True, langs, detect, fallback, _
    )
    assert src_lang.code == "es"
    assert detected == {"confidence": 100.0, "language": "es"}


def test_resolve_source_language_auto_uses_detection():
    langs = _languages()
    fallback = lambda code, languages: next((l for l in languages if l.code == code), None)
    detect = lambda texts: [{"confidence": 99.0, "language": "es"}]

    src_lang, detected = tf.resolve_source_language(
        ["hola"], "auto", True, langs, detect, fallback, _
    )
    assert src_lang.code == "es"
    assert detected == {"confidence": 99.0, "language": "es"}


def test_resolve_source_language_untranslatable_is_english_zero_confidence():
    langs = _languages()
    fallback = lambda code, languages: next((l for l in languages if l.code == code), None)
    detect = lambda texts: pytest.fail("detect must not run when nothing is translatable")

    src_lang, detected = tf.resolve_source_language(
        ["😀"], "auto", False, langs, detect, fallback, _
    )
    assert src_lang.code == "en"
    assert detected == {"confidence": 0.0, "language": "en"}


def test_resolve_source_language_unsupported_explicit():
    langs = _languages()
    fallback = lambda code, languages: next((l for l in languages if l.code == code), None)
    with pytest.raises(tf.RequestValidationError) as e:
        tf.resolve_source_language(["x"], "zz", True, langs, lambda t: [], fallback, _)
    assert "zz is not supported" in str(e.value)


def test_resolve_source_language_unsupported_detected_reports_detected_lang():
    langs = _languages()
    fallback = lambda code, languages: None  # nothing matches the detected code
    detect = lambda texts: [{"confidence": 80.0, "language": "zz"}]
    with pytest.raises(tf.RequestValidationError) as e:
        tf.resolve_source_language(["x"], "auto", True, langs, detect, fallback, _)
    assert "zz is not supported" in str(e.value)


# ---------------------------------------------------------------------------
# Translation core
# ---------------------------------------------------------------------------

def test_run_translation_untranslatable_returns_original():
    translated, alternatives = tf.run_translation(
        "😀", FakeTranslator(["unused"]), "text", 0, translatable=False,
        translate_html=fake_translate_html, improve_translation_formatting=fake_improve,
    )
    assert translated == "😀"
    assert alternatives == []


def test_run_translation_html_unescapes_and_has_no_alternatives():
    translated, alternatives = tf.run_translation(
        "hi", FakeTranslator([]), "html", 3, translatable=True,
        translate_html=fake_translate_html, improve_translation_formatting=fake_improve,
    )
    assert translated == "<hi>"  # &lt;hi&gt; unescaped
    assert alternatives == []


def test_run_translation_text_primary_only():
    translated, alternatives = tf.run_translation(
        "hi", FakeTranslator(["hola"]), "text", 0, translatable=True,
        translate_html=fake_translate_html, improve_translation_formatting=fake_improve,
    )
    assert translated == "hola"
    assert alternatives == []


def test_run_translation_text_with_alternatives_unescaped_and_deduped():
    # Primary plus two alternatives; one alternative duplicates the primary and
    # must be filtered out. Entities must be unescaped.
    translator = FakeTranslator(["a &amp; b", "a &amp; b", "c &amp; d"])
    translated, alternatives = tf.run_translation(
        "x", translator, "text", 2, translatable=True,
        translate_html=fake_translate_html, improve_translation_formatting=fake_improve,
    )
    assert translated == "a & b"
    assert alternatives == ["c & d"]  # duplicate of primary removed


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------

def test_build_translate_result_single_minimal():
    result = tf.build_translate_result(
        ["hola"], [[]], batch=False, source_lang="en",
        detected_src_lang={"confidence": 100.0, "language": "en"},
        num_alternatives=0, model2iso=lambda d: d,
    )
    assert result == {"translatedText": "hola"}


def test_build_translate_result_single_auto_and_alternatives():
    result = tf.build_translate_result(
        ["hola"], [["ola"]], batch=False, source_lang="auto",
        detected_src_lang={"confidence": 99.0, "language": "es"},
        num_alternatives=1, model2iso=lambda d: "ISO",
    )
    assert result["translatedText"] == "hola"
    assert result["detectedLanguage"] == "ISO"  # model2iso applied
    assert result["alternatives"] == ["ola"]


def test_build_translate_result_batch_shapes():
    result = tf.build_translate_result(
        ["hola", "mundo"], [["ola"], ["m"]], batch=True, source_lang="auto",
        detected_src_lang={"confidence": 99.0, "language": "es"},
        num_alternatives=1, model2iso=lambda d: "ISO",
    )
    assert result["translatedText"] == ["hola", "mundo"]
    # detectedLanguage is one entry per text.
    assert result["detectedLanguage"] == ["ISO", "ISO"]
    assert result["alternatives"] == [["ola"], ["m"]]


def test_build_translate_result_omits_optional_keys_when_not_requested():
    result = tf.build_translate_result(
        ["hola", "mundo"], [[], []], batch=True, source_lang="en",
        detected_src_lang={"confidence": 100.0, "language": "en"},
        num_alternatives=0, model2iso=lambda d: d,
    )
    assert "detectedLanguage" not in result  # source was not "auto"
    assert "alternatives" not in result      # none requested
