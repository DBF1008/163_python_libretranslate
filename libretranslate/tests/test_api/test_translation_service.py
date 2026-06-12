"""Unit tests for the unified translation service module.

These tests cover the pure helpers (``detect_translatable``,
``filter_unique``) and the ``TranslationService`` methods using
mock language objects, so they run without real Argos models.
"""

import pytest

from libretranslate.translation import (
    TranslationError,
    TranslationService,
    detect_translatable,
    filter_unique,
)


# ---------------------------------------------------------------------------
# detect_translatable
# ---------------------------------------------------------------------------

class TestDetectTranslatable:
    def test_emoji_only_returns_false(self):
        assert detect_translatable("\U0001f600\U0001f601") is False

    def test_spaces_and_emoji_returns_false(self):
        assert detect_translatable(" \U0001f600 ") is False

    def test_plain_text_returns_true(self):
        assert detect_translatable("Hello") is True

    def test_mixed_text_returns_true(self):
        assert detect_translatable("Hello \U0001f600") is True

    def test_empty_string_returns_false(self):
        assert detect_translatable("") is False

    def test_batch_any_translatable(self):
        assert detect_translatable(["\U0001f600", "Hello"]) is True

    def test_batch_all_emoji(self):
        assert detect_translatable(["\U0001f600", "\U0001f601"]) is False

    def test_batch_empty_list(self):
        assert detect_translatable([]) is False

    def test_single_character(self):
        assert detect_translatable("a") is True

    def test_single_emoji(self):
        assert detect_translatable("\U0001f600") is False


# ---------------------------------------------------------------------------
# filter_unique
# ---------------------------------------------------------------------------

class TestFilterUnique:
    def test_removes_duplicates(self):
        assert filter_unique(["a", "b", "a"], "c") == ["a", "b"]

    def test_removes_extra(self):
        assert filter_unique(["a", "b", "c"], "b") == ["a", "c"]

    def test_removes_empty_strings(self):
        assert filter_unique(["a", "", "b", ""], "c") == ["a", "b"]

    def test_empty_input(self):
        assert filter_unique([], "x") == []

    def test_preserves_order(self):
        assert filter_unique(["c", "b", "a"], "d") == ["c", "b", "a"]

    def test_all_same_as_extra(self):
        assert filter_unique(["x", "x", "x"], "x") == []


# ---------------------------------------------------------------------------
# TranslationError
# ---------------------------------------------------------------------------

class TestTranslationError:
    def test_message(self):
        e = TranslationError("test message")
        assert str(e) == "test message"

    def test_default_reason(self):
        e = TranslationError("msg")
        assert e.reason == "unsupported"

    def test_custom_reason(self):
        e = TranslationError("msg", reason="bad_format")
        assert e.reason == "bad_format"

    def test_is_exception(self):
        assert issubclass(TranslationError, Exception)


# ---------------------------------------------------------------------------
# TranslationService (with mocks)
# ---------------------------------------------------------------------------

class _MockLanguage:
    """Minimal stand-in for an argostranslate Language object."""

    def __init__(self, code, name=None, translation_to=None):
        self.code = code
        self.name = name or code
        self._translation_to = translation_to

    def get_translation(self, target):
        return self._translation_to


class _MockTranslator:
    """Minimal stand-in for an argostranslate Translation object."""

    def __init__(self, output="translated", hypotheses=None):
        self._output = output
        self._hypotheses = hypotheses

    def hypotheses(self, text, n):
        if self._hypotheses:
            return self._hypotheses[:n]
        return [_MockHypothesis(self._output)] * n


class _MockHypothesis:
    def __init__(self, value):
        self.value = value


class TestTranslationServiceResolveSource:
    def _make_service(self, languages=None):
        if languages is None:
            languages = [
                _MockLanguage("en", "English"),
                _MockLanguage("es", "Spanish"),
            ]
        return TranslationService(languages, _NoOpCache())

    def test_explicit_language(self):
        svc = self._make_service()
        src, info, translatable = svc.resolve_source_language("en", ["Hello"])

        assert src.code == "en"
        assert info["confidence"] == 100.0
        assert info["language"] == "en"
        assert translatable is True

    def test_unsupported_language_raises(self):
        svc = self._make_service()
        with pytest.raises(TranslationError, match="not supported"):
            svc.resolve_source_language("zz", ["Hello"])

    def test_emoji_only_text(self):
        svc = self._make_service()
        src, info, translatable = svc.resolve_source_language(
            "en", ["\U0001f600\U0001f601"]
        )

        assert src.code == "en"
        assert info["confidence"] == 0.0
        assert info["language"] == "en"
        assert translatable is False


