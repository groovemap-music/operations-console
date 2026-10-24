"""Pipeline topology served to the Overview: data, not hand-written element ids.

The topology is derived from the console's pipeline configuration and the promoted
``groovemap.catalog-events`` contracts: a pipeline is a producer feeding one fanout exchange per
entity type, and each consumer binds one queue per entity, named by the contract's
``{exchange_prefix}-{consumer}-{entity}`` template, with a dead-letter exchange and queue named
``{queue}.dlx`` and ``{queue}.dlq``. The Overview builds its layout from this payload and fills it
from live metrics, so adding a consumer or an entity type changes data, never markup.
"""

from __future__ import annotations

from typing import Any


# Display names for the datastores the consumers write to; ids match the database readings.
DATASTORES: dict[str, dict[str, str]] = {
    "neo4j": {"id": "neo4j", "label": "Neo4j"},
    "postgresql": {"id": "postgresql", "label": "PostgreSQL"},
}


def _queue(prefix: str, consumer: str, entity: str) -> dict[str, str]:
    name = f"{prefix}-{consumer}-{entity}"
    return {"entity": entity, "name": name, "dead_letter_exchange": f"{name}.dlx", "dead_letter_queue": f"{name}.dlq"}


def build_topology(configs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Describe every configured pipeline and the datastores its consumers write to."""
    pipelines: list[dict[str, Any]] = []
    datastore_ids: list[str] = []
    for pipeline_id, config in configs.items():
        prefix = config["queue_prefix"]
        entities = list(config["entity_types"])
        consumers = []
        for consumer in config["consumers"]:
            datastore = consumer["datastore"]
            if datastore not in datastore_ids:
                datastore_ids.append(datastore)
            queues = [_queue(prefix, consumer["name"], entity) for entity in entities]
            consumers.append(
                {
                    "name": consumer["name"],
                    "label": consumer["name"].capitalize(),
                    "datastore": datastore,
                    "queues": queues,
                }
            )
        pipelines.append(
            {
                "id": pipeline_id,
                "label": config.get("label", pipeline_id.capitalize()),
                "queue_prefix": prefix,
                "entity_types": entities,
                "producer": {
                    "name": config["producer"],
                    "label": "Extractor",
                    "exchanges": [{"entity": entity, "name": f"{prefix}-{entity}"} for entity in entities],
                },
                "consumers": consumers,
                "dead_letter_queues": [queue["dead_letter_queue"] for consumer in consumers for queue in consumer["queues"]],
            }
        )
    datastores = [DATASTORES.get(datastore, {"id": datastore, "label": datastore}) for datastore in datastore_ids]
    return {"pipelines": pipelines, "datastores": datastores}
