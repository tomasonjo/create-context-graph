# Copyright 2026 Neo4j Labs
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Contract test for the scaffolded ``import_data.py``'s bolt write path.

The NAMS counterpart (``test_nams_ingest_parity.py``) compares the CLI
implementation against the scaffolded template. The bolt path lives only in
the scaffolded template (the CLI has its own ``_ingest_with_driver`` but it
takes a different shape — domain-aware MERGE keys with ``(name, domain)``),
so the contract this test pins is **self-consistency**: a single canonical
fixture must produce a deterministic, well-formed sequence of awaited
``session.run(query, params)`` calls on an ``AsyncDriver``-shaped session
double.

If you change anything in ``_ingest_via_bolt`` — Cypher template, batch
size, deadletter ordering, the ``async with`` lifecycle — this test breaks
loudly. The asserted call sequence is the contract.

v0.12.0 shipped this path as plain ``def`` calling ``with driver.session()``
on an ``AsyncDriver``, which crashed at runtime. The bolt path had no
exercise in any unit test, so the bug got out. This file plugs that gap.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Shared canonical fixture — exercises every load-bearing branch:
#   - entity batches per label
#   - relationship with both source and target labels
#   - relationship missing one or both labels (sanitizer must still pass)
#   - document with all metadata fields
#   - reasoning trace with multiple steps
# ---------------------------------------------------------------------------


CANONICAL_FIXTURE: dict[str, Any] = {
    "entities": {
        "Patient": [
            {"name": "Alice Park", "age": 67, "status": "active"},
            {"name": "Bob Singh", "age": 42, "status": "discharged"},
        ],
        "Hospital": [
            {"name": "Mercy General", "city": "Portland"},
        ],
    },
    "relationships": [
        # Fully-typed: both source_label and target_label present.
        {
            "source_name": "Alice Park", "source_label": "Patient",
            "target_name": "Mercy General", "target_label": "Hospital",
            "type": "TREATED_AT",
        },
        # No source_label, with target_label — still must merge.
        {
            "source_name": "Bob Singh", "target_label": "Hospital",
            "target_name": "Mercy General",
            "type": "TREATED_AT",
        },
    ],
    "documents": [
        {
            "title": "Discharge Note — Bob",
            "content": "Patient discharged today, vitals normal.",
            "template_id": "discharge",
            "template_name": "Discharge Note",
        },
    ],
    "traces": [
        {
            "id": "trace-alpha",
            "task": "Diagnose chest pain",
            "outcome": "Cardiology referral",
            "steps": [
                {"thought": "Rule out MI", "action": "Order ECG", "observation": "Normal sinus"},
                {"thought": "Refer", "action": "Page cardiology", "observation": "Accepted"},
            ],
        },
    ],
}

BODY_FIELDS: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Async driver / session doubles
# ---------------------------------------------------------------------------


class _RecordingSession:
    """``AsyncSession``-shaped double that records every awaited ``run(...)``
    call as ``(cypher, params)`` for later assertion."""

    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, cypher: str, parameters: dict | None = None, **kwargs):
        # The bolt path passes the dict either as a positional 2nd arg or as
        # a keyword (``batch=...``). Normalize: when ``batch`` is passed by
        # keyword, fold it into a ``parameters`` shape.
        if parameters is None and kwargs:
            parameters = dict(kwargs)
        self.calls.append((cypher, dict(parameters or {})))
        return SimpleNamespace(consume=AsyncMock())


class _RecordingDriver:
    """``AsyncDriver``-shaped double — implements ``async with driver`` and
    ``async with driver.session() as session`` plus ``verify_connectivity``."""

    def __init__(self):
        self.session_obj = _RecordingSession()
        self.memory_client: _RecordingMemoryClient | None = None
        self.closed = False
        self.verified = False

    async def verify_connectivity(self):
        self.verified = True

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    def session(self):
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                return outer.session_obj

            async def __aexit__(self_inner, *_):
                return None

        return _Ctx()


