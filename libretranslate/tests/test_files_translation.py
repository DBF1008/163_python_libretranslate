import os
import tempfile

from libretranslate.files_translation import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    FileTranslationTasks,
)


def _write_temp_file(content=b"translated"):
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return path


def test_task_success():
    tasks = FileTranslationTasks(max_workers=2)
    produced = _write_temp_file()
    try:
        task = tasks.submit("en", "es", "doc.txt", lambda: produced)
        finished = tasks.wait(task.id, timeout=5)

        assert finished.status == STATUS_COMPLETED
        assert finished.error is None
        assert finished.translated_filepath == produced
        assert finished.translated_filename == os.path.basename(produced)

        body = finished.to_dict()
        assert body["status"] == STATUS_COMPLETED
        assert body["source"] == "en"
        assert body["target"] == "es"
        assert body["originalFilename"] == "doc.txt"
        assert body["translatedFilename"] == os.path.basename(produced)
        assert "error" not in body
    finally:
        tasks.shutdown()
        if os.path.exists(produced):
            os.remove(produced)


def test_task_failure_records_error():
    tasks = FileTranslationTasks(max_workers=2)
    try:
        def job():
            raise ValueError("boom while translating")

        task = tasks.submit("en", "es", "doc.txt", job)
        finished = tasks.wait(task.id, timeout=5)

        assert finished.status == STATUS_FAILED
        assert "boom while translating" in finished.error
        assert finished.translated_filename is None

        body = finished.to_dict()
        assert body["status"] == STATUS_FAILED
        assert "boom while translating" in body["error"]
        # URLs are never built by the store, even on failure.
        assert "translatedFileUrl" not in body
    finally:
        tasks.shutdown()


def test_cleanup_removes_expired_task_and_output_file():
    clock = {"t": 1000.0}
    tasks = FileTranslationTasks(max_workers=2, retention=1800, time_fn=lambda: clock["t"])
    produced = _write_temp_file()
    try:
        task = tasks.submit("en", "es", "doc.txt", lambda: produced)
        tasks.wait(task.id, timeout=5)
        assert tasks.get(task.id) is not None
        assert os.path.exists(produced)

        # Exactly at the retention boundary -> not yet expired, survives.
        assert tasks.cleanup(now=clock["t"] + 1800) == []
        assert tasks.get(task.id) is not None
        assert os.path.exists(produced)

        # Past the retention window -> record pruned and output file deleted.
        removed = tasks.cleanup(now=clock["t"] + 1800 + 1)
        assert task.id in removed
        assert tasks.get(task.id) is None
        assert not os.path.exists(produced)
    finally:
        tasks.shutdown()
        if os.path.exists(produced):
            os.remove(produced)


def test_get_unknown_task_returns_none():
    tasks = FileTranslationTasks(max_workers=1)
    try:
        assert tasks.get("does-not-exist") is None
    finally:
        tasks.shutdown()
