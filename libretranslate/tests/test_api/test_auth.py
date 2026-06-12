from types import SimpleNamespace

from libretranslate.app import is_key_required


def _make_args(**overrides):
    """Build a minimal args namespace with sensible defaults."""
    defaults = {
        'api_keys': False,
        'under_attack': False,
        'require_api_key_origin': '',
        'require_api_key_secret': False,
        'require_api_key_fingerprint': False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_no_api_keys_returns_false():
    assert is_key_required(_make_args()) is False


def test_api_keys_alone_returns_false():
    """Enabling api_keys without any enforcement flag should not require keys."""
    assert is_key_required(_make_args(api_keys=True)) is False


def test_under_attack_requires_key():
    assert is_key_required(_make_args(api_keys=True, under_attack=True)) is True


def test_require_origin_requires_key():
    assert is_key_required(_make_args(
        api_keys=True, require_api_key_origin='https://example.com'
    )) is True


def test_require_secret_requires_key():
    assert is_key_required(_make_args(
        api_keys=True, require_api_key_secret=True
    )) is True


def test_require_fingerprint_requires_key():
    assert is_key_required(_make_args(
        api_keys=True, require_api_key_fingerprint=True
    )) is True


def test_multiple_flags_requires_key():
    assert is_key_required(_make_args(
        api_keys=True,
        require_api_key_secret=True,
        require_api_key_fingerprint=True,
    )) is True


def test_enforcement_without_api_keys_returns_false():
    """Enforcement flags without api_keys enabled should be ignored."""
    assert is_key_required(_make_args(
        under_attack=True,
        require_api_key_secret=True,
    )) is False
