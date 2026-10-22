"""In-sandbox runner shared by every sandbox.

The flag and source travel on stdin as a one-line JSON header, so they never
appear on any argv or in any process environment (``/proc/<pid>/environ``).
The runner exposes the flag to challenge code as the global ``FLAG`` and hands
the rest of stdin to the challenge as ``sys.stdin``. Defence in depth behind the
AST policy: challenge code gets only ``SAFE_BUILTINS``, an ``__import__`` limited to
the policy's pure modules, and module stand-ins without submodules or private names, and a ``sys`` stand-in
exposing only ``stdin``/``stdout``/``exit``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from claude_duel.domain.policy import PURE_MODULES, SAFE_BUILTINS

RUNNER = (
    "import builtins, io, json, resource, sys, types\n"
    "_h = json.loads(sys.stdin.readline())\n"
    "for _k, _v in _h.pop('rlimits').items():\n"
    "    resource.setrlimit(getattr(resource, _k), (_v, _v))\n"
    "def _proxy(m):\n"
    "    return types.SimpleNamespace(**{k: v for k, v in vars(m).items()\n"
    "                                    if not k.startswith('_') and not isinstance(v, types.ModuleType)})\n"
    "_mods = {_m: _proxy(__import__(_m)) for _m in _h.pop('imports')}\n"
    "_mods['sys'] = types.SimpleNamespace(stdin=io.StringIO(sys.stdin.read()), stdout=sys.stdout, exit=sys.exit)\n"
    "def _imp(name, globals=None, locals=None, fromlist=(), level=0):\n"
    "    if level or name not in _mods:\n"
    "        raise ImportError(name)\n"
    "    return _mods[name]\n"
    "_b = {_n: getattr(builtins, _n) for _n in _h.pop('builtins')}\n"
    "_b['__import__'] = _imp\n"
    "_g = {'__name__': '__main__', '__builtins__': _b, 'FLAG': _h.pop('flag')}\n"
    "_c = compile(_h.pop('source'), '<challenge>', 'exec')\n"
    "del _h\n"
    "try:\n"
    "    exec(_c, _g)\n"
    "except Exception as _e:\n"  # type and line only: a message could quote FLAG
    "    _t = _e.__traceback__\n"
    "    while _t.tb_next:\n"
    "        _t = _t.tb_next\n"
    "    print(f'error: {type(_e).__name__} at line {_t.tb_lineno}')\n"
    "    sys.exit(1)\n"
)
PYTHON_FLAGS = ("-I", "-S", "-c", RUNNER)


def frame(source: str, flag: str, stdin: str, rlimits: Mapping[str, int] | None = None) -> str:
    """Encode the runner's stdin: JSON header line, then the challenge input.

    ``rlimits`` (e.g. ``{"RLIMIT_NPROC": 1}``) are applied by the runner before the challenge is compiled.
    """
    header = {
        "flag": flag,
        "source": source,
        "rlimits": dict(rlimits or {}),
        "imports": sorted(PURE_MODULES),
        "builtins": sorted(SAFE_BUILTINS),
    }
    return json.dumps(header) + "\n" + stdin
