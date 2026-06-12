"""Regression tests for the MemoryStorage.get_all_hash_int fix.

The original implementation returned a list of single-key dicts
``[{k: v}, ...]``, while ``RedisStorage`` returns a flat dict
``{k: v}``.  ``flood.forgive_banned()`` iterates with
``for ip in banned: banned[ip]``, which only works with a flat dict.
"""

from libretranslate.storage import MemoryStorage


def test_memory_storage_get_all_hash_int_returns_dict():
    s = MemoryStorage()
    s.set_hash_int("banned", "1.2.3.4", 3)
    s.set_hash_int("banned", "5.6.7.8", 1)

    result = s.get_all_hash_int("banned")

    assert isinstance(result, dict)
    assert result == {"1.2.3.4": 3, "5.6.7.8": 1}


def test_memory_storage_get_all_hash_int_empty_namespace():
    s = MemoryStorage()
    result = s.get_all_hash_int("nonexistent")

    assert isinstance(result, dict)
    assert result == {}


def test_memory_storage_get_all_hash_int_flood_compatible():
    """Verify that the result can be iterated the way
    ``flood.forgive_banned()`` does — ``for ip in banned: banned[ip]``.
    """
    s = MemoryStorage()
    s.set_hash_int("banned", "10.0.0.1", 5)
    s.set_hash_int("banned", "10.0.0.2", 0)

    banned = s.get_all_hash_int("banned")

    # This is exactly the iteration pattern used by flood.forgive_banned()
    clear_list = []
    for ip in banned:
        if banned[ip] <= 0:
            clear_list.append(ip)

    assert clear_list == ["10.0.0.2"]
