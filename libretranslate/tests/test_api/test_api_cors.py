import sys

import pytest

from libretranslate.app import create_app
from libretranslate.main import get_args

ALLOWED_ORIGIN = "https://good.example"
OTHER_ALLOWED_ORIGIN = "https://also-good.example"
DENIED_ORIGIN = "https://evil.example"
MAX_AGE = str(60 * 60 * 24 * 20)


def _make_client(extra_args):
    sys.argv = ['', '--load-only', 'en,es', *extra_args]
    app = create_app(get_args())
    return app.test_client()


@pytest.fixture(scope="module")
def open_client():
    # No --cors-* flags -> default, fully open CORS (backward compatible).
    return _make_client([])


@pytest.fixture(scope="module")
def restricted_client():
    return _make_client([
        '--cors-origins', f'{ALLOWED_ORIGIN},{OTHER_ALLOWED_ORIGIN}',
    ])


# --- Open mode (default, must stay backward compatible) ----------------------

def test_open_mode_returns_wildcard_and_full_header_set(open_client):
    resp = open_client.get("/languages")

    assert resp.status_code == 200
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    assert resp.headers.get("Access-Control-Allow-Credentials") == "true"
    assert resp.headers.get("Access-Control-Allow-Methods") == "GET, POST"
    assert resp.headers.get("Access-Control-Allow-Headers") == "Authorization, Content-Type"
    assert resp.headers.get("Access-Control-Expose-Headers") == "Authorization"
    assert resp.headers.get("Access-Control-Max-Age") == MAX_AGE


def test_open_mode_ignores_request_origin(open_client):
    # An incoming Origin must not change the wildcard response in open mode.
    resp = open_client.get("/languages", headers={"Origin": DENIED_ORIGIN})

    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


# --- Restricted mode --------------------------------------------------------

def test_restricted_mode_echoes_allowed_origin(restricted_client):
    resp = restricted_client.get("/languages", headers={"Origin": ALLOWED_ORIGIN})

    assert resp.status_code == 200
    # The exact origin is echoed (not "*") so credentialed requests are valid.
    assert resp.headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN
    assert resp.headers.get("Access-Control-Allow-Credentials") == "true"
    assert "Origin" in resp.headers.get("Vary", "")


def test_restricted_mode_supports_multiple_allowed_origins(restricted_client):
    resp = restricted_client.get("/languages", headers={"Origin": OTHER_ALLOWED_ORIGIN})

    assert resp.headers.get("Access-Control-Allow-Origin") == OTHER_ALLOWED_ORIGIN


def test_restricted_mode_blocks_unknown_origin(restricted_client):
    resp = restricted_client.get("/languages", headers={"Origin": DENIED_ORIGIN})

    # The request still succeeds, but without a CORS grant the browser blocks it.
    assert resp.status_code == 200
    assert resp.headers.get("Access-Control-Allow-Origin") is None
    assert resp.headers.get("Access-Control-Allow-Credentials") is None
    # Vary: Origin is required so caches don't reuse one origin's response.
    assert "Origin" in resp.headers.get("Vary", "")


def test_restricted_mode_without_origin_header(restricted_client):
    # Same-origin / non-browser callers (no Origin) get no wildcard grant.
    resp = restricted_client.get("/languages")

    assert resp.status_code == 200
    assert resp.headers.get("Access-Control-Allow-Origin") is None


# --- Preflight (OPTIONS) ----------------------------------------------------

def test_preflight_open_mode(open_client):
    resp = open_client.options(
        "/translate",
        headers={
            "Origin": DENIED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )

    assert resp.status_code in (200, 204)
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    assert resp.headers.get("Access-Control-Allow-Methods") == "GET, POST"
    assert resp.headers.get("Access-Control-Allow-Headers") == "Authorization, Content-Type"
    assert resp.headers.get("Access-Control-Max-Age") == MAX_AGE


def test_preflight_restricted_allowed_origin(restricted_client):
    resp = restricted_client.options(
        "/translate",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )

    assert resp.status_code in (200, 204)
    assert resp.headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN
    assert resp.headers.get("Access-Control-Allow-Credentials") == "true"
    assert resp.headers.get("Access-Control-Allow-Methods") == "GET, POST"
    assert resp.headers.get("Access-Control-Allow-Headers") == "Authorization, Content-Type"


def test_preflight_restricted_denied_origin(restricted_client):
    resp = restricted_client.options(
        "/translate",
        headers={
            "Origin": DENIED_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )

    assert resp.headers.get("Access-Control-Allow-Origin") is None
    assert resp.headers.get("Access-Control-Allow-Credentials") is None


# --- Configurable methods / headers -----------------------------------------

def test_configurable_methods_and_headers():
    client = _make_client([
        "--cors-methods", "GET, POST, PUT, DELETE, OPTIONS",
        "--cors-headers", "Authorization, Content-Type, X-Requested-With",
    ])

    resp = client.get("/languages")

    assert resp.headers.get("Access-Control-Allow-Methods") == "GET, POST, PUT, DELETE, OPTIONS"
    assert resp.headers.get("Access-Control-Allow-Headers") == "Authorization, Content-Type, X-Requested-With"
    # Origin handling is independent of method/header config (still open here).
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
