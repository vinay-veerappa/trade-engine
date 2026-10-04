"""Generic composition seams for the native runtime (not a job runner)."""

from trade_engine.runtime.plugins import FactoryContext, FactoryResult, load_factory

__all__ = ["FactoryContext", "FactoryResult", "load_factory"]
