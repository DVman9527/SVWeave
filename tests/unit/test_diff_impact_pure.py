"""DIF-U: parsing git's -U0 output and mapping hunks to byte ranges (no git, no build)."""

from __future__ import annotations

import pytest

from svweave.application.diff_impact import _innermost, _ranges, check_ref, parse_diff
from svweave.domain.errors import SvwError

DIFF = """\
diff --git a/rtl/a.sv b/rtl/a.sv
index 111..222 100644
--- a/rtl/a.sv
+++ b/rtl/a.sv
@@ -3,2 +3,3 @@ module a;
-old1
-old2
+new1
+new2
+new3
@@ -10 +11,0 @@
-gone
diff --git a/rtl/new.sv b/rtl/new.sv
new file mode 100644
--- /dev/null
+++ b/rtl/new.sv
@@ -0,0 +1,2 @@
+x
+y
diff --git a/rtl/del.sv b/rtl/del.sv
deleted file mode 100644
--- a/rtl/del.sv
+++ /dev/null
@@ -1,4 +0,0 @@
-a
diff --git a/dir with space/s.sv b/dir with space/s.sv
--- a/dir with space/s.sv\t
+++ b/dir with space/s.sv\t
@@ -5 +5 @@
-p
+q
Binary files a/img.png and b/img.png differ
"""


def test_DIF_U01_parse_diff_covers_modify_add_delete_space_and_binary() -> None:
    fds = parse_diff(DIFF.replace("\\t", "\t"))
    by = {f.path: f for f in fds}
    a = by["rtl/a.sv"]
    assert a.hunks == [(3, 2), (10, 1)] and a.added == 3 and a.removed == 3  # +3,3 and +11,0
    assert by["rtl/new.sv"].old is None and by["rtl/new.sv"].hunks == [(0, 0)]
    d = by["rtl/del.sv"]
    assert d.new is None and d.hunks == [(1, 4)]
    assert by["dir with space/s.sv"].hunks == [(5, 1)]  # the tab git appends is stripped
    assert by["img.png"].binary and by["img.png"].hunks == []


def test_DIF_U02_ranges_map_old_lines_to_bytes_and_anchor_insertions() -> None:
    data = b"l1\nl2\nl3\nl4\n"  # line starts 0,3,6,9
    assert _ranges([(2, 2)], data) == [(3, 9, 2, 3)]  # lines 2-3
    assert _ranges([(1, 1)], data) == [(0, 3, 1, 1)]
    assert _ranges([(2, 0)], data) == [(6, 7, 3, 3)]  # insertion after line 2 anchors at line 3
    assert _ranges([(4, 0)], data) == [(9, 10, 4, 4)]  # after the last line: clamp to it
    assert _ranges([(0, 0)], data) == [(0, 1, 1, 1)]  # a new/empty-side hunk
    assert _ranges([(3, 99)], data) == [(6, 12, 3, 4)]  # a range past EOF is clamped
    assert _ranges([(1, 1)], b"no newline") == [(0, 10, 1, 1)]  # last line without \n


class R(dict[str, object]):
    pass


def _r(did: str, lo: int, hi: int) -> R:
    return R(declaration_id=did, span_start=lo, span_end=hi, full_start=lo, full_end=hi)


def test_DIF_U03_innermost_declarations_only() -> None:
    mod, fn, var = _r("mod", 0, 100), _r("fn", 10, 40), _r("var", 12, 20)
    got = _innermost([mod, fn, var])  # type: ignore[list-item]
    assert [r["declaration_id"] for r in got] == ["var"]  # the module and the function contain it
    twin_a, twin_b = _r("a", 5, 9), _r("b", 5, 9)  # same span: neither contains the other
    assert {r["declaration_id"] for r in _innermost([mod, twin_a, twin_b])} == {"a", "b"}  # type: ignore[list-item]


@pytest.mark.parametrize(
    "bad", ["--output=x", "-p", "a..b", "a b", "a\nb", "", "x" * 201, ";rm", "$(id)", "a`b`"]
)
def test_DIF_U04_refs_are_plain_tokens_never_options(bad: str) -> None:
    with pytest.raises(SvwError) as e:
        check_ref(bad, "base")
    assert e.value.code.value == "INVALID_CONFIG"


@pytest.mark.parametrize("good", ["HEAD", "HEAD~3", "main", "origin/main", "v1.2.3", "abc123", "HEAD^", "a@b"])
def test_DIF_U05_ordinary_revisions_pass(good: str) -> None:
    assert check_ref(good, "base") == good
