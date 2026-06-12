"""In-memory task store and background runner for asynchronous file translation.

This module is intentionally dependency-free (standard library only) so that the
task lifecycle (queue -> run -> complete/fail -> cleanup) can be unit tested
without loading the translation models or the Flask app.

The actual translation work is supplied by the caller as a ``job`` callable;
this module only tracks status, captures errors, and prunes expired records.
"""

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

# Keep task records (and their produced files) around for the same amount of
# time that remove_translated_files.py keeps translated files on disk, so a
# status lookup never points at a file that has already been swept away.
DEFAULT_RETENTION_SECONDS = 1800  # 30 minutes
DEFAULT_MAX_WORKERS = 4

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


class FileTranslationTask:
    """A single asynchronous file-translation job and its current state."""

    def __init__(self, task_id, source, target, original_filename, created_at):
        self.id = task_id
        self.source = source
        self.target = target
        self.original_filename = original_filename
        self.status = STATUS_QUEUED
        self.error = None
        self.translated_filename = None  # basename, used to build the download URL
        self.translated_filepath = None  # absolute path, used to clean the file up
        self.created_at = created_at
        self.updated_at = created_at

    def to_dict(self):
        """Return JSON-serializable status fields.

        URLs (statusUrl / translatedFileUrl) are deliberately *not* built here:
        they require a Flask request context and are added by the request
        handlers, never from a background worker thread.
        """
        result = {
            "id": self.id,
            "status": self.status,
            "source": self.source,
            "target": self.target,
            "originalFilename": self.original_filename,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.translated_filename is not None:
            result["translatedFilename"] = self.translated_filename
        if self.error is not None:
            result["error"] = self.error
        return result


class FileTranslationTasks:
    """Thread-safe registry of file-translation tasks backed by a thread pool."""

    def __init__(self, max_workers=DEFAULT_MAX_WORKERS, retention=DEFAULT_RETENTION_SECONDS, time_fn=time.time):
        self._tasks = {}
        self._futures = {}
        self._lock = threading.Lock()
        self._retention = retention
        # Injectable clock so cleanup can be tested deterministically.
        self._time_fn = time_fn
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers))

    def submit(self, source, target, original_filename, job):
        """Register a task and run ``job`` in the background.

        ``job`` is a no-argument callable that performs the translation and
        returns the path to the translated file (or raises on failure).
        Returns the created :class:`FileTranslationTask` immediately.
        """
        now = self._time_fn()
        task = FileTranslationTask(uuid.uuid4().hex, source, target, original_filename, now)

        with self._lock:
            self._tasks[task.id] = task

        future = self._executor.submit(self._run, task, job)

        with self._lock:
            self._futures[task.id] = future

        # Opportunistic pruning: the in-memory registry only grows while tasks
        # are being submitted, so this is enough to bound memory in practice.
        self.cleanup()
        return task

    def _run(self, task, job):
        with self._lock:
            task.status = STATUS_RUNNING
            task.updated_at = self._time_fn()

        try:
            translated_filepath = job()
            with self._lock:
                task.translated_filepath = translated_filepath
                task.translated_filename = (
                    os.path.basename(translated_filepath) if translated_filepath else None
                )
                task.status = STATUS_COMPLETED
                task.updated_at = self._time_fn()
        except Exception as e:  # noqa: BLE001 - any failure is recorded on the task
            # HTTPException (from flask.abort) carries a human-readable description.
            message = getattr(e, "description", None) or str(e) or e.__class__.__name__
            with self._lock:
                task.status = STATUS_FAILED
                task.error = str(message)
                task.updated_at = self._time_fn()

    def get(self, task_id):
        """Return the task for ``task_id`` (pruning expired tasks first), or None."""
        self.cleanup()
        with self._lock:
            return self._tasks.get(task_id)

    def wait(self, task_id, timeout=10):
        """Block until the task's worker has finished. Test helper only."""
        with self._lock:
            future = self._futures.get(task_id)
        if future is not None:
            future.result(timeout=timeout)
        with self._lock:
            return self._tasks.get(task_id)

    def cleanup(self, now=None):
        """Remove tasks older than the retention window.

        Best-effort deletes the translated output file each removed task
        produced, so a result that is never downloaded does not outlive the
        retention window. Returns the list of removed task ids.
        """
        if now is None:
            now = self._time_fn()

        removed = []
        with self._lock:
            expired_ids = [
                task_id
                for task_id, task in self._tasks.items()
                if (now - task.updated_at) > self._retention
            ]
            for task_id in expired_ids:
                removed.append((task_id, self._tasks.pop(task_id, None)))
                self._futures.pop(task_id, None)

        # Touch the filesystem outside the lock.
        for _task_id, task in removed:
            if task is not None and task.translated_filepath:
                try:
                    os.remove(task.translated_filepath)
                except OSError:
                    pass

        return [task_id for task_id, _task in removed]

    def shutdown(self):
        """Stop accepting work and let in-flight jobs finish in the background."""
        self._executor.shutdown(wait=False)
