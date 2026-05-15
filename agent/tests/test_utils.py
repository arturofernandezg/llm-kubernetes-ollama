"""Tests for agent/utils.py."""

import pytest
from utils import backoff_delay


class TestBackoffDelay:
    def test_first_attempt_returns_base(self):
        assert backoff_delay(0, base=1.0, max_delay=30.0) == 1.0

    def test_doubles_each_attempt(self):
        assert backoff_delay(1, base=1.0, max_delay=30.0) == 2.0
        assert backoff_delay(2, base=1.0, max_delay=30.0) == 4.0
        assert backoff_delay(3, base=1.0, max_delay=30.0) == 8.0

    def test_capped_at_max_delay(self):
        assert backoff_delay(10, base=1.0, max_delay=30.0) == 30.0

    def test_custom_base_scales_correctly(self):
        assert backoff_delay(0, base=0.5, max_delay=10.0) == 0.5
        assert backoff_delay(1, base=0.5, max_delay=10.0) == 1.0
        assert backoff_delay(2, base=0.5, max_delay=10.0) == 2.0

    def test_max_delay_equal_to_computed(self):
        # When computed == max_delay, should return max_delay without capping error
        assert backoff_delay(5, base=1.0, max_delay=32.0) == 32.0
