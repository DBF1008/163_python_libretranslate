import json
import logging
import os
import threading
import time
import traceback
import uuid

log = logging.getLogger(__name__)

TASK_KEY_PREFIX = "task:"
TASK_TTL = 1800  # 30 minutes, aligned with file cleanup

_task_semaphore = None
_tasks_lock = threading.Lock()


class TaskStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


def setup(max_concurrent_tasks=4):
    """Initialize the task engine semaphore. Called once during app startup."""
    global _task_semaphore
    _task_semaphore = threading.Semaphore(max_concurrent_tasks)


def _get_storage():
    from libretranslate.storage import get_storage
    return get_storage()


def _task_key(task_id):
    return TASK_KEY_PREFIX + task_id


def create_task(source_lang, target_lang, filename, req_cost=1, api_key=None, client_ip=""):
    """Create a new async translation task record in shared storage.

    Returns the task dict with a generated task_id and status=pending.
    """
    task_id = str(uuid.uuid4())
    task = {
        "task_id": task_id,
        "status": TaskStatus.PENDING,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "filename": filename,
        "req_cost": req_cost,
        "api_key": api_key,
        "client_ip": client_ip,
        "error": None,
        "translated_filename": None,
        "created": time.time(),
    }
    store = _get_storage()
    store.set_str(_task_key(task_id), json.dumps(task), ex=TASK_TTL)
    return task


def get_task(task_id):
    """Retrieve a task from storage. Returns None if expired or not found."""
    store = _get_storage()
    raw = store.get_str(_task_key(task_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def update_task(task_id, **fields):
    """Read-modify-write a task record in storage.

    Only updates the specified fields. Preserves TTL by re-setting with the
    original expiration window.
    """
    with _tasks_lock:
        store = _get_storage()
        key = _task_key(task_id)
        raw = store.get_str(key)
        if not raw:
            return None
        try:
            task = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        task.update(fields)
        # Re-set with full TTL to keep the task alive while it's being worked on
        store.set_str(key, json.dumps(task), ex=TASK_TTL)
        return task


def submit_translation(task_id, translator, filepath, upload_dir):
    """Spawn a daemon thread to perform the file translation asynchronously."""
    t = threading.Thread(
        target=_do_translation,
        args=(task_id, translator, filepath, upload_dir),
        daemon=True,
        name=f"file-translate-{task_id[:8]}",
    )
    t.start()
    return t


def _do_translation(task_id, translator, filepath, upload_dir):
    """Thread target: acquire semaphore, translate, update task status."""
    try:
        _task_semaphore.acquire()
        try:
            update_task(task_id, status=TaskStatus.PROCESSING)

            import argostranslatefiles
            translated_file_path = argostranslatefiles.translate_file(
                translator, filepath
            )
            translated_filename = os.path.basename(translated_file_path)

            update_task(
                task_id,
                status=TaskStatus.COMPLETED,
                translated_filename=translated_filename,
            )
        except Exception as e:
            log.error("Async translation %s failed: %s", task_id, traceback.format_exc())
            update_task(
                task_id,
                status=TaskStatus.FAILED,
                error=str(e),
            )
        finally:
            _task_semaphore.release()
    except Exception as e:
        # Last resort: if even the semaphore or update fails
        log.error("Fatal error in async translation thread %s: %s", task_id, traceback.format_exc())
        try:
            update_task(task_id, status=TaskStatus.FAILED, error=str(e))
        except Exception:
            pass


def cleanup_stale_tasks():
    """Mark tasks stuck in 'processing' for too long as 'failed'.

    Called periodically by the scheduler. Scans storage for task keys and
    checks if any have been in 'processing' state for longer than half the TTL.
    """
    store = _get_storage()
    stale_threshold = time.time() - (TASK_TTL / 2)  # 15 minutes

    # For MemoryStorage, we need to iterate keys. For RedisStorage, we'd use SCAN.
    # We use a compatible approach for both.
    if hasattr(store, 'store'):
        # MemoryStorage: iterate the internal dict
        keys = [k for k in store.store.keys() if k.startswith(TASK_KEY_PREFIX)]
    elif hasattr(store, 'conn'):
        # RedisStorage: use SCAN with pattern
        keys = []
        cursor = 0
        while True:
            cursor, batch = store.conn.scan(cursor, match=TASK_KEY_PREFIX + "*", count=100)
            keys.extend([k.decode('utf-8') if isinstance(k, bytes) else k for k in batch])
            if cursor == 0:
                break
    else:
        return

    for key in keys:
        try:
            if hasattr(store, 'store'):
                entry = store.store.get(key)
                if entry is None:
                    continue
                # MemoryStorage stores {'value': ..., 'ex': ...}
                raw = entry.get('value', '') if isinstance(entry, dict) and 'value' in entry else entry
            else:
                raw = store.get_str(key)

            if not raw:
                continue
            task = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode('utf-8'))
            if task.get("status") == TaskStatus.PROCESSING and task.get("created", 0) < stale_threshold:
                update_task(
                    task["task_id"],
                    status=TaskStatus.FAILED,
                    error="Translation timed out or worker crashed",
                )
                log.warning("Marked stale task %s as failed", task["task_id"])
        except Exception:
            log.debug("Error checking task key %s: %s", key, traceback.format_exc())
