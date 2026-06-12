import io
import json
import os
import time
from unittest.mock import patch, MagicMock

import pytest

from libretranslate import file_tasks, storage as storage_module


# ---------------------------------------------------------------------------
# Mock language objects to avoid needing real argos models
# ---------------------------------------------------------------------------

class MockLanguage:
    """Mimics an argostranslate Language object enough for the app to work."""

    def __init__(self, code, name=None):
        self.code = code
        self.name = name or code.upper()
        self.translations_from = []

    def get_translation(self, target_lang):
        """Return a mock translator object."""
        return MockTranslator(self.code, target_lang.code)


class MockTranslator:
    """Mimics an argostranslate Translator."""

    def __init__(self, src, tgt):
        self.src = src
        self.tgt = tgt

    def translate(self, text):
        return f"[{self.tgt}] {text}"

    def hypotheses(self, text, num_hypotheses=1):
        return [self.translate(text)]


class MockTranslationFrom:
    def __init__(self, to_lang):
        self.to_lang = to_lang


# ---------------------------------------------------------------------------
# App fixture with mocked model loading
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    """Create Flask app with mocked language models (no real models needed)."""
    import sys
    sys.argv = ['', '--load-only', 'en,es']

    # Build mock languages
    en_lang = MockLanguage("en", "English")
    es_lang = MockLanguage("es", "Spanish")
    en_lang.translations_from = [MockTranslationFrom(es_lang)]
    es_lang.translations_from = [MockTranslationFrom(en_lang)]
    mock_languages = [en_lang, es_lang]

    with patch("libretranslate.init.boot") as mock_boot, \
         patch("libretranslate.language.load_languages", return_value=mock_languages):

        from libretranslate.main import get_args
        from libretranslate.app import create_app

        args = get_args()
        app = create_app(args)
        yield app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _cleanup_upload_dir():
    """Ensure upload dir exists and clean up test files after each test."""
    from libretranslate.app import get_upload_dir
    upload_dir = get_upload_dir()
    os.makedirs(upload_dir, exist_ok=True)
    files_before = set(os.listdir(upload_dir))
    yield
    for f in os.listdir(upload_dir):
        if f not in files_before:
            path = os.path.join(upload_dir, f)
            if os.path.isfile(path):
                os.remove(path)


@pytest.fixture(autouse=True)
def _init_storage():
    """Ensure storage is initialized for tests that use file_tasks directly."""
    if storage_module.get_storage() is None:
        storage_module.setup("memory://")
    yield


def _make_html_file(content="<p>Hello</p>"):
    """Create a minimal HTML file for upload."""
    return (io.BytesIO(content.encode("utf-8")), "test.html")


# ===========================================================================
# Test Classes
# ===========================================================================


class TestSubmitAsyncTranslation:
    """Tests for POST /translate_file_async"""

    def test_submit_returns_task_id(self, client):
        file_obj, filename = _make_html_file()
        with patch("libretranslate.file_tasks.submit_translation") as mock_submit:
            mock_submit.return_value = MagicMock()
            response = client.post("/translate_file_async", data={
                "file": (file_obj, filename),
                "source": "en",
                "target": "es",
            })

        assert response.status_code == 200
        data = json.loads(response.data)
        assert "taskId" in data
        assert data["status"] == "pending"
        assert "statusUrl" in data
        assert data["taskId"] in data["statusUrl"]

    def test_submit_missing_file_returns_400(self, client):
        response = client.post("/translate_file_async", data={
            "source": "en",
            "target": "es",
        })
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data

    def test_submit_missing_source_returns_400(self, client):
        file_obj, filename = _make_html_file()
        response = client.post("/translate_file_async", data={
            "file": (file_obj, filename),
            "target": "es",
        })
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "source" in data["error"]

    def test_submit_missing_target_returns_400(self, client):
        file_obj, filename = _make_html_file()
        response = client.post("/translate_file_async", data={
            "file": (file_obj, filename),
            "source": "en",
        })
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "target" in data["error"]

    def test_submit_unsupported_format_returns_400(self, client):
        response = client.post("/translate_file_async", data={
            "file": (io.BytesIO(b"test"), "test.xyz"),
            "source": "en",
            "target": "es",
        })
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "not supported" in data["error"]

    def test_submit_unsupported_source_language_returns_400(self, client):
        file_obj, filename = _make_html_file()
        response = client.post("/translate_file_async", data={
            "file": (file_obj, filename),
            "source": "zz",
            "target": "es",
        })
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "not supported" in data["error"]

    def test_submit_unsupported_target_language_returns_400(self, client):
        file_obj, filename = _make_html_file()
        response = client.post("/translate_file_async", data={
            "file": (file_obj, filename),
            "source": "en",
            "target": "zz",
        })
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "not supported" in data["error"]


