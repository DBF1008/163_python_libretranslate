"""Regression tests for the cache gzip decompression fix.

The original ``TranslationCache.hit()`` caught gzip decompression
exceptions but still returned the corrupt ``cached`` value (raw
compressed bytes).  The fix sets ``cached = None`` in the except
block so a corrupt cache entry is treated as a miss.
"""

import gzip
import json

from libretranslate.storage import MemoryStorage
from libretranslate.cache import TranslationCache


def _make_cache():
    """Create a TranslationCache backed by MemoryStorage."""
    # Patch storage so the cache can use it
    import libretranslate.storage as storage_mod
    original = storage_mod.storage
    storage_mod.storage = MemoryStorage()
    try:
        tc = TranslationCache(["all"])
        return tc
    finally:
        # Restore original storage (may be None in test context)
        storage_mod.storage = original


def test_cache_roundtrip():
    """A normal cache write-then-read returns the stored JSON."""
    tc = _make_cache()

    result = {"translatedText": "Hola"}
    cache_key, _ = tc.hit(["Hello"], "en", "es", "text", 0)
    assert cache_key is not None

    tc.cache(cache_key, result)

    _, hit = tc.hit(["Hello"], "en", "es", "text", 0)
    assert hit is not None
    assert json.loads(hit) == result


def test_cache_hit_corrupt_returns_miss():
    """When the cached value is not valid gzip, hit() must return None
    instead of the corrupt bytes."""
    tc = _make_cache()

    # Manually store a non-gzip value under the expected cache key
    cache_key, _ = tc.hit(["Hello"], "en", "es", "text", 0)
    tc.storage.set_str(cache_key, b"this is not gzip data", tc.expire)

    _, hit = tc.hit(["Hello"], "en", "es", "text", 0)
    assert hit is None, "Corrupt cache entry should be treated as a miss"


def test_cache_miss_returns_none():
    """A fresh key should return (key, None)."""
    tc = _make_cache()

    cache_key, hit = tc.hit(["Nonexistent"], "en", "es", "text", 0)
    assert cache_key is not None
    assert hit is None


def test_cache_should_check_disabled():
    """When caching is not enabled for the API key, should_check is False."""
    import libretranslate.storage as storage_mod
    original = storage_mod.storage
    storage_mod.storage = MemoryStorage()
    try:
        tc = TranslationCache([])  # empty = disabled
        assert not tc.should_check("any-key")
    finally:
        storage_mod.storage = original


def test_cache_should_check_all():
    """When 'all' is in the list, should_check is True for any key."""
    import libretranslate.storage as storage_mod
    original = storage_mod.storage
    storage_mod.storage = MemoryStorage()
    try:
        tc = TranslationCache(["all"])
        assert tc.should_check("any-key")
        assert tc.should_check(None)
    finally:
        storage_mod.storage = original
