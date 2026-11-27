"""One model proxy table (M58): cost and timeouts resolve the same models."""

from __future__ import annotations

from claude_duel.app import calibration, latency


def test_latency_uses_the_calibration_proxy_table() -> None:
    assert latency.PROXY is calibration.PROXY


def test_every_calibrated_model_has_a_latency_row() -> None:
    # cost and timeouts must agree on which models are measured: a model priced from calibration (its own row or a
    # proxy) also gets its timeout from a measured row, never the slowest-model fallback by accident
    for model in [*calibration.MEASURED, *calibration.PROXY]:
        source = model if model in latency.MEASURED else latency.PROXY.get(model, "")
        assert source in latency.MEASURED, f"{model}: no latency row or proxy"