class TestPollTaskStatus:
    """Tests for GET /tasks/<task_id>"""

    def test_poll_nonexistent_task_returns_404(self, client):
        response = client.get("/tasks/nonexistent-task-id")
        assert response.status_code == 404

    def test_poll_pending_task(self, client):
        """A freshly created task should be in pending state."""
        with patch("libretranslate.file_tasks.submit_translation") as mock_submit:
            mock_submit.return_value = MagicMock()
            resp = client.post("/translate_file_async", data={
                "file": _make_html_file(),
                "source": "en",
                "target": "es",
            })
        task_data = json.loads(resp.data)
        task_id = task_data["taskId"]

        response = client.get(f"/tasks/{task_id}")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["taskId"] == task_id
        assert data["status"] in ("pending", "processing", "completed", "failed")
        assert "created" in data

    def test_poll_completed_task_has_download_url(self, client):
        """After translation completes, polling should return translatedFileUrl."""
        task = file_tasks.create_task(
            source_lang="en",
            target_lang="es",
            filename="test.html",
            req_cost=1,
        )
        file_tasks.update_task(
            task["task_id"],
            status=file_tasks.TaskStatus.COMPLETED,
            translated_filename="translated-test.html",
        )

        response = client.get(f"/tasks/{task['task_id']}")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["status"] == "completed"
        assert "translatedFileUrl" in data
        assert "translated-test.html" in data["translatedFileUrl"]

    def test_poll_failed_task_has_error(self, client):
        """A failed task should include an error message."""
        task = file_tasks.create_task(
            source_lang="en",
            target_lang="es",
            filename="test.html",
            req_cost=1,
        )
        file_tasks.update_task(
            task["task_id"],
            status=file_tasks.TaskStatus.FAILED,
            error="Translation engine crashed",
        )

        response = client.get(f"/tasks/{task['task_id']}")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["status"] == "failed"
        assert data["error"] == "Translation engine crashed"


class TestAsyncTranslationEndToEnd:
    """End-to-end tests with mocked translation engine."""

    def test_submit_then_poll_completed(self, client):
        """Full flow: submit → poll until completed → download."""
        translated_content = b"<p>Hola</p>"

        def mock_translate(translator, filepath):
            """Simulate a successful translation by writing a translated file."""
            translated_path = filepath + ".translated"
            with open(translated_path, 'wb') as f:
                f.write(translated_content)
            return translated_path

        with patch("argostranslatefiles.translate_file", side_effect=mock_translate):
            resp = client.post("/translate_file_async", data={
                "file": _make_html_file("<p>Hello</p>"),
                "source": "en",
                "target": "es",
            })
            assert resp.status_code == 200
            task_data = json.loads(resp.data)
            task_id = task_data["taskId"]

            # Poll until completed (with timeout)
            result = None
            for _ in range(50):
                poll_resp = client.get(f"/tasks/{task_id}")
                result = json.loads(poll_resp.data)
                if result["status"] in ("completed", "failed"):
                    break
                time.sleep(0.2)

            assert result["status"] == "completed"
            assert "translatedFileUrl" in result

    def test_submit_then_translation_fails(self, client):
        """Translation failure should be captured in the task status."""
        def mock_translate_fail(translator, filepath):
            raise RuntimeError("Translation engine exploded")

        with patch("argostranslatefiles.translate_file", side_effect=mock_translate_fail):
            resp = client.post("/translate_file_async", data={
                "file": _make_html_file("<p>Hello</p>"),
                "source": "en",
                "target": "es",
            })
            assert resp.status_code == 200
            task_id = json.loads(resp.data)["taskId"]

            # Poll until failed
            result = None
            for _ in range(50):
                poll_resp = client.get(f"/tasks/{task_id}")
                result = json.loads(poll_resp.data)
                if result["status"] in ("completed", "failed"):
                    break
                time.sleep(0.2)

            assert result["status"] == "failed"
            assert "Translation engine exploded" in result["error"]


