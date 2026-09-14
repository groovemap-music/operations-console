"""Interface contracts for the dashboard database fixtures."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_neo4j_fixture_matches_driver_session_and_result_interfaces(
    dashboard_mock_neo4j_driver: MagicMock,
    dashboard_mock_neo4j_session: MagicMock,
    dashboard_mock_neo4j_result: MagicMock,
) -> None:
    """The resilient wrapper stays sync at session(), then becomes await-faithful."""
    assert isinstance(dashboard_mock_neo4j_driver.session, MagicMock)
    assert isinstance(dashboard_mock_neo4j_session.run, AsyncMock)
    assert isinstance(dashboard_mock_neo4j_result.single, AsyncMock)

    async with dashboard_mock_neo4j_driver.session() as session:
        result = await session.run("RETURN 1")
        assert await result.data() == [{"count": 10}]

    with pytest.raises(AttributeError):
        dashboard_mock_neo4j_driver.not_a_driver_method = True


@pytest.mark.asyncio
async def test_postgres_fixture_matches_resilient_connection_and_cursor_interfaces(
    dashboard_mock_psycopg_connect: MagicMock,
    dashboard_mock_psycopg_connection: MagicMock,
    dashboard_mock_psycopg_cursor: MagicMock,
) -> None:
    """Connection checkout is awaited while cursor() remains a context-manager factory."""
    assert isinstance(dashboard_mock_psycopg_connect.get_connection, AsyncMock)
    assert isinstance(dashboard_mock_psycopg_connection.cursor, MagicMock)
    assert isinstance(dashboard_mock_psycopg_cursor.execute, AsyncMock)

    connection = await dashboard_mock_psycopg_connect.get_connection()
    async with connection.cursor() as cursor:
        await cursor.execute("SELECT 1")
        assert await cursor.fetchone() == (10,)

    with pytest.raises(AttributeError):
        dashboard_mock_psycopg_cursor.not_a_cursor_method = True
