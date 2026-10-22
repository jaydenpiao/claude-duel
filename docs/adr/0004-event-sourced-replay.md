# ADR 0004: Event log + checkpointed state
Status: accepted. State is checkpointed atomically per unit for resume; an append-only event log allows deterministic replay and audit of scores.
