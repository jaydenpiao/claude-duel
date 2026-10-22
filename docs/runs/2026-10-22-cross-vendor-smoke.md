# 2026-10-22 cross-vendor smoke (#10, #145)

claude-haiku-5-5 (alice) vs gpt-6-luna (bob), checked mode, 1 round, 1 challenge each, cap $0.05 total.

| match | max_output_tokens | effort | result | calls | tokens | usd |
|---|---|---|---|---|---|---|
| `47543a795cb1` (vs gpt-4.1-nano, before #145) | 1500 | default | Claude forfeited: "no JSON object found" | 8 | 18150 | 0.0046 |
| `39aec5e4e420` | 1500 | default | both forfeited: `truncated` (8 cut-off replies, repair calls included) | 16 | 48714 | 0.0145 |
| `21f0365f9b49` | 8000 | low | 2 valid challenges, both captured on try 1 | 4 | 10536 | 0.0036 |

Two single-call adapter probes (Messages structured output, Responses `json_schema`) cost under $0.001.

Findings:
- The malformed Claude replies were truncation: `stop_reason == "max_tokens"`. gpt-6-luna truncates at 1500 too, because both vendors count reasoning toward the cap.
- 1500 is too small for current reasoning models. The launch pins `max_output_tokens` and `reasoning_effort` for both sides (docs/experiments/launch-claude-vs-gpt.md).
- alice's challenge was the prompt's example maze, which is why it was captured on try 1. That is acceptable for a smoke, but `variety` stays on for the launch.
