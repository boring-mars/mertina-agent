"""Automatic context window compression: a cheap auxiliary model summarizes middle turns while head and
tail are protected (iterative summaries, token-budget tail, tool-output pruning first, scaled budgets)."""

_DB_PERSISTED_MARKER = "_db_persisted"
