"""HDL-CI-05: real mandatory probe boundary, never a mock or degraded launch.

These checks require a working bwrap host. Missing isolation is FAIL in required mode via the
shared sandbox fixture. They do not run HDL analysis or contact any external network service.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from svweave.domain.errors import ErrorCode, SvwError
from svweave.security.sandbox import SandboxRunner

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/spikes"))
import _sandbox as boundary  # noqa: E402

pytestmark = pytest.mark.integration


def test_HDL_CI_05_readonly_inputs_private_network_and_no_secret(
    tmp_path: Path, sandbox: SandboxRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, state = tmp_path / "source", tmp_path / "state"
    source.mkdir()
    state.mkdir()
    src = source / "fixture.sv"
    src.write_text("module fixture; endmodule\n")
    original = src.read_bytes()
    monkeypatch.setenv("SVW_TEST_SECRET", "do-not-inherit")
    spec = boundary.python_spec(sources=(source,), writable=(state,), cwd=source)
    code = """
import json,os,sys
from pathlib import Path
try:
    Path(sys.argv[1]).write_text('must not write')
    denied = False
except OSError:
    denied = True
Path(sys.argv[2]).write_text('scoped output')
print(json.dumps({'readonly': denied, 'netns': os.readlink('/proc/self/ns/net'),
                  'secret': os.environ.get('SVW_TEST_SECRET')}))
"""
    result = boundary.run(spec, [sys.executable, "-I", "-c", code, str(src), str(state / "ok")])
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["readonly"] is True
    assert evidence["secret"] is None
    assert evidence["netns"] != os.readlink("/proc/self/ns/net")
    assert src.read_bytes() == original
    assert (state / "ok").read_text() == "scoped output"


def _live_token_processes(token: str) -> list[int]:
    result = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if token.encode() in path.read_bytes():
                result.append(int(path.parent.name))
        except (OSError, ValueError):
            continue
    return result


@pytest.mark.parametrize("failure", ["timeout", "stdout", "stderr"])
def test_HDL_CI_05_failure_reaps_detached_descendant(
    tmp_path: Path, sandbox: SandboxRunner, failure: str,
) -> None:
    source, state = tmp_path / "source", tmp_path / "state"
    source.mkdir()
    state.mkdir()
    original = source / "fixture.sv"
    original.write_text("module fixture; endmodule\n")
    token = "svw-probe-child-" + uuid.uuid4().hex
    ready = state / "ready"
    spec = boundary.python_spec(sources=(source,), writable=(state,), cwd=source)
    code = """
import subprocess,sys,time
from pathlib import Path
child = "from pathlib import Path;import sys,time;Path(sys.argv[1]).write_text('ready');time.sleep(60)"
subprocess.Popen([sys.executable,'-I','-c',child,sys.argv[1],sys.argv[2]], start_new_session=True)
while not Path(sys.argv[1]).exists():
    time.sleep(.01)
if sys.argv[3] == 'timeout':
    time.sleep(60)
else:
    stream = sys.stdout if sys.argv[3] == 'stdout' else sys.stderr
    while True:
        stream.write('x'*65536)
        stream.flush()
"""
    argv = [sys.executable, "-I", "-c", code, str(ready), token, failure]
    if failure == "timeout":
        with pytest.raises(subprocess.TimeoutExpired):
            boundary.run(spec, argv, timeout=3)
    else:
        with pytest.raises(SvwError) as error:
            boundary.run(spec, argv, timeout=10, max_stdout_bytes=128 * 1024, max_stderr_bytes=128 * 1024)
        assert error.value.code == ErrorCode.OUTPUT_LIMIT
    assert ready.read_text() == "ready", "the detached child must have started before termination"
    deadline = time.monotonic() + 5
    while _live_token_processes(token) and time.monotonic() < deadline:
        time.sleep(.05)
    assert _live_token_processes(token) == []
    assert original.read_text() == "module fixture; endmodule\n"
