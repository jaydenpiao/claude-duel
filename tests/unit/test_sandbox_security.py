"""Regression tests for the security review: each reproduces an exploit and asserts it now fails safe."""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_duel.adapters.providers.fake import TEMPLATES, is_template
from claude_duel.adapters.sandbox.common import execute
from claude_duel.adapters.sandbox.runner import frame
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.domain import policy
from claude_duel.domain.errors import SandboxRefusedError
from claude_duel.domain.models import ChallengeSpec, SandboxLimits
from tests.helpers import any_source

FLAG = "DUEL{0123456789abcdef0123456789abcdef}"
LIM = SandboxLimits(wall_seconds=2, cpu_seconds=1, memory_mb=128, max_output_bytes=256)


def spec(source: str) -> ChallengeSpec:
    return ChallengeSpec(title="t", description="d", source=source, reference_payload="hidden-input", difficulty=1)


BYPASSES = [
    "from os import system\nsystem('id')\n",  # C1
    "from os import *\nsystem('id')\n",  # C1
    "from os import fork\nfork()\n",
    "from os import environ\nprint(environ)\n",
    "import os\nos.execvp('sh', ['sh'])\n",
    "import os\nprint(os.listdir('/'))\n",  # L2
    "import sys\nprint(sys.modules)\n",  # L2
    "import sys as s\nprint(s.stdin)\n",
    "import sys\ns = sys\nprint(s.modules)\n",
    "from sys import modules\n",
    "b = __builtins__\nb.eval('1')\n",  # L3
    "print(__loader__)\n",
    "x = getattr(1, 'real')\n",
    "print('{0.__class__}'.format(1))\n",
    "from . import x\n",
    "import os.path\n",
    "x = ().__class__\n",
    "type(1)\n",
    # round 2, N1: frame/code introspection without underscores, dunder string keys
    "def g():\n    yield 1\nx = g()\nos = x.gi_frame.f_builtins['__import__']('os')\n",
    "def g():\n    yield 1\nprint(g().gi_frame.f_back)\n",
    "def g():\n    yield 1\nprint(g().gi_code.co_consts)\n",
    "async def c():\n    pass\nprint(c().cr_frame.f_globals)\n",
    "try:\n    1 / 0\nexcept ZeroDivisionError as e:\n    print(e.tb_frame)\n",
    "print(str.mro())\n",
    "d = {}\nprint(d['__builtins__'])\n",
    "print(b'__import__')\n",
    # round 2, arch #1: attribute chains into submodules
    "import re\nre.enum.sys.modules['os'].system('id')\n",
    "import json\njson.codecs.open('/etc/passwd')\n",
    "from json import codecs\n",
    "import re\nr = re\n",
]


@pytest.mark.parametrize("source", BYPASSES)
def test_policy_rejects_bypass(source: str) -> None:
    assert policy.check(spec(source), 4096) is not None


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.key)
def test_policy_accepts_fake_templates(template: object) -> None:
    assert policy.check(spec(template.source), 4096) is None  # type: ignore[attr-defined]


def test_policy_accepts_sys_stdin_forms() -> None:
    assert policy.check(spec("from sys import stdin\nimport sys\nsys.stdout.write(stdin.read())\n"), 4096) is None


