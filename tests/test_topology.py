"""``/api/topology``: the pipeline layout as data, derived from the pipeline config and the contracts."""

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from dashboard import catalog_contract
from dashboard.dashboard import PIPELINE_CONFIGS
from dashboard.topology import build_topology
from tests.dashboard_test_app import create_test_app


if TYPE_CHECKING:
    from tests.fake_catalog_api import FakeCatalogApi


@pytest.fixture
def topology() -> dict[str, Any]:
    return build_topology(PIPELINE_CONFIGS)


def _pipeline(topology: dict[str, Any], pipeline_id: str) -> dict[str, Any]:
    return next(pipeline for pipeline in topology["pipelines"] if pipeline["id"] == pipeline_id)


def test_every_configured_pipeline_is_described_with_its_producer_and_consumers(topology: dict[str, Any]) -> None:
    assert [pipeline["id"] for pipeline in topology["pipelines"]] == list(PIPELINE_CONFIGS)
    discogs = _pipeline(topology, "discogs")
    assert discogs["producer"]["name"] == "extractor-discogs"
    assert [(c["name"], c["datastore"]) for c in discogs["consumers"]] == [("graphinator", "neo4j"), ("tableinator", "postgresql")]
    assert [store["id"] for store in topology["datastores"]] == ["neo4j", "postgresql"]


def test_queue_and_dead_letter_names_match_the_promoted_contracts(topology: dict[str, Any]) -> None:
    for pipeline in topology["pipelines"]:
        for consumer in pipeline["consumers"]:
            assert [q["entity"] for q in consumer["queues"]] == pipeline["entity_types"]
            for queue in consumer["queues"]:
                entity, consumer_name = queue["entity"], consumer["name"]
                assert queue["name"] == catalog_contract.queue_name(consumer_name, entity)
                assert queue["dead_letter_exchange"] == catalog_contract.dead_letter_exchange_name(consumer_name, entity)
                assert queue["dead_letter_queue"] == catalog_contract.dead_letter_queue_name(consumer_name, entity)
        source = pipeline["id"]
        assert [e["name"] for e in pipeline["producer"]["exchanges"]] == [
            catalog_contract.exchange_name(source, entity) for entity in pipeline["entity_types"]
        ]


def test_the_entity_vocabulary_is_the_contracts(topology: dict[str, Any]) -> None:
    for pipeline in topology["pipelines"]:
        assert sorted(pipeline["entity_types"]) == sorted(catalog_contract.entity_types(pipeline["id"]))


def test_dead_letter_queues_are_listed_per_pipeline(topology: dict[str, Any]) -> None:
    discogs = _pipeline(topology, "discogs")
    assert f"{discogs['queue_prefix']}-graphinator-masters.dlq" in discogs["dead_letter_queues"]
    assert len(discogs["dead_letter_queues"]) == len(discogs["consumers"]) * len(discogs["entity_types"])


def test_a_third_consumer_and_entity_type_are_just_more_data() -> None:
    config = {
        "label": "Synthetic",
        "producer": "extractor-synthetic",
        "consumers": [{"name": "alpha", "datastore": "neo4j"}, {"name": "beta", "datastore": "postgresql"}, {"name": "gamma", "datastore": "valkey"}],
        "queue_prefix": "acme-synthetic",
        "entity_types": ["artists", "labels", "tracks"],
    }
    pipeline = build_topology({"synthetic": config})["pipelines"][0]
    assert [c["name"] for c in pipeline["consumers"]] == ["alpha", "beta", "gamma"]
    assert pipeline["consumers"][2]["queues"][2]["name"] == "acme-synthetic-gamma-tracks"
    assert pipeline["consumers"][2]["queues"][2]["dead_letter_queue"] == "acme-synthetic-gamma-tracks.dlq"
    assert build_topology({"synthetic": config})["datastores"][-1] == {"id": "valkey", "label": "valkey"}


def test_the_route_serves_the_topology_to_a_session_and_refuses_anonymous(fake_catalog: FakeCatalogApi) -> None:
    with TestClient(create_test_app()) as client:
        assert client.get("/api/topology").status_code == 401
        response = client.get("/api/topology", headers={"Authorization": f"Bearer {fake_catalog.mint()}"})
    assert response.status_code == 200
    assert response.json() == build_topology(PIPELINE_CONFIGS)
