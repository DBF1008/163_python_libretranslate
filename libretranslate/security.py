import os


class SuspiciousFileOperationError(Exception):
    pass


def path_traversal_check(unsafe_path, known_safe_path):
    # Use realpath to resolve symlinks, preventing symlink-based escapes
    known_safe_path = os.path.realpath(known_safe_path)
    unsafe_path = os.path.realpath(unsafe_path)

    # Append os.sep to enforce a path-component boundary check.
    # Without this, os.path.commonprefix is character-based and would let
    # a sibling directory with a matching prefix slip through
    # (e.g. /tmp/libretranslate-files-translate-evil passes against
    #       /tmp/libretranslate-files-translate).
    safe_prefix = known_safe_path + os.sep
    if unsafe_path + os.sep != safe_prefix and not unsafe_path.startswith(safe_prefix):
        raise SuspiciousFileOperationError(f"{unsafe_path} is not safe")

    return unsafe_path


def validate_basename(name):
    """Reject any filename that contains path separators, null bytes, or
    other characters that could be used to escape the intended directory."""
    if not name:
        raise SuspiciousFileOperationError("Empty filename")
    if os.sep in name:
        raise SuspiciousFileOperationError(f"{name!r} contains path separator")
    if "/" in name or "\\" in name:
        raise SuspiciousFileOperationError(f"{name!r} contains path separator")
    if "\x00" in name:
        raise SuspiciousFileOperationError(f"{name!r} contains null byte")
