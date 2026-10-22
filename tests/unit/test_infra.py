from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from claude_duel.adapters.sandbox.docker_sandbox import DockerSandbox
from claude_duel.adapters.sandbox.subprocess_sandbox import SubprocessSandbox
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.app import agents
from claude_duel.config import load_settings
from claude_duel.domain.errors import ConfigError, MalformedOutputError, MatchLockedError, MatchNotFoundError
from claude_duel.domain.flags import derive_flag
from claude_duel.domain.models import Rules, SandboxLimits
from claude_duel.observability import JsonFormatter, NullMetrics, log, trace

LIM = SandboxLimits(wall_seconds=2, cpu_seconds=1, memory_mb=128, max_output_bytes=64)
SB = SubprocessSandbox()


class TestSandbox:
    def test_flag_via_env(self) -> None:
        r = SB.run('import os\nprint(os.environ["FLAG"])', "", {"FLAG": "F"}, LIM)
        assert r.stdout.strip() == "F"
        assert r.exit_code == 0

    def test_env_is_clean(self) -> None:
        r = SB.run("import os\nprint(sorted(os.environ))", "", {}, LIM)
        assert "OPENAI_API_KEY" not in r.stdout

    def test_infinite_loop_killed(self) -> None:
        r = SB.run("while True:\n    pass\n", "", {}, LIM)
        assert r.timed_out or r.killed_reason == "signal"

    def test_memory_bomb_contained(self) -> None:
        r = SB.run("x = bytearray(1024 * 1024 * 1024)\nprint('alive')", "", {}, LIM)
        assert "alive" not in r.stdout
        assert r.exit_code != 0

    def test_output_truncated(self) -> None:
        assert len(SB.run("print('a' * 10000)", "", {}, LIM).stdout) == 64

    def test_cannot_write_files(self) -> None:
        r = SB.run("open('x', 'w').write('hi')", "", {}, LIM)
        assert r.exit_code != 0

    def test_docker_argv_hardened(self) -> None:
        argv = DockerSandbox().argv("print(1)", {"FLAG": "F"}, LIM)
        for flag in ("--network", "none", "--read-only", "--cap-drop", "--pids-limit"):
            assert flag in argv


class TestStore:
    def test_roundtrip_and_lock(self, tmp_path: Path, new_state) -> None:  # type: ignore[no-untyped-def]
        store = FileMatchStore(tmp_path)
        s = new_state()
        store.save(s)
        assert store.load("m1") == s
        with store.lock("m1"), pytest.raises(MatchLockedError), store.lock("m1"):
            pass
        with store.lock("m1"):
            pass

    def test_missing_and_bad_id(self, tmp_path: Path) -> None:
        store = FileMatchStore(tmp_path)
        with pytest.raises(MatchNotFoundError):
            store.load("nope")
        with pytest.raises(MatchNotFoundError):
            store.load("../etc/passwd")


class TestParsing:
    def test_parses_fenced_json(self) -> None:
        assert agents.parse_attack('blah ```json\n{"payload": "x"}\n```').payload == "x"

    @pytest.mark.parametrize("text", ["no json", "{bad json}", '{"title": 1}'])
    def test_malformed(self, text: str) -> None:
        with pytest.raises(MalformedOutputError):
            agents.parse_challenge(text)

    def test_build_prompt_includes_error(self) -> None:

        assert "rejected: boom" in agents.build_prompt("a", 1, 0, Rules(), "boom")


class TestConfig:
    def test_file_and_env(self, tmp_path: Path) -> None:
        cfg = tmp_path / "c.toml"
        cfg.write_text('sandbox = "subprocess"\n[rules]\nrounds = 5\n')
        s = load_settings(cfg, {"DUEL_MAX_ATTEMPTS": "7", "OPENAI_API_KEY": "sk-secret-value-123"})
        assert (s.sandbox, s.rules.rounds, s.rules.max_attempts) == ("subprocess", 5, 7)
        assert "sk-secret" not in repr(s)

    def test_errors(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load_settings(tmp_path / "missing.toml", {})
        with pytest.raises(ConfigError):
            load_settings(None, {"DUEL_ROUNDS": "0"})


class TestLogging:
    def test_redacts_secrets_and_flags(self) -> None:
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setFormatter(JsonFormatter())
        lg = logging.getLogger("t.redact")
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        flag = derive_flag("k", "c")
        with trace("abc123"):
            log(lg, logging.INFO, "x", key="sk-abcdefghijklmnop", flag=flag)
            try:
                raise ValueError("boom")
            except ValueError:
                lg.exception("err")
        out = buf.getvalue()
        assert "sk-abcdefghijklmnop" not in out
        assert flag not in out
        assert "abc123" in out
        assert "boom" in out
        NullMetrics().incr("x")
        NullMetrics().timing("x", 1.0)
