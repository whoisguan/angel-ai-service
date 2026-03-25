"""H3: Rate limiter tests — concurrency slots and daily limits."""

import asyncio
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

import pytest
import pytest_asyncio

from security.rate_limiter import acquire_cli_slot, release_cli_slot, check_daily_limit


# ---------------------------------------------------------------------------
# H3: Concurrency slot (Lock + counter)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_globals():
    """Reset the global counter before each test."""
    import security.rate_limiter as rl
    rl._cli_active = 0
    yield
    rl._cli_active = 0


class TestConcurrencySlot:
    @pytest.mark.asyncio
    async def test_acquire_and_release(self):
        await acquire_cli_slot()
        import security.rate_limiter as rl
        assert rl._cli_active == 1

        await release_cli_slot()
        assert rl._cli_active == 0

    @pytest.mark.asyncio
    async def test_acquire_up_to_max(self):
        with patch("security.rate_limiter.settings") as mock_s:
            mock_s.MAX_CONCURRENT_REQUESTS = 3

            await acquire_cli_slot()
            await acquire_cli_slot()
            await acquire_cli_slot()

            import security.rate_limiter as rl
            assert rl._cli_active == 3

    @pytest.mark.asyncio
    async def test_acquire_beyond_max_raises_429(self):
        from fastapi import HTTPException

        with patch("security.rate_limiter.settings") as mock_s:
            mock_s.MAX_CONCURRENT_REQUESTS = 2

            await acquire_cli_slot()
            await acquire_cli_slot()

            with pytest.raises(HTTPException) as exc_info:
                await acquire_cli_slot()
            assert exc_info.value.status_code == 429

    @pytest.mark.asyncio
    async def test_release_never_goes_negative(self):
        import security.rate_limiter as rl
        assert rl._cli_active == 0

        await release_cli_slot()
        assert rl._cli_active == 0  # clamped to 0, not -1

    @pytest.mark.asyncio
    async def test_release_frees_slot_for_next(self):
        from fastapi import HTTPException

        with patch("security.rate_limiter.settings") as mock_s:
            mock_s.MAX_CONCURRENT_REQUESTS = 1

            await acquire_cli_slot()

            with pytest.raises(HTTPException):
                await acquire_cli_slot()

            await release_cli_slot()

            # Now should succeed
            await acquire_cli_slot()
            import security.rate_limiter as rl
            assert rl._cli_active == 1


# ---------------------------------------------------------------------------
# Daily limit
# ---------------------------------------------------------------------------

class TestDailyLimit:
    def test_under_limit_passes(self):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = {"cnt": 5}

        @contextmanager
        def fake_db():
            yield mock_conn

        with patch("security.rate_limiter.get_db", fake_db), \
             patch("security.rate_limiter.settings") as mock_s:
            mock_s.MAX_REQUESTS_PER_USER_PER_DAY = 100
            # Should not raise
            check_daily_limit(user_id=1, source_system="angel-kpi")

    def test_at_limit_raises_429(self):
        from fastapi import HTTPException

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = {"cnt": 100}

        @contextmanager
        def fake_db():
            yield mock_conn

        with patch("security.rate_limiter.get_db", fake_db), \
             patch("security.rate_limiter.settings") as mock_s:
            mock_s.MAX_REQUESTS_PER_USER_PER_DAY = 100
            with pytest.raises(HTTPException) as exc_info:
                check_daily_limit(user_id=1, source_system="angel-kpi")
            assert exc_info.value.status_code == 429
            assert "Daily" in exc_info.value.detail

    def test_over_limit_raises_429(self):
        from fastapi import HTTPException

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = {"cnt": 150}

        @contextmanager
        def fake_db():
            yield mock_conn

        with patch("security.rate_limiter.get_db", fake_db), \
             patch("security.rate_limiter.settings") as mock_s:
            mock_s.MAX_REQUESTS_PER_USER_PER_DAY = 100
            with pytest.raises(HTTPException):
                check_daily_limit(user_id=1, source_system="angel-kpi")
