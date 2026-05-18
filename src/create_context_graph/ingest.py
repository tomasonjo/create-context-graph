# Copyright 2026 Neo4j Labs
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Data ingestion pipeline via neo4j-agent-memory.

Two backend-specific paths:

* **NAMS** (default) — entities via ``client.long_term.add_entity`` with all
  non-name properties serialized into the entity ``description`` field as a
  markdown block (NAMS REST drops free-form attributes). Relationships,
  preferences, and facts are unsupported by the NAMS write API and are
  logged-and-skipped. Documents are stored as ``role="document"`` messages.
  Decision traces use the reasoning REST API.

* **Bolt** (self-hosted) — full Cypher ingest. Entities via
  ``MemoryClient.long_term.add_entity`` with attributes, relationships via
  direct Cypher MERGE, documents and decision traces likewise.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from create_context_graph.ontology import DomainOntology, generate_cypher_schema

if TYPE_CHECKING:
    from create_context_graph.config import ProjectConfig

console = Console()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


_RESERVED_DESCRIPTION_KEYS = {"name", "description", "domain", "id", "uuid"}


def _format_attribute(key: str, value: Any) -> str | None:
    """Render one attribute as a human-readable phrase, or None to skip."""
    if value is None or value == "":
        return None
    if isinstance(value, (list, dict)):
        value = json.dumps(value, default=str)
    pretty_key = key.replace("_", " ").strip().capitalize()
    return f"**{pretty_key}**: {value}"


def _serialize_entity_to_description(
    item: dict[str, Any], label: str, pole_type: str
) -> str:
    """Build a markdown description that preserves entity attributes.

    NAMS REST drops everything except ``{name, type, description}`` on
    ``add_entity``. To keep the per-entity property pane in the frontend
    populated, we serialize the remaining ontology properties into a markdown
    block stuffed into ``description``.
    """
    parts: list[str] = []
    existing = (item.get("description") or "").strip()
    if existing:
        parts.append(existing)
    else:
        parts.append(f"{label}.")

    attr_lines: list[str] = []
    for key, value in item.items():
        if key in _RESERVED_DESCRIPTION_KEYS:
            continue
        line = _format_attribute(key, value)
        if line:
            attr_lines.append(line)

    if attr_lines:
        parts.append("")
        parts.extend(attr_lines)

    parts.append("")
    parts.append(f"_pole_type: {pole_type}_")
    return "\n".join(parts)


def _get_pole_type(label: str, ontology: DomainOntology) -> str:
    """Map an entity label to its POLE+O type."""
    for et in ontology.entity_types:
        if et.label == label:
            return et.pole_type
    return "OBJECT"


# ---------------------------------------------------------------------------
# NAMS branch — best-effort B-partial port
# ---------------------------------------------------------------------------


