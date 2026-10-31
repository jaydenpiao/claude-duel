# ADR 0013: provider registry

Status: accepted (closes #153).

## Context
Adding Anthropic (#10) touched `container.py` (a branch per vendor), `config.py` (two fields and two env
names per vendor) and `budget.py` (a prefix map). `anthropic.py` imported `raise_for_status` from the
OpenAI adapter, so one vendor's adapter depended on another's.

## Decision
- `adapters/providers/http.py` owns the shared HTTP error mapping, `raise_for_status(resp, vendor)`.
  Every adapter imports it from there.
- `adapters/providers/registry.py:VENDORS` declares each real vendor once: name, model prefix, key env
  var, default model and factory. `make_provider`, `Settings` (`models` and `api_keys` maps, filled from
  the documented `<vendor>_model` keys, `DUEL_<VENDOR>_MODEL` and the key env var) and `budget.vendor_of`
  are all generic over it.
- The user-facing surface stays the same: `openai_model`, `DUEL_ANTHROPIC_MODEL`, `OPENAI_API_KEY` and so on.

## Consequences
A new vendor is one adapter, one registry entry and its prices. Keys still come from the env only, as
`SecretStr`.
