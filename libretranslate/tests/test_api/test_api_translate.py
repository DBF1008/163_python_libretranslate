import json
import sys

import pytest

from libretranslate.app import create_app
from libretranslate.main import get_args


@pytest.fixture()
def cached_client():
    sys.argv = ['', '--load-only', 'en,es', '--translation-cache', 'all']
    app = create_app(get_args())
    return app.test_client()


def test_api_translate(client):
    response = client.post("/translate", data={
        "q": "Hello",
        "source": "en",
        "target": "es",
        "format": "text"
    })

    response_json = json.loads(response.data)

    assert "translatedText" in response_json
    assert response.status_code == 200


def test_api_translate_batch(client):

    response = client.post("/translate", json={
        "q": ["Hello", "World"],
        "source": "en",
        "target": "es",
        "format": "text"
    })

    response_json = json.loads(response.data)

    assert "translatedText" in response_json
    assert isinstance(response_json["translatedText"], list)
    assert len(response_json["translatedText"]) == 2
    assert response.status_code == 200


def test_api_translate_unsupported_language(client):
    response = client.post("/translate", data={
        "q": "Hello",
        "source": "en",
        "target": "zz",
        "format": "text"
    })

    response_json = json.loads(response.data)

    assert "error" in response_json
    assert response_json["error"] == "zz is not supported"
    assert response.status_code == 400


def test_api_translate_missing_parameter(client):
    response = client.post("/translate", data={
        "source": "en",
        "target": "es",
        "format": "text"
    })

    response_json = json.loads(response.data)

    assert "error" in response_json
    assert response_json["error"] == "Invalid request: missing q parameter"
    assert response.status_code == 400


def test_api_translate_alternatives_not_a_number_form(client):
    response = client.post("/translate", data={
        "q": "Hello",
        "source": "en",
        "target": "es",
        "alternatives": "abc",
    })

    assert response.status_code == 400
    assert "error" in json.loads(response.data)


def test_api_translate_alternatives_not_a_number_json(client):
    # The JSON path used to raise an unhandled 500 here; it must now return the
    # same clean 400 as the form path.
    response = client.post("/translate", json={
        "q": "Hello",
        "source": "en",
        "target": "es",
        "alternatives": "abc",
    })

    assert response.status_code == 400
    assert "error" in json.loads(response.data)


def test_api_translate_cache_roundtrip(cached_client):
    payload = {"q": "Hello", "source": "en", "target": "es", "format": "text"}

    first = cached_client.post("/translate", json=payload)
    second = cached_client.post("/translate", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    # The second request is served from cache and must be byte-for-byte identical.
    assert first.data == second.data


def test_api_translate_cache_omitted_format_matches_text(cached_client):
    # An omitted format and format="text" describe the same request, so they
    # must share a cache entry and return the same translation.
    omitted = cached_client.post("/translate", json={
        "q": "Hello", "source": "en", "target": "es",
    })
    explicit = cached_client.post("/translate", json={
        "q": "Hello", "source": "en", "target": "es", "format": "text",
    })

    assert omitted.status_code == 200
    assert explicit.status_code == 200
    assert json.loads(omitted.data)["translatedText"] == json.loads(explicit.data)["translatedText"]
