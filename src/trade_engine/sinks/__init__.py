"""Sinks for event publishing and persistence (Architecture §4.11)."""

from trade_engine.sinks.journal import HttpJournalSink

__all__ = ["HttpJournalSink"]
