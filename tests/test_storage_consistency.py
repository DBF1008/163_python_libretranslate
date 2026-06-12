"""Regression tests for shared-storage read/write semantics.

After the service moved to Redis shared storage, multi-process nodes saw
intermittent failures in ban decay, fingerprint checks, secret rotation and the
translation cache: bans that never recovered and stale values that kept being
treated as truth. The root cause was that ``MemoryStorage`` and ``RedisStorage``
had drifted apart on the primitives those features depend on. These tests pin
the two backends to *identical* behaviour by running the exact same assertions
against both (see the ``backend`` fixture parametrization), and cover the three
chains called out by the bug report: rate-limit recovery, secret verification
and cache hits (plus the fingerprint check for good measure).

This module lives outside the ``libretranslate`` package on purpose: importing
the package runs ``libretranslate/__init__.py``, which pulls in the full
translation stack (argostranslate, flask, ...). The storage layer only needs
``redis``, so when those heavy deps are missing we load the relevant modules
directly from source. With the full stack installed the normal import is used.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parents[1] / "libretranslate"


def _load_isolated():
    """Load storage and its dependents without triggering the package __init__."""
    pkg_name = "libretranslate"
    if pkg_name not in sys.modules:
        shell = types.ModuleType(pkg_name)
        shell.__path__ = [str(_PKG_DIR)]
        sys.modules[pkg_name] = shell

    loaded = {}
    # storage must come first: flood/secret/cache import from it at module load.
    for name in ("storage", "flood", "secret", "cache"):
        full = f"{pkg_name}.{name}"
        if full in sys.modules:
            loaded[name] = sys.modules[full]
            continue
        spec = importlib.util.spec_from_file_location(full, _PKG_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded


try:  # Full stack available: use the real package.
    from libretranslate import cache, flood, secret
    from libretranslate import storage as storage_mod
except Exception:  # Heavy deps missing: load the storage modules in isolation.
    _mods = _load_isolated()
    storage_mod = _mods["storage"]
    flood = _mods["flood"]
    secret = _mods["secret"]
    cache = _mods["cache"]


def _make_memory():
    return storage_mod.MemoryStorage()


def _make_redis():
    fakeredis = pytest.importorskip("fakeredis")
    # Bypass __init__ (which would dial a real Redis) and point at a fresh,
    # isolated fake server so each test starts from an empty database.
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    rs = storage_mod.RedisStorage.__new__(storage_mod.RedisStorage)
    rs.conn = conn
    return rs


@pytest.fixture(params=["memory", "redis"])
def backend(request, monkeypatch):
    """Yield a fresh storage backend and install it as the process-wide storage.

    ``flood``/``secret``/``cache`` all reach storage via
    ``libretranslate.storage.get_storage()``, which returns the module global, so
    patching it here redirects them at the parametrized backend.
    """
    storage = _make_memory() if request.param == "memory" else _make_redis()
    monkeypatch.setattr(storage_mod, "storage", storage)
    return storage


# --------------------------------------------------------------------------- #
# Storage primitive parity
# --------------------------------------------------------------------------- #

def test_hash_counter_parity(backend):
    s = backend

    # Counters are 1-based and return the new value (matches Redis HINCRBY).
    assert s.get_hash_int("banned", "ip") == 0
    assert s.inc_hash_int("banned", "ip") == 1
    assert s.inc_hash_int("banned", "ip") == 2
    assert s.get_hash_int("banned", "ip") == 2
    assert s.dec_hash_int("banned", "ip") == 1

    # get_all_hash_int returns a flat {field: int} mapping on both backends.
    assert s.get_all_hash_int("banned") == {"ip": 1}

    # Deleting clears the field; deleting a missing field is a no-op (no raise).
    s.del_hash("banned", "ip")
    assert s.get_all_hash_int("banned") == {}
    s.del_hash("banned", "does-not-exist")

    # A missing field decrements to -1 (matches HINCRBY -1).
    assert s.dec_hash_int("counter", "x") == -1
    assert s.get_all_hash_int("missing-namespace") == {}


def test_scalar_parity(backend):
    s = backend

    # Ints round-trip as ints, default 0 when missing.
    assert s.get_int("n") == 0
    s.set_int("n", 7)
    assert s.get_int("n") == 7
    assert isinstance(s.get_int("n"), int)

    # Bools round-trip, default False when missing. Regression: on Redis a stored
    # False used to read back True because bytes b"0" is truthy.
    assert s.get_bool("flag") is False
    s.set_bool("flag", True)
    assert s.get_bool("flag") is True
    s.set_bool("flag", False)
    assert s.get_bool("flag") is False

    # Strings: empty when missing; raw bytes survive a round-trip (cache path).
    assert s.get_str("missing") == ""
    s.set_str("greeting", "hello", ex=100)
    assert s.get_str("greeting") == "hello"
    blob = b"\x1f\x8b\x08 raw-bytes \x00\x01"
    s.set_str("blob", blob, ex=100)
    assert s.get_str("blob", raw=True) == blob


# --------------------------------------------------------------------------- #
# Rate-limit recovery (限流恢复)
# --------------------------------------------------------------------------- #

def test_rate_limit_recovery(backend):
    flood.setup(types.SimpleNamespace(req_flood_threshold=3))
    ip = "203.0.113.7"

    assert flood.is_banned(ip) is False

    for _ in range(3):
        flood.report(ip)

    assert flood.has_violation(ip) is True
    assert flood.is_banned(ip) is True  # 3 offences >= threshold(3)

    # The scheduled job must decay the counter all the way back out.
    for _ in range(20):
        flood.forgive_banned()
        if ip not in backend.get_all_hash_int("banned"):
            break

    assert ip not in backend.get_all_hash_int("banned")
    assert flood.is_banned(ip) is False
    assert flood.has_violation(ip) is False
    assert backend.get_hash_int("banned", ip) == 0


def test_decrease_clears_violation(backend):
    flood.setup(types.SimpleNamespace(req_flood_threshold=3))
    ip = "203.0.113.9"

    flood.report(ip)
    assert flood.has_violation(ip) is True

    flood.decrease(ip)
    assert flood.has_violation(ip) is False

    # decrease never drives a non-violating IP below zero.
    flood.decrease(ip)
    assert backend.get_hash_int("banned", ip) == 0


# --------------------------------------------------------------------------- #
# Secret verification / rotation (密钥校验)
# --------------------------------------------------------------------------- #

def test_secret_rotation(backend, monkeypatch):
    # Deterministic secrets so the grace window is unambiguous across backends.
    seq = iter(["AAA0001", "AAA0002", "AAA0003", "AAA0004", "AAA0005"])
    monkeypatch.setattr(secret, "generate_secret", lambda: next(seq))

    secret.setup(types.SimpleNamespace(require_api_key_secret=True))
    current = secret.get_current_secret()        # secret_1 -> AAA0002
    previous = backend.get_str("secret_0")       # secret_0 -> AAA0001

    assert current == "AAA0002"
    assert previous == "AAA0001"
    assert secret.secret_match(current) is True
    assert secret.secret_match(previous) is True
    assert secret.secret_match("definitely-not-a-secret") is False

    secret.rotate_secrets()  # secret_0 <- AAA0002, secret_1 <- AAA0004
    # Grace window: the previously-current secret is still accepted...
    assert secret.secret_match(current) is True
    # ...the freshly rotated secret is accepted...
    new_current = secret.get_current_secret()
    assert new_current == "AAA0004"
    assert secret.secret_match(new_current) is True
    # ...and the secret that predated this window has aged out.
    assert secret.secret_match(previous) is False

    secret.rotate_secrets()  # secret_0 <- AAA0004, secret_1 <- AAA0005
    # After a second rotation the original 'current' has aged out too.
    assert secret.secret_match(current) is False
    assert secret.secret_match(secret.get_current_secret()) is True


# --------------------------------------------------------------------------- #
# Translation cache hit (缓存命中)
# --------------------------------------------------------------------------- #

def test_cache_hit_roundtrip(backend):
    tc = cache.setup(["all"])  # cache enabled for every API key

    cache_key, miss = tc.hit("hello world", "en", "es", "text", 0)
    assert miss is None  # cold cache -> miss

    result = {
        "translatedText": "hola mundo",
        "detectedLanguage": {"confidence": 100, "language": "en"},
    }
    tc.cache(cache_key, result)

    same_key, hit = tc.hit("hello world", "en", "es", "text", 0)
    assert same_key == cache_key
    assert hit is not None  # warm cache -> hit
    assert json.loads(hit) == result  # survives gzip + storage round-trip

    # Distinct request shapes must not collide on the same cache entry.
    _, other = tc.hit("hello world", "en", "es", "text", 1)
    assert other is None
    _, other2 = tc.hit("different text", "en", "es", "text", 0)
    assert other2 is None


# --------------------------------------------------------------------------- #
# Fingerprint check (指纹校验)
# --------------------------------------------------------------------------- #

def test_fingerprint_check(backend):
    flood.setup(types.SimpleNamespace(req_flood_threshold=3))
    ip = "198.51.100.5"

    assert flood.fingerprint_mismatch(ip, "") is True      # empty -> mismatch
    assert flood.fingerprint_mismatch(ip, None) is True    # non-str -> mismatch
    assert flood.fingerprint_mismatch(ip, "fp-A") is False  # first sighting registers
    assert flood.fingerprint_mismatch(ip, "fp-A") is False  # same fingerprint, ok
    assert flood.fingerprint_mismatch(ip, "fp-B") is True   # changed -> mismatch

    # A different IP is tracked independently.
    assert flood.fingerprint_mismatch("198.51.100.6", "fp-A") is False
