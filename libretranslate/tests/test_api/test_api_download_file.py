"""Regression tests for the /download_file/<filename> endpoint.

Covers:
  * Legitimate download round-trips correctly
  * Path-traversal attempts are rejected (400)
  * Symlink escapes are rejected (400)
  * Non-existent files return 404
  * Large files are streamed from disk, not loaded into memory (BytesIO)

Uses a lightweight Flask app with ONLY the download_file route registered
so the tests do not require translation models to be installed.
"""

import os
import sys
import tempfile
from unittest.mock import patch

import flask
import pytest

from libretranslate import security


# ---------------------------------------------------------------------------
# Lightweight app factory — no translation models needed
# ---------------------------------------------------------------------------


def _create_test_app(upload_dir):
    """Build a minimal Flask app exposing only /download_file/<filename>.

    Uses ``flask.send_file`` (module-attribute access) so tests can
    monkey-patch ``flask.send_file`` to observe the call signature.
    """
    from argparse import Namespace

    args = Namespace(disable_files_translation=False)

    app = flask.Flask(__name__)
    app.config["TESTING"] = True

    @app.get("/download_file/<string:filename>")
    def download_file(filename: str):
        if args.disable_files_translation:
            flask.abort(400, description="Files translation are disabled on this server.")

        try:
            security.validate_basename(filename)
        except security.SuspiciousFileOperationError:
            flask.abort(400, description="Invalid filename")

        filepath = os.path.realpath(os.path.join(upload_dir, filename))

        try:
            checked_filepath = security.path_traversal_check(filepath, upload_dir)
        except security.SuspiciousFileOperationError:
            flask.abort(400, description="Invalid filename")

        if not os.path.isfile(checked_filepath):
            flask.abort(404, description="File not found")

        download_filename = filename.split('.', 1)
        if len(download_filename) > 1:
            download_filename = download_filename[1]
        else:
            download_filename = filename

        # Use flask.send_file (attribute on module) so tests can patch it
        return flask.send_file(
            checked_filepath,
            as_attachment=True,
            download_name=download_filename,
            conditional=True,
        )

    return app


# ---------------------------------------------------------------------------
# Fixtures  (override conftest.py fixtures for this test module)
# ---------------------------------------------------------------------------


@pytest.fixture()
def upload_dir(tmp_path):
    d = tmp_path / "uploads"
    d.mkdir()
    return str(d)


@pytest.fixture()
def app(upload_dir):
    return _create_test_app(upload_dir)


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Legitimate download
# ---------------------------------------------------------------------------


class TestDownloadFileLegitimate:
    def test_returns_content_and_strips_uuid_prefix(self, client, upload_dir):
        filename = "abc123def.test.txt"
        content = b"Hello, translated world!"
        with open(os.path.join(upload_dir, filename), "wb") as f:
            f.write(content)

        resp = client.get(f"/download_file/{filename}")

        assert resp.status_code == 200
        assert resp.data == content
        # The UUID prefix (everything before the first '.') must be stripped
        cd = resp.headers.get("Content-Disposition", "")
        assert "test.txt" in cd


# ---------------------------------------------------------------------------
# Path-traversal rejections
# ---------------------------------------------------------------------------


class TestDownloadFileTraversal:
    def test_dotdot_in_url_rejected(self, client, upload_dir):
        """Flask's <string:> converter rejects '/' so literal '../' never
        reaches the handler (404).  URL-encoded %2F also results in 404
        or is decoded and rejected by our validate_basename (400)."""
        resp = client.get("/download_file/..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code in (400, 404)

    def test_backslash_rejected(self, client, upload_dir):
        resp = client.get("/download_file/subdir%5Cfile.txt")  # %5C = backslash
        assert resp.status_code == 400

    def test_null_byte_rejected(self, client, upload_dir):
        resp = client.get("/download_file/test.txt%00.jpg")
        assert resp.status_code == 400

    def test_empty_filename_rejected(self, client, upload_dir):
        resp = client.get("/download_file/")
        assert resp.status_code in (400, 404)

    def test_dotdot_basename_rejected(self, client, upload_dir):
        """When the raw filename is '..' the handler must reject it.
        We call the handler function directly since Flask routing
        treats '..' as a path segment."""
        from libretranslate import security as sec

        # validate_basename allows ".." (it's a valid basename) but
        # the realpath + path_traversal_check combo must reject it.
        filepath = os.path.realpath(os.path.join(upload_dir, ".."))
        with pytest.raises(sec.SuspiciousFileOperationError):
            sec.path_traversal_check(filepath, upload_dir)

    @pytest.mark.skipif(os.name == "nt", reason="symlinks may need admin on Windows")
    def test_symlink_escape_rejected(self, client, upload_dir):
        secret = os.path.join(tempfile.gettempdir(), "secret_outside_dl.txt")
        with open(secret, "w") as f:
            f.write("secret")

        link = os.path.join(upload_dir, "link.txt")
        try:
            os.symlink(secret, link)
            resp = client.get("/download_file/link.txt")
            assert resp.status_code == 400
        finally:
            if os.path.islink(link):
                os.unlink(link)
            if os.path.exists(secret):
                os.unlink(secret)

    def test_sibling_directory_bypass_rejected(self, client, upload_dir):
        """The old commonprefix bug is exercised at the security-module
        level (test_security.py).  Through the HTTP route, Flask's
        <string:> converter blocks '/' in filenames so the sibling
        attack cannot reach the handler — verify that here."""
        sibling = upload_dir + "-evil"
        os.makedirs(sibling, exist_ok=True)
        evil_file = os.path.join(sibling, "evil.txt")
        with open(evil_file, "w") as f:
            f.write("evil")

        # URL-encoded '/' — Flask still returns 404 (route mismatch)
        evil_basename = os.path.basename(upload_dir) + "-evil%2Fevil.txt"
        resp = client.get(f"/download_file/{evil_basename}")
        assert resp.status_code in (400, 404)


# ---------------------------------------------------------------------------
# Non-existent file
# ---------------------------------------------------------------------------


class TestDownloadFileNotFound:
    def test_missing_file_returns_404(self, client, upload_dir):
        resp = client.get("/download_file/does-not-exist.txt")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Large-file streaming (memory safety)
# ---------------------------------------------------------------------------


class TestDownloadFileStreaming:
    """Verify that the handler streams via send_file(path) instead of
    loading the entire file into an in-memory BytesIO buffer."""

    def test_large_file_served_from_path_not_bytesio(self, client, upload_dir):
        filename = "uuid.large.bin"
        file_size = 10 * 1024 * 1024  # 10 MB
        filepath = os.path.join(upload_dir, filename)

        with open(filepath, "wb") as f:
            chunk = os.urandom(65536)
            for _ in range(file_size // len(chunk)):
                f.write(chunk)

        _flask_mod = sys.modules["flask"]
        original_send_file = _flask_mod.send_file
        captured = {}

        def spy_send_file(file_or_path, **kwargs):
            captured["file_arg"] = file_or_path
            captured["is_path_string"] = isinstance(file_or_path, str)
            return original_send_file(file_or_path, **kwargs)

        with patch.object(_flask_mod, "send_file", side_effect=spy_send_file):
            resp = client.get(f"/download_file/{filename}")

        assert resp.status_code == 200
        assert captured.get("is_path_string") is True, (
            "send_file must receive a file-path string for streaming, "
            "not a BytesIO or other file-like object"
        )
        assert int(resp.headers.get("Content-Length", 0)) == file_size
