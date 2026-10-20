"""SEM-ARR: elements of instance arrays keep the array's name in their identity.

slang gives an array ELEMENT an empty name; the extractor used ("", [i]) as the path component, so
two arrays in one scope collided on instance_id and the whole build died with
``UNIQUE constraint failed: instances.build_id, instances.instance_id`` (found on the SSE-315 SoC).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import Ws, profile
from tests.integration.test_cli_semantic import run

pytestmark = pytest.mark.integration

SRC = """\
module leaf_a (input logic i, output logic o);
  assign o = i;
endmodule
module leaf_b (input logic i, output logic o);
  assign o = ~i;
endmodule
module arr_top (input logic [1:0] a, output logic [1:0] y);
  logic [1:0] t;
  leaf_a u_a [1:0] (.i(a), .o(t));
  leaf_b u_b [1:0] (.i(t), .o(y));
  leaf_a u_m [1:0][1:0] (.i(1'b0), .o());
endmodule
"""


def test_SEM_ARR01_two_arrays_in_one_scope_have_distinct_named_instances(tmp_path: Path) -> None:
    w = Ws(tmp_path / "ws")
    w.write("rtl/arr.sv", SRC)
    w.config({"p": profile(source_inputs=[{"kind": "file", "path": "rtl/arr.sv"}], top_modules=["arr_top"])})
    w.policy()
    code, b = run(w.root, "build", "--profile", "p")
    assert code == 0 and b["outcome"] == "OK", b.get("error")  # used to be an internal IntegrityError
    code, h = run(w.root, "query", "hierarchy", "--build", b["build_id"], "--top", "arr_top")
    assert code == 0
    items = h["data"]["items"]
    paths = sorted(i["path"] for i in items)
    assert paths == sorted(
        ["arr_top", "arr_top.u_a[0]", "arr_top.u_a[1]", "arr_top.u_b[0]", "arr_top.u_b[1]"]
        + [f"arr_top.u_m[{r}][{c}]" for r in (0, 1) for c in (0, 1)]
    )
    assert len({i["instance_id"] for i in items}) == len(items)
    for i in items[1:]:  # no empty-name component, and the array name is in the identity
        assert all(c["name"] for c in i["path_components"]), i["path"]
    by = {i["path"]: i["definition"] for i in items}
    assert by["arr_top.u_a[0]"] == "leaf_a" and by["arr_top.u_b[0]"] == "leaf_b"