class _RecordingMemoryClient:
    """``MemoryClient``-shaped double for native reasoning trace writes."""

    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

        async def _start_trace(**kw):
            self.calls.append(("reasoning.start_trace", dict(kw)))
            return SimpleNamespace(id=f"trace-{len(self.calls)}")

        async def _add_step(**kw):
            self.calls.append(("reasoning.add_step", dict(kw)))
            return SimpleNamespace(id=f"id-{len(self.calls)}")

        async def _complete_trace(**kw):
            self.calls.append(("reasoning.complete_trace", dict(kw)))
            return SimpleNamespace(id=f"id-{len(self.calls)}")

        self.reasoning = SimpleNamespace(
            start_trace=AsyncMock(side_effect=_start_trace),
            add_step=AsyncMock(side_effect=_add_step),
            complete_trace=AsyncMock(side_effect=_complete_trace),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


# ---------------------------------------------------------------------------
# Render the scaffolded template and exec its ``_ingest_via_bolt`` in a
# sandbox so we can wire it to the recording driver.
# ---------------------------------------------------------------------------


def _exec_scaffold_template(driver: _RecordingDriver, memory_client: _RecordingMemoryClient) -> dict[str, Any]:
    template_path = (
        Path(__file__).parent.parent
        / "src" / "create_context_graph" / "templates"
        / "backend" / "connectors" / "import_data.py.j2"
    )
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        keep_trailing_newline=True,
    )
    rendered = env.get_template("import_data.py.j2").render(saas_connectors=[])

    # Stub the modules the template imports at module load time.
    fake_settings = SimpleNamespace(
        memory_backend="bolt",
        neo4j_uri="neo4j://test:7687",
        neo4j_username="neo4j",
        neo4j_password="testpass",
    )
    fake_config_mod = ModuleType("app.config")
    fake_config_mod.settings = fake_settings
    fake_app_mod = ModuleType("app")
    fake_connectors_mod = ModuleType("app.connectors")
    sys.modules["app"] = fake_app_mod
    sys.modules["app.config"] = fake_config_mod
    sys.modules["app.connectors"] = fake_connectors_mod

    # Patch neo4j.AsyncGraphDatabase so the function-local import inside
    # ``_ingest_via_bolt`` resolves to a factory returning our recording
    # driver.
    fake_neo4j = ModuleType("neo4j")

    class _Factory:
        @staticmethod
        def driver(*_args, **_kwargs):
            return driver

    fake_neo4j.AsyncGraphDatabase = _Factory
    sys.modules["neo4j"] = fake_neo4j

    fake_nam = ModuleType("neo4j_agent_memory")
    fake_nam.MemoryClient = MagicMock(return_value=memory_client)
    fake_nam.MemorySettings = MagicMock()
    sys.modules["neo4j_agent_memory"] = fake_nam

    import tempfile
    synthetic_root = Path(tempfile.gettempdir()) / "ccg_bolt_parity_test"
    (synthetic_root / "backend" / "scripts").mkdir(parents=True, exist_ok=True)
    ns: dict[str, Any] = {
        "__name__": "scaffolded_import_data",
        "__file__": str(synthetic_root / "backend" / "scripts" / "import_data.py"),
    }
    exec(compile(rendered, str(template_path), "exec"), ns)
    return ns


def _run_bolt_path() -> tuple[_RecordingDriver, dict[str, int]]:
    driver = _RecordingDriver()
    memory_client = _RecordingMemoryClient()
    driver.memory_client = memory_client
    prior_modules = {
        name: sys.modules.get(name)
        for name in ("app", "app.config", "app.connectors", "neo4j", "neo4j_agent_memory")
    }
    try:
        ns = _exec_scaffold_template(driver, memory_client)
        ingest_via_bolt = ns["_ingest_via_bolt"]

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ns["SIDECAR_DIR"] = Path(tmp)
            ns["DEADLETTER_FILE"] = Path(tmp) / "deadletter.jsonl"
            counts = asyncio.run(ingest_via_bolt(CANONICAL_FIXTURE, BODY_FIELDS))
        return driver, counts
    finally:
        for name, module in prior_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


# ---------------------------------------------------------------------------
# Contract assertions
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bolt_run():
    return _run_bolt_path()


def test_async_context_lifecycle(bolt_run):
    """The function must verify connectivity AND close the driver. v0.12.0's
    sync version did neither — first because it never ran, second because
    the function had no lifecycle management at all."""
    driver, _ = bolt_run
    assert driver.verified, "verify_connectivity was not awaited"
    assert driver.closed, "driver was not closed (async with driver: should auto-close)"


def test_counts(bolt_run):
    _, counts = bolt_run
    # 3 patients + hospital MERGE batches → entities counts each batch's len
    assert counts["entities"] == 3, counts
    # 2 relationships, both should succeed (sanitizer treats missing
    # source_label as "no constraint" — that's the documented behavior).
    assert counts["relationships"] == 2, counts
    assert counts["documents"] == 1, counts
    assert counts["traces"] == 1, counts
    assert counts["failures"] == 0, counts