async def _ingest_with_nams(
    fixture_data: dict,
    ontology: DomainOntology,
    api_key: str,
    endpoint: str,
) -> None:
    """Ingest fixture data through the NAMS REST client.

    Limits:
      * Entity attributes other than ``description`` are not persisted by NAMS
        REST — we serialize them into the description field as markdown.
      * Relationships, preferences, and facts are unsupported. We log a single
        aggregated warning per category.
      * Schema DDL is owned by NAMS; we skip ``cypher/schema.cypher``.
    """
    from neo4j_agent_memory import MemoryClient, MemorySettings, NamsConfig
    from pydantic import SecretStr

    settings = MemorySettings(
        backend="nams",
        nams=NamsConfig(api_key=SecretStr(api_key), endpoint=endpoint),
    )

    async with MemoryClient(settings) as client:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:

            # Step 1: skip schema (NAMS owns it)
            console.print("  [dim][1/4] NAMS owns schema — skipping CREATE CONSTRAINT statements[/dim]")

            # Step 2: entities
            task = progress.add_task("[2/4] Ingesting entities (NAMS)...", total=None)
            entity_count = 0
            entities = fixture_data.get("entities", {})
            for label, items in entities.items():
                pole_type = _get_pole_type(label, ontology)
                for item in items:
                    name = item.get("name") or f"{label}-{entity_count}"
                    description = _serialize_entity_to_description(item, label, pole_type)
                    try:
                        await client.long_term.add_entity(
                            name=name,
                            entity_type=pole_type,
                            description=description,
                        )
                        entity_count += 1
                    except Exception as e:
                        console.print(f"  [yellow]Warning:[/yellow] Entity {name}: {e}")
            progress.update(task, description=f"[2/4] Ingested {entity_count} entities")

            # Step 2b: relationships — unsupported, log once
            rel_count = len(fixture_data.get("relationships", []))
            if rel_count:
                console.print(
                    f"  [yellow]Note:[/yellow] {rel_count} relationships not "
                    "persisted — NAMS write API does not yet support "
                    "add_relationship. The graph will be disconnected; "
                    "use --self-hosted for the full relationship-rich demo."
                )

            # Step 3: documents → messages with role="document"
            task = progress.add_task("[3/4] Ingesting documents (as messages)...", total=None)
            doc_count = 0
            documents = fixture_data.get("documents", [])
            doc_session = f"docs-{ontology.domain.id}"
            for doc in documents:
                try:
                    await client.short_term.add_message(
                        session_id=doc_session,
                        role="document",
                        content=doc.get("content", ""),
                        metadata={
                            "title": doc.get("title", ""),
                            "template_id": doc.get("template_id", ""),
                            "template_name": doc.get("template_name", ""),
                            "domain": ontology.domain.id,
                        },
                    )
                    doc_count += 1
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] Document: {e}")
            progress.update(task, description=f"[3/4] Ingested {doc_count} documents")

            # Step 4: decision traces via reasoning API
            task = progress.add_task("[4/4] Ingesting decision traces...", total=None)
            trace_count = 0
            traces = fixture_data.get("traces", [])
            trace_session = f"traces-{ontology.domain.id}"
            for trace_data in traces:
                try:
                    trace = await client.reasoning.start_trace(
                        session_id=trace_session,
                        task=trace_data.get("task", ""),
                    )
                    trace_id = getattr(trace, "id", None) or trace_data.get("id", "")
                    for step in trace_data.get("steps", []):
                        await client.reasoning.add_step(
                            trace_id=trace_id,
                            thought=step.get("thought", ""),
                            action=step.get("action", ""),
                            observation=step.get("observation", ""),
                        )
                    await client.reasoning.complete_trace(
                        trace_id=trace_id,
                        outcome=trace_data.get("outcome", ""),
                        success=True,
                    )
                    trace_count += 1
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] Trace: {e}")
            progress.update(task, description=f"[4/4] Ingested {trace_count} decision traces")

    console.print(
        f"\n  [green]NAMS ingestion complete:[/green] {entity_count} entities, "
        f"{doc_count} documents, {trace_count} traces "
        f"([dim]{rel_count} relationships skipped[/dim])"
    )


# ---------------------------------------------------------------------------
# Bolt branch — full ingest via MemoryClient (writes Cypher under the hood)
# ---------------------------------------------------------------------------


