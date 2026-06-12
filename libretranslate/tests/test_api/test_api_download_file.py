import os
import uuid

import pytest

from libretranslate.app import get_upload_dir


@pytest.fixture()
def upload_file():
    """Write a file into the translate upload dir and clean it up afterwards.

    Translated files are stored as ``<uuid>.<original-name>`` (see the
    /translate_file handler), and download_file strips the uuid prefix to build
    the user-facing download name, so the fixture mirrors that layout.
    """
    created = []

    def _make(content: bytes, original_name: str = "report.txt") -> str:
        upload_dir = get_upload_dir()
        stored_name = f"{uuid.uuid4()}.{original_name}"
        path = os.path.join(upload_dir, stored_name)
        with open(path, "wb") as f:
            f.write(content)
        created.append(path)
        return stored_name

    yield _make

    for path in created:
        try:
            os.remove(path)
        except OSError:
            pass


def test_download_legit_file(client, upload_file):
    content = b"hello translated world\n"
    stored_name = upload_file(content, "myfile.txt")

    response = client.get(f"/download_file/{stored_name}")

    assert response.status_code == 200
    assert response.data == content

    disposition = response.headers.get("Content-Disposition", "")
    assert "attachment" in disposition
    # The uuid prefix is stripped from the name presented to the user.
    assert "myfile.txt" in disposition


def test_download_rejects_parent_traversal(client):
    # ".." has no slash, so it reaches the <string:filename> route, resolves to
    # the parent of the upload directory, and must be explicitly rejected.
    response = client.get("/download_file/..")

    assert response.status_code == 400
    assert b"Invalid filename" in response.data


def test_download_missing_file_returns_404(client):
    response = client.get(f"/download_file/{uuid.uuid4()}.nope.txt")

    assert response.status_code == 404


def test_download_large_file_is_streamed_not_buffered(client, upload_file):
    # ~5 MiB payload. Exercises the streaming path end-to-end and pins the fix
    # that previously read the whole file into an in-memory BytesIO buffer.
    content = (b"0123456789abcdef" * 64) * 5120  # 5 MiB
    stored_name = upload_file(content, "big.bin")

    response = client.get(f"/download_file/{stored_name}")

    assert response.status_code == 200
    assert len(response.data) == len(content)
    assert response.data == content
    # send_file(<path>) serves the file from disk and therefore sets
    # Last-Modified from the file's mtime; the old in-memory BytesIO response
    # could not, so this header is a reliable signal that we no longer buffer
    # the entire file in memory.
    assert response.headers.get("Last-Modified") is not None


def test_download_large_file_supports_range_requests(client, upload_file):
    # Disk-backed streaming supports partial/resumable downloads, which matters
    # for large files. The old buffered response did not advertise this.
    content = bytes(range(256)) * 4096  # 1 MiB of deterministic bytes
    stored_name = upload_file(content, "ranged.bin")

    response = client.get(
        f"/download_file/{stored_name}",
        headers={"Range": "bytes=0-9"},
    )

    assert response.status_code == 206
    assert response.data == content[:10]
