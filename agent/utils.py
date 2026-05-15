"""Shared utility functions for the AIOps agent."""


def backoff_delay(attempt: int, base: float, max_delay: float) -> float:
    """Compute exponential backoff delay for a given attempt (0-indexed)."""
    return min(base * (2 ** attempt), max_delay)
