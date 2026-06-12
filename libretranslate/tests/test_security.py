import os
import tempfile

import pytest

from libretranslate.security import (
    SuspiciousFileOperationError,
    path_traversal_check,
    validate_basename,
)


# ---------------------------------------------------------------------------
# path_traversal_check
# ---------------------------------------------------------------------------

class TestPathTraversalCheck:
    @pytest.fixture()
    def safe_dir(self, tmp_path):
        d = tmp_path / "uploads"
        d.mkdir()
        return str(d)

    # ---- legitimate paths ----

    def test_file_inside_dir(self, safe_dir):
        fp = os.path.join(safe_dir, "file.txt")
        open(fp, "w").close()
        assert path_traversal_check(fp, safe_dir) == os.path.realpath(fp)

    def test_nested_subdir(self, safe_dir):
        sub = os.path.join(safe_dir, "sub")
        os.makedirs(sub, exist_ok=True)
        fp = os.path.join(sub, "file.txt")
        open(fp, "w").close()
        assert path_traversal_check(fp, safe_dir) == os.path.realpath(fp)

    # ---- dot-dot escape ----

    def test_dotdot_escape(self, safe_dir):
        with pytest.raises(SuspiciousFileOperationError):
            path_traversal_check(os.path.join(safe_dir, "../evil.txt"), safe_dir)

    def test_deep_dotdot_escape(self, safe_dir):
        # 5 levels of ".." from a/b/c goes: c→b→a→safe_dir→parent(escape)
        deep = os.path.join(safe_dir, "a", "b", "c", "..", "..", "..", "..", "..", "evil.txt")
        with pytest.raises(SuspiciousFileOperationError):
            path_traversal_check(deep, safe_dir)

    # ---- absolute path outside ----

    def test_absolute_path_outside(self, safe_dir):
        with pytest.raises(SuspiciousFileOperationError):
            path_traversal_check("/etc/passwd", safe_dir)

    # ---- symlink escape ----

    @pytest.mark.skipif(os.name == "nt", reason="symlinks may need admin on Windows")
    def test_symlink_escape(self, safe_dir):
        target = os.path.join(tempfile.gettempdir(), "secret_target.txt")
        with open(target, "w") as f:
            f.write("secret")
        link = os.path.join(safe_dir, "link.txt")
        try:
            os.symlink(target, link)
            with pytest.raises(SuspiciousFileOperationError):
                path_traversal_check(link, safe_dir)
        finally:
            os.unlink(link)
            if os.path.exists(target):
                os.unlink(target)

    # ---- sibling directory with matching prefix (old commonprefix bug) ----

    def test_sibling_dir_with_same_prefix(self, safe_dir):
        sibling = safe_dir + "-evil"
        os.makedirs(sibling, exist_ok=True)
        evil = os.path.join(sibling, "file.txt")
        with open(evil, "w") as f:
            f.write("evil")
        with pytest.raises(SuspiciousFileOperationError):
            path_traversal_check(evil, safe_dir)

    def test_sibling_dir_shorter_name(self, safe_dir):
        """A directory whose name is a prefix of the safe dir must also fail."""
        parent = os.path.dirname(safe_dir)
        short_name = os.path.basename(safe_dir)[:-2]  # chop last 2 chars
        if not short_name:
            pytest.skip("safe_dir name too short for this test")
        sibling = os.path.join(parent, short_name)
        os.makedirs(sibling, exist_ok=True)
        evil = os.path.join(sibling, "file.txt")
        with open(evil, "w") as f:
            f.write("evil")
        with pytest.raises(SuspiciousFileOperationError):
            path_traversal_check(evil, safe_dir)


# ---------------------------------------------------------------------------
# validate_basename
# ---------------------------------------------------------------------------

class TestValidateBasename:
    def test_normal_filename(self):
        validate_basename("abc123.test.txt")

    def test_uuid_prefixed_filename(self):
        validate_basename("a1b2c3d4-e5f6-7890-abcd-ef1234567890.document.pdf")

    @pytest.mark.parametrize(
        "bad_name",
        [
            "../etc/passwd",
            "subdir/file.txt",
            "..\\file.txt",
            "dir\\file.txt",
            "",
            "file\x00.txt",
        ],
    )
    def test_rejects_dangerous_names(self, bad_name):
        with pytest.raises(SuspiciousFileOperationError):
            validate_basename(bad_name)