class TestTranslationServiceResolveTarget:
    def test_supported_target(self):
        svc = TranslationService(
            [_MockLanguage("en"), _MockLanguage("es")], _NoOpCache()
        )
        tgt = svc.resolve_target_language("es")
        assert tgt.code == "es"

    def test_unsupported_target_raises(self):
        svc = TranslationService(
            [_MockLanguage("en"), _MockLanguage("es")], _NoOpCache()
        )
        with pytest.raises(TranslationError, match="not supported"):
            svc.resolve_target_language("zz")


class TestTranslationServiceTranslateSingle:
    def _make_service(self):
        translator = _MockTranslator(output="hola")
        en = _MockLanguage("en", "English", translation_to=translator)
        es = _MockLanguage("es", "Spanish")
        return TranslationService([en, es], _NoOpCache())

    def test_basic_translation(self):
        svc = self._make_service()
        en = svc.resolve_target_language("en")
        es = svc.resolve_target_language("es")

        result = svc.translate_single(
            "hello", en, es, "text", 0, is_translatable=True
        )
        assert result["translated_text"] == "hola"

    def test_emoji_passthrough(self):
        svc = self._make_service()
        en = svc.resolve_target_language("en")
        es = svc.resolve_target_language("es")

        result = svc.translate_single(
            "\U0001f600", en, es, "text", 0, is_translatable=False
        )
        assert result["translated_text"] == "\U0001f600"
        assert result["alternatives"] == []

    def test_unavailable_pair_raises(self):
        en = _MockLanguage("en", "English", translation_to=None)
        es = _MockLanguage("es", "Spanish")
        svc = TranslationService([en, es], _NoOpCache())

        with pytest.raises(TranslationError, match="not available"):
            svc.translate_single(
                "hello", en, es, "text", 0, is_translatable=True
            )


class TestTranslationServiceBuildResponse:
    def _make_service(self):
        translator = _MockTranslator(output="hola")
        en = _MockLanguage("en", "English", translation_to=translator)
        es = _MockLanguage("es", "Spanish")
        return TranslationService([en, es], _NoOpCache())

    def test_single_text_response(self):
        svc = self._make_service()
        result = svc.build_response(
            ["hello"], "en", "es", text_format="text",
            num_alternatives=0, is_batch=False,
        )
        assert isinstance(result["translatedText"], str)
        assert "detectedLanguage" not in result

    def test_batch_text_response(self):
        svc = self._make_service()
        result = svc.build_response(
            ["hello", "world"], "en", "es",
            text_format="text", num_alternatives=0, is_batch=True,
        )
        assert isinstance(result["translatedText"], list)
        assert len(result["translatedText"]) == 2

    def test_bad_format_raises(self):
        svc = self._make_service()
        with pytest.raises(TranslationError, match="format"):
            svc.build_response(
                ["hello"], "en", "es", text_format="xml", is_batch=False,
            )

    def test_unsupported_source_raises(self):
        svc = self._make_service()
        with pytest.raises(TranslationError, match="not supported"):
            svc.build_response(
                ["hello"], "zz", "es", text_format="text", is_batch=False,
            )

    def test_unsupported_target_raises(self):
        svc = self._make_service()
        with pytest.raises(TranslationError, match="not supported"):
            svc.build_response(
                ["hello"], "en", "zz", text_format="text", is_batch=False,
            )


class TestTranslationServiceCharLimit:
    def test_within_limit(self):
        svc = TranslationService([], _NoOpCache())
        # Should not raise
        svc.enforce_char_limit(["hello"], 10)

    def test_exceeds_limit_raises(self):
        svc = TranslationService([], _NoOpCache())
        with pytest.raises(TranslationError, match="exceeds"):
            svc.enforce_char_limit(["hello world"], 5)

    def test_unlimited(self):
        svc = TranslationService([], _NoOpCache())
        # -1 means unlimited
        svc.enforce_char_limit(["a" * 10000], -1)

    def test_empty_text(self):
        svc = TranslationService([], _NoOpCache())
        svc.enforce_char_limit([""], 5)


# ---------------------------------------------------------------------------
# No-op cache for testing
# ---------------------------------------------------------------------------

class _NoOpCache:
    """Minimal stand-in for TranslationCache that never caches."""

    def should_check(self, ak):
        return False

    def hit(self, *a, **kw):
        return None, None

    def cache(self, *a, **kw):
        pass
