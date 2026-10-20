"""GRF-U: the static graph page embeds source-derived text safely and fetches nothing."""

from __future__ import annotations

import json
import re

from svweave.application.graph_template import dumps_safe, render_html

HOSTILE = '</script><script>alert(1)</script><!-- &     "quote" \\'


def test_GRF_U01_dumps_safe_cannot_close_the_script_element() -> None:
    out = dumps_safe({"path": HOSTILE})
    for bad in ("<", ">", "&", " ", " "):
        assert bad not in out
    assert json.loads(out)["path"] == HOSTILE  # lossless after the escapes


def test_GRF_U02_page_has_one_data_block_a_strict_csp_and_no_network_or_html_sinks() -> None:
    page = render_html({"nodes": [{"id": "x", "path": HOSTILE}], "edges": [], "hubs": [], "meta": {}})
    assert page.count('<script id="data" type="application/json">') == 1
    assert page.count("</script>") == 2  # the data block and the viewer script, nothing injected
    assert page.count("<script") == 2 and "<script>alert" not in page  # hostile text stays escaped JSON
    assert "default-src 'none'" in page and "connect-src" not in page
    script = page.split('<script>')[1]
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "fetch(",
                 "XMLHttpRequest", "WebSocket", "import("):
        assert sink not in script, sink
    assert not re.search(r'(?:src|href)\s*=\s*["\']?(?:https?:)?//', page)  # no external resource
    assert "@@SVWEAVE_GRAPH_DATA@@" not in page
