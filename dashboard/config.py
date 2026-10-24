"""Operations console configuration owned by the dashboard service."""

from dataclasses import dataclass, field
from os import getenv
from typing import cast

from common.config import (
    _build_neo4j_uri,
    _build_postgres_connstr,
    _is_truthy,
    get_secret,
)


@dataclass(frozen=True)
class DashboardConfig:
    """Configuration for the privileged operations console."""

    neo4j_host: str
    neo4j_username: str
    neo4j_password: str = field(repr=False)
    postgres_host: str
    postgres_username: str
    postgres_password: str = field(repr=False)
    postgres_database: str
    rabbitmq_prometheus_url: str = "http://rabbitmq:15692"
    cors_origins: list[str] | None = None
    cache_warming_enabled: bool = True
    cache_webhook_secret: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> DashboardConfig:
        """Create configuration without depending on another service's config class."""
        neo4j_username = get_secret("NEO4J_USERNAME")
        neo4j_password = get_secret("NEO4J_PASSWORD")
        postgres_username = get_secret("POSTGRES_USERNAME")
        postgres_password = get_secret("POSTGRES_PASSWORD")
        postgres_database = getenv("POSTGRES_DATABASE")
        missing_vars = [
            name
            for name, value in (
                ("NEO4J_HOST", getenv("NEO4J_HOST")),
                ("NEO4J_USERNAME", neo4j_username),
                ("NEO4J_PASSWORD", neo4j_password),
                ("POSTGRES_HOST", getenv("POSTGRES_HOST")),
                ("POSTGRES_USERNAME", postgres_username),
                ("POSTGRES_PASSWORD", postgres_password),
                ("POSTGRES_DATABASE", postgres_database),
            )
            if not value
        ]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

        cors_origins_raw = getenv("CORS_ORIGINS")
        cors_origins = [origin.strip() for origin in cors_origins_raw.split(",") if origin.strip()] if cors_origins_raw else None
        return cls(
            neo4j_host=_build_neo4j_uri(),
            neo4j_username=cast("str", neo4j_username),
            neo4j_password=cast("str", neo4j_password),
            postgres_host=_build_postgres_connstr(),
            postgres_username=cast("str", postgres_username),
            postgres_password=cast("str", postgres_password),
            postgres_database=cast("str", postgres_database),
            rabbitmq_prometheus_url=getenv("RABBITMQ_PROMETHEUS_URL", "http://rabbitmq:15692").rstrip("/"),
            cors_origins=cors_origins,
            cache_warming_enabled=_is_truthy(getenv("CACHE_WARMING_ENABLED", "true")),
            cache_webhook_secret=get_secret("CACHE_WEBHOOK_SECRET"),
        )


def get_config() -> DashboardConfig:
    """Load operations-console configuration."""
    return DashboardConfig.from_env()
