import io
import json
import time
import uuid
from urllib.parse import urlparse


def _upload(client, content=b"Hello world", filename="test.txt", **extra):
    data = {
        "source": "en",
        "target": "es",
        "file": (io.BytesIO(content), filename),
    }
    data.update(extra)
    return client.post("/translate_file", data=data, content_type="multipart/form-data")


def _poll_status(client, status_path, attempts=80, delay=0.25):
    """Poll a task status URL until it leaves the queued/running states."""
    for _ in range(attempts):
        response = client.get(status_path)
        body = json.loads(response.data)
        if body.get("status") in ("completed", "failed"):
            return response, body
        time.sleep(delay)
    return response, body


def test_translate_file_sync_unchanged(client):
    # The synchronous path (no async flag) keeps its original 200 + URL contract.
    response = _upload(client)
    assert response.status_code == 200

    body = json.loads(response.data)
    assert "translatedFileUrl" in body

    download = client.get(urlparse(body["translatedFileUrl"]).path)
    assert download.status_code == 200
    assert len(download.data) > 0


def test_translate_file_async_success(client):
    response = _upload(client, **{"async": "true"})
    assert response.status_code == 202

    body = json.loads(response.data)
    assert body["status"] == "queued"
    assert "id" in body
    assert "statusUrl" in body

    status_path = urlparse(body["statusUrl"]).path
    status_response, status_body = _poll_status(client, status_path)

    assert status_response.status_code == 200
    assert status_body["status"] == "completed"
    assert "translatedFileUrl" in status_body

    download = client.get(urlparse(status_body["translatedFileUrl"]).path)
    assert download.status_code == 200
    assert len(download.data) > 0


def test_translate_file_async_still_validates_request(client):
    # Async requests must still get proper request validation up front.
    response = client.post(
        "/translate_file",
        data={
            "source": "en",
            "file": (io.BytesIO(b"Hello world"), "test.txt"),
            "async": "true",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    body = json.loads(response.data)
    assert "error" in body


def test_translate_file_status_unknown_task(client):
    response = client.get("/translate_file_status/%s" % uuid.uuid4().hex)
    assert response.status_code == 404
    body = json.loads(response.data)
    assert "error" in body