async def _ingest_with_memory_client(
    fixture_data: dict,
    ontology: DomainOntology,
    neo4j_uri: str,
    neo4j_username: str,
    neo4j_password: str,
) -> None:
    """Ingest data using neo4j-agent-memory MemoryClient (bolt backend)."""
    from pydantic import SecretStr
    from neo4j_agent_memory import MemoryClient, MemorySettings

    settings = MemorySettings(
        neo4j={
            "uri": neo4j_uri,
            "username": neo4j_username,
            "password": SecretStr(neo4j_password),
        }
    )

    async with MemoryClient(settings) as client:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:

            # Step 1: Apply schema
            task = progress.add_task("[1/4] Applying schema...", total=None)
            cypher_schema = generate_cypher_schema(ontology)
            for statement in cypher_schema.split(";"):
                stmt = statement.strip()
                if stmt and not stmt.startswith("//"):
                    try:
                        await client.graph.execute_write(stmt)
                    except Exception as e:
                        if "already exists" not in str(e).lower():
                            console.print(f"  [yellow]Warning:[/yellow] Schema: {e}")
            progress.update(task, description="[1/4] Schema applied")

            # Step 2: Ingest entities
            task = progress.add_task("[2/4] Ingesting entities...", total=None)
            entity_count = 0
            entities = fixture_data.get("entities", {})
            for label, items in entities.items():
                pole_type = _get_pole_type(label, ontology)
                for item in items:
                    name = item.get("name", f"{label}-{entity_count}")
                    try:
                        attrs = {**item, "domain": ontology.domain.id}
                        await client.long_term.add_entity(
                            name=name,
                            entity_type=pole_type,
                            description=item.get("description", f"{label}: {name}"),
                            attributes=attrs,
                        )
                        entity_count += 1
                    except Exception as e:
                        console.print(f"  [yellow]Warning:[/yellow] Entity {name}: {e}")
            progress.update(task, description=f"[2/4] Ingested {entity_count} entities")

            # Step 2b: relationships via Cypher
            relationships = fixture_data.get("relationships", [])
            rel_count = 0
            for rel in relationships:
                try:
                    cypher = f"""
                    MATCH (a {{name: $source_name}})
                    MATCH (b {{name: $target_name}})
                    MERGE (a)-[r:{rel['type']}]->(b)
                    RETURN type(r)
                    """
                    await client.graph.execute_write(
                        cypher,
                        {
                            "source_name": rel["source_name"],
                            "target_name": rel["target_name"],
                        },
                    )
                    rel_count += 1
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] Relationship {rel.get('type', '?')}: {e}")
            console.print(f"  Created {rel_count} relationships")

            # Step 3: documents
            task = progress.add_task("[3/4] Ingesting documents...", total=None)
            doc_count = 0
            documents = fixture_data.get("documents", [])
            for doc in documents:
                try:
                    cypher = """
                    MERGE (d:Document {title: $title})
                    SET d.content = $content,
                        d.template_id = $template_id,
                        d.template_name = $template_name,
                        d.domain = $domain
                    """
                    await client.graph.execute_write(
                        cypher,
                        {
                            "title": doc.get("title", ""),
                            "content": doc.get("content", ""),
                            "template_id": doc.get("template_id", ""),
                            "template_name": doc.get("template_name", ""),
                            "domain": ontology.domain.id,
                        },
                    )
                    doc_count += 1
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] Document: {e}")
            if doc_count > 0:
                try:
                    link_cypher = """
                    MATCH (d:Document) WHERE d.domain = $domain
                    MATCH (e) WHERE e.name IS NOT NULL
                      AND NOT 'Document' IN labels(e)
                      AND NOT 'DecisionTrace' IN labels(e)
                      AND NOT 'TraceStep' IN labels(e)
                      AND (e.domain IS NULL OR e.domain = $domain)
                      AND d.content CONTAINS e.name
                    MERGE (d)-[:MENTIONS]->(e)
                    """
                    await client.graph.execute_write(
                        link_cypher, {"domain": ontology.domain.id}
                    )
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] Document links: {e}")
            progress.update(task, description=f"[3/4] Ingested {doc_count} documents")

            # Step 4: decision traces
            task = progress.add_task("[4/4] Ingesting decision traces...", total=None)
            trace_count = 0
            traces = fixture_data.get("traces", [])
            for trace_data in traces:
                try:
                    await client.graph.execute_write(
                        "MERGE (t:DecisionTrace {id: $id}) "
                        "SET t.task = $task, t.outcome = $outcome, t.domain = $domain",
                        {
                            "id": trace_data.get("id", ""),
                            "task": trace_data.get("task", ""),
                            "outcome": trace_data.get("outcome", ""),
                            "domain": ontology.domain.id,
                        },
                    )
                    for i, step in enumerate(trace_data.get("steps", [])):
                        await client.graph.execute_write(
                            "MATCH (t:DecisionTrace {id: $trace_id}) "
                            "MERGE (s:TraceStep {trace_id: $trace_id, step_number: $step_number}) "
                            "SET s.thought = $thought, s.action = $action, s.observation = $observation "
                            "MERGE (t)-[:HAS_STEP]->(s)",
                            {
                                "trace_id": trace_data.get("id", ""),
                                "step_number": i + 1,
                                "thought": step.get("thought", ""),
                                "action": step.get("action", ""),
                                "observation": step.get("observation", ""),
                            },
                        )
                    trace_count += 1
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] Trace: {e}")
            progress.update(task, description=f"[4/4] Ingested {trace_count} decision traces")

    console.print(
        f"\n  [green]Ingestion complete:[/green] {entity_count} entities, "
        f"{rel_count} relationships, {doc_count} documents, {trace_count} traces"
    )


