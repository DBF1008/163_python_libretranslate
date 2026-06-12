import os


class SuspiciousFileOperationError(Exception):
    pass


def path_traversal_check(unsafe_path, known_safe_path):
    known_safe_path = os.path.abspath(known_safe_path)
    unsafe_path = os.path.abspath(unsafe_path)

    # Compare whole path components, not characters. os.path.commonprefix works
    # character-by-character, so a sibling directory whose name merely shares a
    # textual prefix (e.g. ".../foo" vs ".../foo-evil") would slip through and
    # allow reads outside the safe directory. os.path.commonpath compares path
    # components, so such siblings are correctly rejected.
    try:
        common_path = os.path.commonpath([known_safe_path, unsafe_path])
    except ValueError:
        # Raised when the paths cannot share a base (e.g. different drives on
        # Windows). Treat that as an escape attempt.
        raise SuspiciousFileOperationError(f"{unsafe_path} is not safe")

    if common_path != known_safe_path:
        raise SuspiciousFileOperationError(f"{unsafe_path} is not safe")

    # Passes the check
    return unsafe_path
