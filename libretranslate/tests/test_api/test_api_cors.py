import sys
from unittest.mock import patch, MagicMock

import pytest

from libretranslate.main import get_args


def _make_mock_language(code, to_codes=None):
    """Create a mock language object compatible with create_app()."""
    lang = MagicMock()
    lang.code = code
    lang.name = code.upper()
    lang.translations_from = []
    for to_code in (to_codes or []):
        to_lang = MagicMock()
        to_lang.code = to_code
        translation = MagicMock()
        translation.to_lang = to_lang
        lang.translations_from.append(translation)
    return lang


_MOCK_LANGUAGES = [
    _make_mock_language("en", ["es"]),
    _make_mock_language("es", ["en"]),
]


def _create_test_app(argv):
    """Create app with mocked language models (no real downloads needed)."""
    sys.argv = argv
    with patch("libretranslate.init.boot"), \
         patch("libretranslate.language.load_languages", return_value=_MOCK_LANGUAGES), \
         patch("libretranslate.detect.Detector", MagicMock()):
        from libretranslate.app import create_app
        app = create_app(get_args())
    return app


@pytest.fixture()
def open_client():
    """Client with default (wildcard) CORS — backward compatible."""
    app = _create_test_app(["", "--load-only", "en,es"])
    return app.test_client()


@pytest.fixture()
def restricted_client():
    """Client with specific allowed origins."""
    app = _create_test_app([
        "",
        "--load-only",
        "en,es",
        "--cors-origins",
        "https://example.com,https://app.example.com",
    ])
    return app.test_client()


# === WILDCARD / OPEN MODE (backward compatibility) ===


class TestCorsOpenMode:
    def test_wildcard_origin_header(self, open_client):
        response = open_client.get("/health")
        assert response.headers["Access-Control-Allow-Origin"] == "*"

    def test_no_credentials_in_wildcard_mode(self, open_client):
        """Access-Control-Allow-Credentials must NOT be set with wildcard origin (spec violation)."""
        response = open_client.get("/health")
        assert "Access-Control-Allow-Credentials" not in response.headers

    def test_wildcard_default_methods(self, open_client):
        response = open_client.get("/health")
        assert response.headers["Access-Control-Allow-Methods"] == "GET, POST"

    def test_wildcard_default_headers(self, open_client):
        response = open_client.get("/health")
        assert (
            response.headers["Access-Control-Allow-Headers"]
            == "Authorization, Content-Type"
        )

    def test_wildcard_expose_headers(self, open_client):
        response = open_client.get("/health")
        assert response.headers["Access-Control-Expose-Headers"] == "Authorization"

    def test_wildcard_preflight_returns_204(self, open_client):
        response = open_client.options("/translate")
        assert response.status_code == 204
        assert response.headers["Access-Control-Allow-Origin"] == "*"

    def test_preflight_on_health(self, open_client):
        response = open_client.options("/health")
        assert response.status_code == 204
        assert "Access-Control-Allow-Origin" in response.headers

    def test_preflight_on_detect(self, open_client):
        response = open_client.options("/detect")
        assert response.status_code == 204

    def test_preflight_has_max_age(self, open_client):
        response = open_client.options("/translate")
        assert response.headers["Access-Control-Max-Age"] == str(60 * 60 * 24 * 20)

    def test_preflight_has_allow_headers(self, open_client):
        response = open_client.options("/translate")
        assert (
            response.headers["Access-Control-Allow-Headers"]
            == "Authorization, Content-Type"
        )


# === RESTRICTED MODE ===


class TestCorsRestrictedMode:
    def test_matching_origin_echoed(self, restricted_client):
        response = restricted_client.get(
            "/health", headers={"Origin": "https://example.com"}
        )
        assert (
            response.headers["Access-Control-Allow-Origin"] == "https://example.com"
        )

    def test_second_matching_origin(self, restricted_client):
        response = restricted_client.get(
            "/health", headers={"Origin": "https://app.example.com"}
        )
        assert (
            response.headers["Access-Control-Allow-Origin"]
            == "https://app.example.com"
        )

    def test_non_matching_origin_rejected(self, restricted_client):
        response = restricted_client.get(
            "/health", headers={"Origin": "https://evil.com"}
        )
        assert "Access-Control-Allow-Origin" not in response.headers

    def test_credentials_set_in_restricted_mode(self, restricted_client):
        response = restricted_client.get(
            "/health", headers={"Origin": "https://example.com"}
        )
        assert (
            response.headers["Access-Control-Allow-Credentials"] == "true"
        )

    def test_vary_header_set(self, restricted_client):
        response = restricted_client.get(
            "/health", headers={"Origin": "https://example.com"}
        )
        assert "Origin" in response.headers.get("Vary", "")

    def test_no_origin_header_in_restricted_mode(self, restricted_client):
        """Request without Origin header should not get Access-Control-Allow-Origin."""
        response = restricted_client.get("/health")
        assert "Access-Control-Allow-Origin" not in response.headers

    def test_restricted_preflight_matching(self, restricted_client):
        response = restricted_client.options(
            "/translate", headers={"Origin": "https://example.com"}
        )
        assert response.status_code == 204
        assert (
            response.headers["Access-Control-Allow-Origin"] == "https://example.com"
        )
        assert (
            response.headers["Access-Control-Allow-Credentials"] == "true"
        )

    def test_restricted_preflight_non_matching(self, restricted_client):
        response = restricted_client.options(
            "/translate", headers={"Origin": "https://evil.com"}
        )
        assert response.status_code == 204
        assert "Access-Control-Allow-Origin" not in response.headers