def test_entity_unwind_cypher_shape(bolt_run):
    driver, _ = bolt_run
    entity_calls = [
        (cy, params) for (cy, params) in driver.session_obj.calls
        if cy.startswith("UNWIND $batch AS item MERGE")
    ]
    # One UNWIND per label (Patient, Hospital). Both labels appear in the
    # generated Cypher; the order is dict-iteration order on the fixture.
    assert len(entity_calls) == 2, entity_calls
    labels_seen = [cy.split("(n:")[1].split(" ")[0] for cy, _ in entity_calls]
    assert sorted(labels_seen) == ["Hospital", "Patient"], labels_seen


def test_relationship_uses_labeled_match_when_available(bolt_run):
    driver, _ = bolt_run
    rel_calls = [
        (cy, params) for (cy, params) in driver.session_obj.calls
        if "MERGE (a)-[r:" in cy
    ]
    assert len(rel_calls) == 2, rel_calls
    # First relationship has both labels — both MATCH lines must include the label.
    fully_typed_cypher, _ = rel_calls[0]
    assert "MATCH (a:Patient" in fully_typed_cypher
    assert "MATCH (b:Hospital" in fully_typed_cypher
    # Second relationship has only target_label — only the (b:Hospital) MATCH should be labeled.
    partial_cypher, _ = rel_calls[1]
    assert "MATCH (a {name" in partial_cypher  # bare 'a' on the source
    assert "MATCH (b:Hospital" in partial_cypher


def test_relationship_params_are_parameterized(bolt_run):
    """Names must go through $params, never string interpolation. v0.12.0
    introduced ``_require_safe_cypher_identifier`` for labels, but the
    *names* must remain parameter-bound — otherwise we'd have re-opened the
    Cypher injection seam the v0.12.0 hardening closed."""
    driver, _ = bolt_run
    rel_calls = [
        (cy, params) for (cy, params) in driver.session_obj.calls
        if "MERGE (a)-[r:" in cy
    ]
    for cy, params in rel_calls:
        assert "$source_name" in cy
        assert "$target_name" in cy
        # Names must be in the params dict, not interpolated.
        assert "source_name" in params
        assert "target_name" in params


def test_document_call_shape(bolt_run):
    driver, _ = bolt_run
    doc_calls = [
        (cy, params) for (cy, params) in driver.session_obj.calls
        if "MERGE (d:Document" in cy
    ]
    assert len(doc_calls) == 1
    _, params = doc_calls[0]
    assert params == {
        "title": "Discharge Note — Bob",
        "content": "Patient discharged today, vitals normal.",
        "template_id": "discharge",
        "template_name": "Discharge Note",
    }


def test_trace_call_shape(bolt_run):
    """Reasoning traces must go through neo4j-agent-memory, not custom Cypher."""
    driver, _ = bolt_run
    assert all("DecisionTrace" not in cy and "TraceStep" not in cy for cy, _ in driver.session_obj.calls)
    assert driver.memory_client is not None
    calls = driver.memory_client.calls
    assert [name for name, _ in calls] == [
        "reasoning.start_trace",
        "reasoning.add_step",
        "reasoning.add_step",
        "reasoning.complete_trace",
    ]
    assert calls[0][1]["task"] == "Diagnose chest pain"
    assert calls[1][1]["trace_id"] == "trace-1"
    assert calls[2][1]["trace_id"] == "trace-1"
    assert calls[3][1]["outcome"] == "Cardiology referral"


def test_call_order(bolt_run):
    """The bolt path writes graph data in a fixed order: entities →
    relationships → documents. Reasoning traces use MemoryClient after graph
    writes. This ordering matters when a relationship MATCH
    needs the endpoints to exist already. Pin the order so a refactor
    can't silently move a write that breaks a downstream MERGE."""
    driver, _ = bolt_run
    order: list[str] = []
    for cy, _ in driver.session_obj.calls:
        if cy.startswith("UNWIND $batch AS item MERGE"):
            order.append("entity")
        elif "MERGE (a)-[r:" in cy:
            order.append("relationship")
        elif "MERGE (d:Document" in cy:
            order.append("document")
        else:
            order.append("other")
    # First all entities, then all relationships, then docs.
    first_rel = order.index("relationship")
    last_entity = max(i for i, x in enumerate(order) if x == "entity")
    assert last_entity < first_rel, order
    first_doc = order.index("document")
    last_rel = max(i for i, x in enumerate(order) if x == "relationship")
    assert last_rel < first_doc, order
