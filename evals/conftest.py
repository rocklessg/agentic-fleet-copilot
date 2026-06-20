import uuid
from pathlib import Path
from typing import Any

import pytest

from evals.helpers import seed_test_database
from src.agent import graph as graph_module
from src.agent.graph import build_graph


@pytest.fixture(autouse=True)
def disable_live_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph_module, "_llm_available", lambda: False)


@pytest.fixture
def patched_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    db_path = tmp_path / "telemetry.db"
    metadata = seed_test_database(db_path)
    monkeypatch.setattr("src.agent.tools.DB_PATH", db_path)
    monkeypatch.setattr("src.database.ingest.DB_PATH", db_path)
    monkeypatch.setattr("src.agent.graph.DB_PATH", db_path)
    monkeypatch.setattr("src.agent.insights.DB_PATH", db_path)
    return metadata


@pytest.fixture
def thread_config() -> dict[str, Any]:
    return {"configurable": {"thread_id": f"test-{uuid.uuid4()}"}}


@pytest.fixture
def deterministic_graph(patched_db: dict[str, Any]) -> Any:
    return build_graph()