async def _ingest_with_driver(
    fixture_data: dict,
    ontology: DomainOntology,
    neo4j_uri: str,
    neo4j_username: str,
    neo4j_password: str,
) -> None:
    """Fallback: ingest using neo4j driver directly (no neo4j-agent-memory)."""
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(
        neo4j_uri,
        auth=(neo4j_username, neo4j_password),
    )

    try:
        await driver.verify_connectivity()
    except Exception as e:
        console.print(f"  [red]Cannot connect to Neo4j:[/red] {e}")
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:

        task = progress.add_task("[1/5] Applying schema...", total=None)
        cypher_schema = generate_cypher_schema(ontology)
        async with driver.session() as session:
            for statement in cypher_schema.split(";"):
                stmt = statement.strip()
                if stmt and not stmt.startswith("//"):
                    try:
                        await session.run(stmt)
                    except Exception as e:
                        if "already exists" not in str(e).lower():
                            console.print(f"  [yellow]Warning:[/yellow] Schema: {e}")
        progress.update(task, description="[1/5] Schema applied")

        task = progress.add_task("[2/5] Creating entities...", total=None)
        entity_count = 0
        entities = fixture_data.get("entities", {})
        async with driver.session() as session:
            for label, items in entities.items():
                for item in items:
                    enriched = {**item, "domain": ontology.domain.id}
                    set_clauses = ", ".join(f"n.{k} = ${k}" for k in enriched.keys())
                    cypher = f"MERGE (n:{label} {{name: $name, domain: $domain}}) SET {set_clauses}"
                    try:
                        await session.run(cypher, enriched)
                        entity_count += 1
                    except Exception as e:
                        console.print(f"  [yellow]Warning:[/yellow] Entity {item.get('name', '?')}: {e}")
        progress.update(task, description=f"[2/5] Created {entity_count} entities")

        task = progress.add_task("[3/5] Creating relationships...", total=None)
        rel_count = 0
        relationships = fixture_data.get("relationships", [])
        async with driver.session() as session:
            for rel in relationships:
                cypher = f"""
                MATCH (a:{rel['source_label']} {{name: $source_name}})
                MATCH (b:{rel['target_label']} {{name: $target_name}})
                MERGE (a)-[r:{rel['type']}]->(b)
                """
                try:
                    await session.run(cypher, {
                        "source_name": rel["source_name"],
                        "target_name": rel["target_name"],
                    })
                    rel_count += 1
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] Relationship {rel.get('type', '?')}: {e}")
        progress.update(task, description=f"[3/5] Created {rel_count} relationships")

        task = progress.add_task("[4/5] Creating documents...", total=None)
        doc_count = 0
        documents = fixture_data.get("documents", [])
        async with driver.session() as session:
            for doc in documents:
                try:
                    await session.run(
                        "MERGE (d:Document {title: $title}) "
                        "SET d.content = $content, d.template_id = $template_id, "
                        "d.template_name = $template_name, d.domain = $domain",
                        {
                            "title": doc.get("title", ""),
                            "content": doc.get("content", ""),
                            "template_id": doc.get("template_id", ""),
                            "template_name": doc.get("template_name", ""),
                            "domain": ontology.domain.id,
                        },
                    )
                    doc_count += 1
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] Document: {e}")
            if doc_count > 0:
                try:
                    await session.run(
                        "MATCH (d:Document) WHERE d.domain = $domain "
                        "MATCH (e) WHERE e.name IS NOT NULL "
                        "AND NOT 'Document' IN labels(e) "
                        "AND NOT 'DecisionTrace' IN labels(e) "
                        "AND NOT 'TraceStep' IN labels(e) "
                        "AND (e.domain IS NULL OR e.domain = $domain) "
                        "AND d.content CONTAINS e.name "
                        "MERGE (d)-[:MENTIONS]->(e)",
                        {"domain": ontology.domain.id},
                    )
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] Document links: {e}")
        progress.update(task, description=f"[4/5] Created {doc_count} documents")

        task = progress.add_task("[5/5] Creating decision traces...", total=None)
        trace_count = 0
        traces = fixture_data.get("traces", [])
        async with driver.session() as session:
            for trace_data in traces:
                try:
                    await session.run(
                        "MERGE (t:DecisionTrace {id: $id}) "
                        "SET t.task = $task, t.outcome = $outcome, t.domain = $domain",
                        {
                            "id": trace_data.get("id", ""),
                            "task": trace_data.get("task", ""),
                            "outcome": trace_data.get("outcome", ""),
                            "domain": ontology.domain.id,
                        },
                    )
                    for i, step in enumerate(trace_data.get("steps", [])):
                        await session.run(
                            "MATCH (t:DecisionTrace {id: $trace_id}) "
                            "MERGE (s:TraceStep {trace_id: $trace_id, step_number: $step_number}) "
                            "SET s.thought = $thought, s.action = $action, s.observation = $observation "
                            "MERGE (t)-[:HAS_STEP]->(s)",
                            {
                                "trace_id": trace_data.get("id", ""),
                                "step_number": i + 1,
                                "thought": step.get("thought", ""),
                                "action": step.get("action", ""),
                                "observation": step.get("observation", ""),
                            },
                        )
                    trace_count += 1
                except Exception as e:
                    console.print(f"  [yellow]Warning:[/yellow] Trace: {e}")
        progress.update(task, description=f"[5/5] Created {trace_count} decision traces")

    await driver.close()
    console.print(
        f"\n  [green]Ingestion complete:[/green] {entity_count} entities, "
        f"{rel_count} relationships, {doc_count} documents, {trace_count} traces"
    )


