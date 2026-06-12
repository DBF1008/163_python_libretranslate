import itertools
import types

import pytest

from libretranslate import auth, flood, secret


def make_args(**overrides):
    """Build a minimal args namespace with all knobs auth.py reads.

    Defaults: API keys enabled, no gating. Override individual flags per test.
    """
    base = dict(
        api_keys=True,
        under_attack=False,
        require_api_key_origin="",
        require_api_key_secret=False,
        require_api_key_fingerprint=False,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class FakeDB:
    def __init__(self, keys=None):
        self.keys = keys or {}

    def lookup(self, api_key):
        # Mirrors api_keys.Database.lookup: tuple for known keys, None otherwise.
        return self.keys.get(api_key)


@pytest.fixture
def db():
    return FakeDB({"GOODKEY": (100, None)})


def call(args, db=None, *, api_key=None, req_secret=None,
         origin="", ip="1.2.3.4", fingerprint="fp"):
    return auth.check(
        args, db,
        api_key=api_key, req_secret=req_secret,
        origin=origin, ip=ip, fingerprint=fingerprint,
    )


# --- key_required / under_attack -------------------------------------------

def test_key_not_required_when_api_keys_disabled():
    # Even with every gate "on", disabling the API-keys feature means no gating.
    args = make_args(
        api_keys=False,
        under_attack=True,
        require_api_key_origin="https://x",
        require_api_key_secret=True,
        require_api_key_fingerprint=True,
    )
    assert auth.key_required(args) is False
    assert auth.under_attack(args) is False


def test_no_gating_allows_keyless(db):
    args = make_args()  # all gates off
    assert auth.key_required(args) is False
    assert call(args, db) == auth.ALLOW


def test_under_attack_only_gate_that_locks_the_ui(db):
    # under_attack -> both "key required" and "UI forced to use a key".
    args = make_args(under_attack=True)
    assert auth.key_required(args) is True
    assert auth.under_attack(args) is True

    # Origin / secret / fingerprint gate keyless API access but do NOT force a
    # key on the official UI, so under_attack() stays False for them.
    for gate in ("require_api_key_secret", "require_api_key_fingerprint"):
        args = make_args(**{gate: True})
        assert auth.key_required(args) is True
        assert auth.under_attack(args) is False
    args = make_args(require_api_key_origin="https://example.com")
    assert auth.key_required(args) is True
    assert auth.under_attack(args) is False


# --- check(): keys ----------------------------------------------------------

def test_valid_key_bypasses_all_gates(db):
    args = make_args(
        under_attack=True,
        require_api_key_origin="https://example.com",
        require_api_key_secret=True,
        require_api_key_fingerprint=True,
    )
    assert call(args, db, api_key="GOODKEY") == auth.ALLOW


def test_invalid_key_rejected(db):
    args = make_args()
    assert call(args, db, api_key="NOPE") == auth.INVALID_KEY


# --- check(): individual gates ---------------------------------------------

def test_under_attack_requires_key(db):
    args = make_args(under_attack=True)
    assert call(args, db) == auth.NEED_KEY
    assert call(args, db, api_key="GOODKEY") == auth.ALLOW


def test_origin_match_and_mismatch(db):
    args = make_args(require_api_key_origin="https://example.com")
    # re.match anchors at the start, so a matching prefix is accepted.
    assert call(args, db, origin="https://example.com/page") == auth.ALLOW
    assert call(args, db, origin="https://evil.com") == auth.NEED_KEY
    assert call(args, db, origin="") == auth.NEED_KEY


def test_empty_origin_pattern_is_treated_as_off(db):
    args = make_args(require_api_key_origin="")
    assert auth.key_required(args) is False
    assert call(args, db, origin="anything") == auth.ALLOW


def test_secret_match_mismatch_and_bogus(monkeypatch, db):
    args = make_args(require_api_key_secret=True)

    monkeypatch.setattr(secret, "secret_match", lambda s: s == "GOOD")
    monkeypatch.setattr(secret, "secret_bogus_match", lambda s: False)
    assert call(args, db, req_secret="GOOD") == auth.ALLOW
    assert call(args, db, req_secret="BAD") == auth.NEED_KEY

    # A wrong secret that happens to be the bogus one served to bots -> honeypot.
    monkeypatch.setattr(secret, "secret_bogus_match", lambda s: True)
    assert call(args, db, req_secret="BOGUS") == auth.SERVE_BOGUS


def test_fingerprint_mismatch(monkeypatch, db):
    args = make_args(require_api_key_fingerprint=True)

    monkeypatch.setattr(flood, "fingerprint_mismatch", lambda ip, fp: True)
    assert call(args, db, fingerprint="x") == auth.NEED_KEY

    monkeypatch.setattr(flood, "fingerprint_mismatch", lambda ip, fp: False)
    assert call(args, db, fingerprint="x") == auth.ALLOW


def test_bogus_takes_precedence_over_other_failing_gates(monkeypatch, db):
    # Faithful to the original: the bogus honeypot returns ahead of the generic
    # 400 even when another gate (here Origin) has already failed.
    args = make_args(require_api_key_origin="https://example.com", require_api_key_secret=True)
    monkeypatch.setattr(secret, "secret_match", lambda s: False)
    monkeypatch.setattr(secret, "secret_bogus_match", lambda s: True)
    assert call(args, db, origin="https://evil.com", req_secret="BOGUS") == auth.SERVE_BOGUS


# --- anti-drift invariant ---------------------------------------------------

def test_advertised_key_required_matches_enforcement(monkeypatch):
    """The headline guarantee: key_required(args) is True for exactly the
    configurations where some keyless request is actually rejected (NEED_KEY).

    This is what was broken before — keyRequired only reflected the Origin gate
    while access_check also enforced secret / fingerprint / under_attack. If a
    future change adds a gate to one side but not the other, this fails.
    """
    db = FakeDB()
    # Force every value-based gate to "fail" so a single worst-case keyless
    # request exercises all of them; bogus off so secret -> NEED_KEY.
    monkeypatch.setattr(secret, "secret_match", lambda s: False)
    monkeypatch.setattr(secret, "secret_bogus_match", lambda s: False)
    monkeypatch.setattr(flood, "fingerprint_mismatch", lambda ip, fp: True)

    bool_flags = ["under_attack", "require_api_key_secret", "require_api_key_fingerprint"]
    for origin in ["", "https://example.com"]:
        for combo in itertools.product([False, True], repeat=len(bool_flags)):
            args = make_args(require_api_key_origin=origin, **dict(zip(bool_flags, combo)))

            advertised = auth.key_required(args)
            enforced = call(
                args, db,
                api_key=None,
                req_secret="wrong",
                origin="https://does-not-match.invalid",
                fingerprint="x",
            ) == auth.NEED_KEY

            assert advertised == enforced, f"drift at origin={origin!r} combo={combo}"
