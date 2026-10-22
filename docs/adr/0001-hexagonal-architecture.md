# ADR 0001: Hexagonal architecture
Status: accepted. The domain core is pure and tested without I/O; providers, sandboxes and stores are adapters behind Protocols so a deterministic fake can drive full matches in tests.