# ---------------------------------------------------------------------------
# Reset helpers
# ---------------------------------------------------------------------------


def reset_neo4j(neo4j_uri: str, neo4j_username: str, neo4j_password: str) -> None:
    """Clear all data from Neo4j (bolt backend)."""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_username, neo4j_password))
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    driver.close()


async def _reset_nams(api_key: str, endpoint: str) -> None:
    """Best-effort reset on NAMS: list entities and delete one-by-one.

    Slow (one REST call per entity) — print a warning. Conversations and
    reasoning traces are reset via session-level clear calls.
    """
    from neo4j_agent_memory import MemoryClient, MemorySettings, NamsConfig
    from pydantic import SecretStr

    console.print(
        "  [yellow]NAMS reset is per-entity (slow). For fast reset, use --self-hosted.[/yellow]"
    )

    settings = MemorySettings(
        backend="nams",
        nams=NamsConfig(api_key=SecretStr(api_key), endpoint=endpoint),
    )
    async with MemoryClient(settings) as client:
        deleted = 0
        try:
            entities = await client.long_term.search_entities(query="", limit=1000)
            for ent in entities:
                ent_id = getattr(ent, "id", None) or getattr(ent, "entity_id", None)
                if ent_id is None:
                    continue
                try:
                    await client.long_term.delete_entity(ent_id)
                    deleted += 1
                except Exception:
                    pass
        except Exception as e:
            console.print(f"  [yellow]Reset partial:[/yellow] {e}")
    console.print(f"  [green]Reset complete:[/green] {deleted} entities removed")


def reset_memory_store(config: "ProjectConfig") -> None:
    """Backend-aware reset entry point."""
    if config.is_nams:
        if not config.nams_api_key:
            console.print("  [red]No NAMS API key available — set MEMORY_API_KEY to reset.[/red]")
            return
        asyncio.run(_reset_nams(config.nams_api_key, config.nams_endpoint))
    else:
        reset_neo4j(config.neo4j_uri, config.neo4j_username, config.neo4j_password)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def ingest_data(
    fixture_path: Path,
    ontology: DomainOntology,
    config: "ProjectConfig",
) -> None:
    """Ingest fixture data into the configured memory backend."""
    if not fixture_path.exists():
        console.print(f"[red]Fixture file not found:[/red] {fixture_path}")
        return

    fixture_data = json.loads(fixture_path.read_text())

    console.print(f"\n  Ingesting {ontology.domain.name} data into {config.memory_backend}...")

    if config.is_nams:
        if not config.nams_api_key:
            console.print(
                "  [red]Cannot ingest into NAMS: no API key. "
                "Set MEMORY_API_KEY or pass --nams-api-key.[/red]"
            )
            return
        asyncio.run(
            _ingest_with_nams(
                fixture_data, ontology, config.nams_api_key, config.nams_endpoint
            )
        )
        return

    # Bolt path: try MemoryClient first, fall back to direct driver
    try:
        import neo4j_agent_memory  # noqa: F401
        asyncio.run(
            _ingest_with_memory_client(
                fixture_data, ontology,
                config.neo4j_uri, config.neo4j_username, config.neo4j_password,
            )
        )
    except ImportError:
        console.print("  [yellow]neo4j-agent-memory not installed, using direct driver[/yellow]")
        asyncio.run(
            _ingest_with_driver(
                fixture_data, ontology,
                config.neo4j_uri, config.neo4j_username, config.neo4j_password,
            )
        )
