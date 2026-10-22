"""In-sandbox runner shared by every sandbox.

The flag and source travel on stdin as a one-line JSON header, so they never
appear on any argv or in any process environment (``/proc/<pid>/environ``).
The runner exposes the flag to challenge code as the global ``FLAG`` and hands
the rest of stdin to the challenge as ``sys.stdin``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

RUNNER = (
    "import io, json, resource, sys\n"
    "_h = json.loads(sys.stdin.readline())\n"
    "for _k, _v in _h.pop('rlimits').items():\n"
    "    resource.setrlimit(getattr(resource, _k), (_v, _v))\n"
    "sys.stdin = io.StringIO(sys.stdin.read())\n"
    "_g = {'__name__': '__main__', '__builtins__': __builtins__, 'FLAG': _h.pop('flag')}\n"
    "_c = compile(_h.pop('source'), '<challenge>', 'exec')\n"
    "del _h\n"
    "exec(_c, _g)\n"
)
PYTHON_FLAGS = ("-I", "-S", "-c", RUNNER)


def frame(source: str, flag: str, stdin: str, rlimits: Mapping[str, int] | None = None) -> str:
    """Encode the runner's stdin: JSON header line, then the challenge input.

    ``rlimits`` (e.g. ``{"RLIMIT_NPROC": 1}``) are applied by the runner before the challenge is compiled.
    """
    header = {"flag": flag, "source": source, "rlimits": dict(rlimits or {})}
    return json.dumps(header) + "\n" + stdin