class TestTaskCleanup:
    """Tests for task expiration and stale task cleanup."""

    def test_expired_task_returns_404(self, client):
        """A task that has expired (TTL passed) should return 404."""
        task = file_tasks.create_task(
            source_lang="en",
            target_lang="es",
            filename="test.html",
        )
        task_id = task["task_id"]

        # Verify it exists first
        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200

        # Manually expire the task by deleting from storage
        store = storage_module.get_storage()
        store.store.pop(file_tasks._task_key(task_id), None)

        resp = client.get(f"/tasks/{task_id}")
        assert resp.status_code == 404

    def test_cleanup_marks_stale_processing_tasks_as_failed(self):
        """cleanup_stale_tasks should mark tasks stuck in processing as failed."""
        task = file_tasks.create_task(
            source_lang="en",
            target_lang="es",
            filename="test.html",
        )
        # Simulate a task stuck in processing for a long time
        file_tasks.update_task(
            task["task_id"],
            status=file_tasks.TaskStatus.PROCESSING,
            created=time.time() - 2000,  # 2000 seconds ago (> 15 min threshold)
        )

        file_tasks.cleanup_stale_tasks()

        updated = file_tasks.get_task(task["task_id"])
        assert updated is not None
        assert updated["status"] == file_tasks.TaskStatus.FAILED
        assert "timed out" in updated["error"]

    def test_cleanup_does_not_affect_recent_tasks(self):
        """cleanup_stale_tasks should not touch recently created processing tasks."""
        task = file_tasks.create_task(
            source_lang="en",
            target_lang="es",
            filename="test.html",
        )
        file_tasks.update_task(
            task["task_id"],
            status=file_tasks.TaskStatus.PROCESSING,
            created=time.time(),  # just created
        )

        file_tasks.cleanup_stale_tasks()

        updated = file_tasks.get_task(task["task_id"])
        assert updated is not None
        assert updated["status"] == file_tasks.TaskStatus.PROCESSING


class TestSyncEndpointRegression:
    """Ensure the existing synchronous endpoint still works correctly."""

    def test_sync_translate_file_still_works(self, client):
        """POST /translate_file should continue to work synchronously."""
        translated_content = b"<p>Hola</p>"

        def mock_translate(translator, filepath):
            translated_path = filepath + ".translated"
            with open(translated_path, 'wb') as f:
                f.write(translated_content)
            return translated_path

        with patch("argostranslatefiles.translate_file", side_effect=mock_translate):
            response = client.post("/translate_file", data={
                "file": _make_html_file("<p>Hello</p>"),
                "source": "en",
                "target": "es",
            })

        assert response.status_code == 200
        data = json.loads(response.data)
        assert "translatedFileUrl" in data


class TestFileTaskEngine:
    """Unit tests for the file_tasks module internals."""

    def test_create_task_returns_valid_dict(self):
        task = file_tasks.create_task(
            source_lang="en",
            target_lang="es",
            filename="test.html",
            req_cost=2,
            api_key="my-key",
            client_ip="127.0.0.1",
        )
        assert task["status"] == file_tasks.TaskStatus.PENDING
        assert task["source_lang"] == "en"
        assert task["target_lang"] == "es"
        assert task["filename"] == "test.html"
        assert task["req_cost"] == 2
        assert task["api_key"] == "my-key"
        assert task["client_ip"] == "127.0.0.1"
        assert task["error"] is None
        assert task["translated_filename"] is None
        assert "task_id" in task
        assert "created" in task

    def test_get_task_returns_none_for_missing(self):
        result = file_tasks.get_task("nonexistent-id")
        assert result is None

    def test_update_task_modifies_fields(self):
        task = file_tasks.create_task(
            source_lang="en",
            target_lang="es",
            filename="test.html",
        )
        updated = file_tasks.update_task(
            task["task_id"],
            status=file_tasks.TaskStatus.COMPLETED,
            translated_filename="output.html",
        )
        assert updated["status"] == file_tasks.TaskStatus.COMPLETED
        assert updated["translated_filename"] == "output.html"
        # Original fields preserved
        assert updated["source_lang"] == "en"

    def test_update_task_returns_none_for_missing(self):
        result = file_tasks.update_task("nonexistent-id", status="completed")
        assert result is None
