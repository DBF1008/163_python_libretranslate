import os

import pytest

from libretranslate.security import (
    SuspiciousFileOperationError,
    path_traversal_check,
)

# A representative upload directory. path_traversal_check only normalises and
# compares the paths textually (via os.path.abspath), so the directory does not
# need to exist on disk for these tests.
SAFE_DIR = "/tmp/libretranslate-files-translate"


def test_allows_file_directly_inside_safe_dir():
    path = os.path.join(SAFE_DIR, "abc123.report.txt")
    assert path_traversal_check(path, SAFE_DIR) == os.path.abspath(path)


def test_allows_nested_file_inside_safe_dir():
    path = os.path.join(SAFE_DIR, "nested", "abc123.report.txt")
    assert path_traversal_check(path, SAFE_DIR) == os.path.abspath(path)


def test_rejects_sibling_dir_sharing_name_prefix():
    # Regression for the reported defect: the previous os.path.commonprefix
    # check compared character-by-character, so a sibling directory whose name
    # merely starts with the safe directory's name slipped through and allowed
    # reads outside the upload directory.
    evil = "/tmp/libretranslate-files-translate-evil/secret.txt"
    with pytest.raises(SuspiciousFileOperationError):
        path_traversal_check(evil, SAFE_DIR)


def test_rejects_parent_directory_traversal():
    evil = os.path.join(SAFE_DIR, "..", "secret.txt")
    with pytest.raises(SuspiciousFileOperationError):
        path_traversal_check(evil, SAFE_DIR)


def test_rejects_deep_traversal_outside_safe_dir():
    evil = os.path.join(SAFE_DIR, "..", "..", "..", "etc", "passwd")
    with pytest.raises(SuspiciousFileOperationError):
        path_traversal_check(evil, SAFE_DIR)


def test_rejects_absolute_path_outside_safe_dir():
    with pytest.raises(SuspiciousFileOperationError):
        path_traversal_check("/etc/passwd", SAFE_DIR)


def test_safe_dir_is_not_a_prefix_of_unrelated_path():
    # "/tmp/libretranslate-files" is a character prefix of the safe dir but is a
    # different directory; nothing under it should be considered safe.
    evil = "/tmp/libretranslate-files/secret.txt"
    with pytest.raises(SuspiciousFileOperationError):
        path_traversal_check(evil, SAFE_DIR)


def test_returns_normalised_absolute_path_for_safe_input():
    # A path containing a harmless ".." that still resolves inside the safe dir
    # is allowed and returned in normalised form.
    path = os.path.join(SAFE_DIR, "nested", "..", "abc123.report.txt")
    result = path_traversal_check(path, SAFE_DIR)
    assert result == os.path.join(os.path.abspath(SAFE_DIR), "abc123.report.txt")
