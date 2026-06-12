import json

import pytest

try:
    from libretranslate.language import load_languages
    _langs = load_languages()
    _models_available = len(_langs) >= 2
except Exception:
    _models_available = False

pytestmark = pytest.mark.skipif(
    not _models_available,
    reason="Language models not available (requires network/model download)"
)


def test_api_get_frontend_settings(client):
    response = client.get("/frontend/settings")

    assert response.status_code == 200


def test_frontend_settings_default_no_key_required(client):
    """Without --api-keys, keyRequired and underAttack must be false."""
    response = client.get("/frontend/settings")
    data = json.loads(response.data)
    assert data["keyRequired"] is False
    assert data["underAttack"] is False


def test_frontend_settings_key_required_with_origin(make_client):
    """--require-api-key-origin should set keyRequired=true."""
    client = make_client([
        '--api-keys',
        '--require-api-key-origin', 'https://example.com'
    ])
    response = client.get("/frontend/settings")
    data = json.loads(response.data)
    assert data["keyRequired"] is True


def test_frontend_settings_key_required_with_secret(make_client):
    """--require-api-key-secret should set keyRequired=true."""
    client = make_client(['--api-keys', '--require-api-key-secret'])
    response = client.get("/frontend/settings")
    data = json.loads(response.data)
    assert data["keyRequired"] is True


def test_frontend_settings_key_required_with_fingerprint(make_client):
    """--require-api-key-fingerprint should set keyRequired=true."""
    client = make_client(['--api-keys', '--require-api-key-fingerprint'])
    response = client.get("/frontend/settings")
    data = json.loads(response.data)
    assert data["keyRequired"] is True


def test_frontend_settings_under_attack(make_client):
    """--under-attack should set both keyRequired and underAttack."""
    client = make_client(['--api-keys', '--under-attack'])
    response = client.get("/frontend/settings")
    data = json.loads(response.data)
    assert data["keyRequired"] is True
    assert data["underAttack"] is True


def test_frontend_settings_api_keys_without_enforcement(make_client):
    """--api-keys alone (no enforcement flags) should NOT require keys."""
    client = make_client(['--api-keys'])
    response = client.get("/frontend/settings")
    data = json.loads(response.data)
    assert data["keyRequired"] is False


def test_translate_blocked_without_key_under_attack(make_client):
    """Under attack mode should block unauthenticated translate requests."""
    client = make_client(['--api-keys', '--under-attack'])
    response = client.post("/translate", data={
        "q": "Hello",
        "source": "en",
        "target": "es",
        "format": "text"
    })
    assert response.status_code == 400


def test_translate_rejects_invalid_key_under_attack(make_client):
    """Under attack mode should reject an invalid API key with 403."""
    client = make_client(['--api-keys', '--under-attack'])
    response = client.post("/translate", data={
        "q": "Hello",
        "source": "en",
        "target": "es",
        "format": "text",
        "api_key": "bogus-key"
    })
    assert response.status_code == 403


def test_translate_allowed_without_key_when_no_enforcement(client):
    """Default config (no enforcement) should allow unauthenticated requests."""
    response = client.post("/translate", data={
        "q": "Hello",
        "source": "en",
        "target": "es",
        "format": "text"
    })
    assert response.status_code == 200
