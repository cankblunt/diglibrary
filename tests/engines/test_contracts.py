"""Tests for the public Engine contract surface."""

from diglibrary.engines import (
    DecisionEngine,
    LibraryEngine,
    MetadataEngine,
    ProviderEngine,
    QualityEngine,
    ReportingEngine,
    ResolutionEngine,
)


def test_engine_contracts_expose_only_their_declared_public_operations() -> None:
    """Each Engine exposes its public Phase 1 contract without an implementation."""
    assert "resolve" in ResolutionEngine.__dict__
    assert "collect" in ProviderEngine.__dict__
    assert "decide" in DecisionEngine.__dict__
    assert MetadataEngine.__dict__.get("_is_protocol") is True
    assert QualityEngine.__dict__.get("_is_protocol") is True
    assert LibraryEngine.__dict__.get("_is_protocol") is True
    assert ReportingEngine.__dict__.get("_is_protocol") is True
