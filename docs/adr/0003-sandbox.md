# ADR 0003: Sandbox behind a port
Status: accepted. Docker (`--network none`, read-only, memory/pids limits) when available; otherwise subprocess with rlimits plus a static policy. The subprocess sandbox is defence in depth, not a security boundary; run untrusted real-model matches with Docker.