class TestSubprocess:
    sb = SubprocessSandbox(any_source)

    def test_flag_not_in_proc_environ(self) -> None:  # H2: scan every visible process while a challenge runs
        seen: list[str] = []
        done = threading.Event()

        def scan() -> None:
            while not done.is_set():
                for env in Path("/proc").glob("[0-9]*/environ"):
                    try:
                        seen.append(env.read_bytes().decode(errors="replace"))
                    except OSError:
                        continue

        t = threading.Thread(target=scan)
        t.start()
        self.sb.run("x = 0\nfor i in range(3000000):\n    x += i\n", "", FLAG, LIM)
        done.set()
        t.join()
        assert seen
        assert not any(FLAG in env for env in seen)

    def test_refuses_sources_not_allowed(self) -> None:  # round 2: no model code outside docker
        with pytest.raises(SandboxRefusedError):
            SubprocessSandbox(is_template).run("print(FLAG)", "", FLAG, LIM)
        t = TEMPLATES[0]
        assert FLAG in SubprocessSandbox(is_template).run(t.source, t.payload, FLAG, LIM).stdout

    @pytest.mark.parametrize(
        "source",
        [
            "print(open)",
            "import os",
            "import re\nprint(re.enum)",
            "import json\nprint(json.codecs)",
            "import sys\nprint(sys.modules)",
            "def g():\n    yield 1\ng().gi_frame.f_builtins['__import__']('os')",  # N1 exploit, policy bypassed
        ],
    )
    def test_runtime_refuses_what_policy_bans(self, source: str) -> None:  # N1 defence in depth
        r = self.sb.run(source + "\n", "", FLAG, LIM)
        assert r.exit_code != 0
        assert re.fullmatch(r"error: (NameError|ImportError|AttributeError) at line \d+\n", r.stdout)

    def test_crash_report_never_quotes_the_message(self) -> None:  # int(FLAG) would put FLAG in the message
        r = self.sb.run("x = int(FLAG)\n", "", FLAG, LIM)
        assert (r.exit_code, r.stdout) == (1, "error: ValueError at line 1\n")

    def test_runtime_builtins_are_minimal(self) -> None:
        src = (
            "def g():\n    yield 1\nb = g().gi_frame.f_builtins\nprint(sorted(set(b) - set("
            + repr(sorted(policy.SAFE_BUILTINS))
            + ")))\n"
        )
        assert self.sb.run(src, "", FLAG, LIM).stdout.strip() == "['__import__']"

    def test_flag_not_on_cmdline(self) -> None:  # H1
        r = self.sb.run("print(open('/proc/self/cmdline').read())", "", FLAG, LIM)
        assert FLAG not in r.stdout

    def test_fork_is_refused(self) -> None:  # H4
        r = self.sb.run("import os\nos.fork()\nprint('forked')", "", FLAG, LIM)
        assert "forked" not in r.stdout
        assert r.exit_code != 0

    def test_runaway_output_is_killed(self) -> None:  # L1
        started = time.monotonic()
        r = self.sb.run("while True:\n    print('a' * 1000)", "", FLAG, LIM)
        assert r.killed_reason == "output"
        assert len(r.stdout) == LIM.max_output_bytes
        assert time.monotonic() - started < LIM.wall_seconds

    def test_stdin_reaches_challenge(self) -> None:
        r = self.sb.run("import sys\nif sys.stdin.read() == 'go':\n    print(FLAG)", "go", FLAG, LIM)
        assert r.stdout.strip() == FLAG


def test_timeout_kills_whole_process_group(tmp_path: Path) -> None:  # H3
    marker = tmp_path / "orphan"
    script = f"(sleep 1; touch {marker}) & sleep 30"
    r = execute(["/bin/sh", "-c", script], "", LIM, env={"PATH": "/usr/bin:/bin"}, wall_seconds=0.3)
    assert r.timed_out
    assert r.killed_reason == "wall"
    time.sleep(1.5)
    assert not marker.exists()


def test_children_killed_after_normal_exit(tmp_path: Path) -> None:  # H3
    marker = tmp_path / "orphan"
    r = execute(["/bin/sh", "-c", f"(sleep 1; touch {marker}) &"], "", LIM, env={"PATH": "/usr/bin:/bin"})
    assert not r.timed_out
    time.sleep(1.5)
    assert not marker.exists()


def test_cleanup_always_runs() -> None:
    calls: list[str] = []
    execute([sys.executable, "-c", "pass"], "", LIM, env={}, cleanup=lambda: calls.append("x"))
    assert calls == ["x"]


def test_frame_header_round_trips() -> None:
    head, _, rest = frame("print(1)", FLAG, "in\nput", {"RLIMIT_NPROC": 1}).partition("\n")
    assert FLAG in head
    assert rest == "in\nput"
