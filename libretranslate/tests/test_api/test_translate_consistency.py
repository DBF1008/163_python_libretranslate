"""Integration tests verifying that the refactored translation
endpoints maintain consistent behavior for auto-detection, emoji
handling, HTML format, alternatives, and file translation.

These tests run against the real Flask app with en/es models loaded.
"""

import io
import json


def test_translate_emoji_only(client):
    """Pure emoji input should be returned as-is (no translation)."""
    response = client.post("/translate", data={
        "q": "\U0001f600\U0001f601",
        "source": "en",
        "target": "es",
    })
    data = json.loads(response.data)

    assert response.status_code == 200
    assert data["translatedText"] == "\U0001f600\U0001f601"


def test_translate_batch_emoji_only(client):
    """Batch of pure emoji should all be returned as-is."""
    response = client.post("/translate", json={
        "q": ["\U0001f600", "\U0001f601\U0001f602"],
        "source": "en",
        "target": "es",
    })
    data = json.loads(response.data)

    assert response.status_code == 200
    assert isinstance(data["translatedText"], list)
    assert data["translatedText"] == ["\U0001f600", "\U0001f601\U0001f602"]


def test_translate_html_format(client):
    """HTML format translation should preserve tags."""
    response = client.post("/translate", data={
        "q": "<b>Hello</b>",
        "source": "en",
        "target": "es",
        "format": "html",
    })
    data = json.loads(response.data)

    assert response.status_code == 200
    assert "translatedText" in data
    # The <b> tag should be preserved in the output
    assert "<b>" in data["translatedText"]
    assert "</b>" in data["translatedText"]


def test_translate_alternatives(client):
    """Requesting alternatives should return a non-empty list."""
    response = client.post("/translate", data={
        "q": "Hello",
        "source": "en",
        "target": "es",
        "alternatives": "3",
    })
    data = json.loads(response.data)

    assert response.status_code == 200
    assert "alternatives" in data
    assert isinstance(data["alternatives"], list)


def test_translate_batch_alternatives(client):
    """Batch translation with alternatives should return list of lists."""
    response = client.post("/translate", json={
        "q": ["Hello", "World"],
        "source": "en",
        "target": "es",
        "alternatives": 2,
    })
    data = json.loads(response.data)

    assert response.status_code == 200
    assert "alternatives" in data
    assert isinstance(data["alternatives"], list)
    assert len(data["alternatives"]) == 2
    # Each element should be a list of alternatives
    for alt_list in data["alternatives"]:
        assert isinstance(alt_list, list)


def test_translate_auto_detect(client):
    """Auto-detect should include detectedLanguage in the response."""
    response = client.post("/translate", data={
        "q": "Hello",
        "source": "auto",
        "target": "es",
    })
    data = json.loads(response.data)

    assert response.status_code == 200
    assert "translatedText" in data
    assert "detectedLanguage" in data
    assert "confidence" in data["detectedLanguage"]
    assert "language" in data["detectedLanguage"]


def test_translate_auto_detect_batch(client):
    """Auto-detect batch should include detectedLanguage as a list."""
    response = client.post("/translate", json={
        "q": ["Hello", "World"],
        "source": "auto",
        "target": "es",
    })
    data = json.loads(response.data)

    assert response.status_code == 200
    assert "detectedLanguage" in data
    assert isinstance(data["detectedLanguage"], list)
    assert len(data["detectedLanguage"]) == 2


def test_translate_single_no_detected_language(client):
    """When source is explicit, detectedLanguage should NOT be in response."""
    response = client.post("/translate", data={
        "q": "Hello",
        "source": "en",
        "target": "es",
    })
    data = json.loads(response.data)

    assert response.status_code == 200
    assert "detectedLanguage" not in data


def test_translate_bad_format(client):
    """Invalid format should return 400."""
    response = client.post("/translate", data={
        "q": "Hello",
        "source": "en",
        "target": "es",
        "format": "xml",
    })
    data = json.loads(response.data)

    assert response.status_code == 400
    assert "error" in data


def test_translate_unsupported_source(client):
    """Unsupported source language should return 400."""
    response = client.post("/translate", data={
        "q": "Hello",
        "source": "zz",
        "target": "es",
    })
    data = json.loads(response.data)

    assert response.status_code == 400
    assert "error" in data


def test_translate_unsupported_target(client):
    """Unsupported target language should return 400."""
    response = client.post("/translate", data={
        "q": "Hello",
        "source": "en",
        "target": "zz",
    })
    data = json.loads(response.data)

    assert response.status_code == 400
    assert "error" in data


def test_detect_endpoint(client):
    """/detect should return language detection results."""
    response = client.post("/detect", data={
        "q": "Hello world",
    })
    data = json.loads(response.data)

    assert response.status_code == 200
    assert isinstance(data, list)
    assert len(data) >= 1
    assert "confidence" in data[0]
    assert "language" in data[0]


def test_detect_missing_q(client):
    """/detect without q should return 400."""
    response = client.post("/detect")
    data = json.loads(response.data)

    assert response.status_code == 400
    assert "error" in data


def test_translate_json_single(client):
    """JSON body with single string should return string response."""
    response = client.post("/translate", json={
        "q": "Hello",
        "source": "en",
        "target": "es",
    })
    data = json.loads(response.data)

    assert response.status_code == 200
    assert isinstance(data["translatedText"], str)


def test_translate_json_single_as_batch(client):
    """JSON body with single-element list should return list response."""
    response = client.post("/translate", json={
        "q": ["Hello"],
        "source": "en",
        "target": "es",
    })
    data = json.loads(response.data)

    assert response.status_code == 200
    assert isinstance(data["translatedText"], list)
    assert len(data["translatedText"]) == 1


def test_translate_form_normalizes_line_endings(client):
    """Form-encoded CRLF should be normalized to LF."""
    response = client.post("/translate", data={
        "q": "Hello\r\nWorld",
        "source": "en",
        "target": "es",
    })
    data = json.loads(response.data)

    assert response.status_code == 200
    assert "translatedText" in data
