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

Ingests generated fixture data into Neo4j through the MemoryClient,
demonstrating all three memory types:
- Long-term: Entity knowledge graph (POLE+O)
- Short-term: Document storage as conversation messages
- Reasoning: Decision traces with provenance
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from create_context_graph.ontology import DomainOntology, generate_cypher_schema

console = Console()


async def _ingest_with_memory_client(
    fixture_data: dict,
    ontology: DomainOntology,
    neo4j_uri: str,
    neo4j_username: str,
    neo4j_password: str,
) -> None:
    """Ingest data using neo4j-agent-memory MemoryClient."""
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

            # Step 2: Ingest entities (Long-term memory)
            task = progress.add_task("[2/4] Ingesting entities...", total=None)
            entity_count = 0
            entities = fixture_data.get("entities", {})
            for label, items in entities.items():
                # Map label to POLE+O type
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

            # Step 2b: Create relationships
            relationships = fixture_data.get("relationships", [])
            rel_count = 0
            for rel in relationships:
                try:
                    # Use direct Cypher for relationship creation
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

            # Step 3: Ingest documents as :Document nodes (direct Cypher)
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
            # Link documents to mentioned entities
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

            # Step 4: Ingest decision traces as :DecisionTrace/:TraceStep nodes
            task = progress.add_task("[4/4] Ingesting decision traces...", total=None)
            trace_count = 0
            traces = fixture_data.get("traces", [])
            for trace_data in traces:
                try:
                    await client.graph.execute_write(
                        "MERGE (t:DecisionTrace {id: $id}) "
                        "ON CREATE SET t.started_at = datetime() "
                        "SET t.task = $task, t.outcome = $outcome, t.domain = $domain, "
                        "t.completed_at = datetime()",
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
                            "ON CREATE SET s.timestamp = datetime() "
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

        # Apply schema
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

        # Create entities
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

        # Create relationships
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

        # Create documents
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
            # Link documents to mentioned entities
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

        # Create decision traces
        task = progress.add_task("[5/5] Creating decision traces...", total=None)
        trace_count = 0
        traces = fixture_data.get("traces", [])
        async with driver.session() as session:
            for trace_data in traces:
                try:
                    await session.run(
                        "MERGE (t:DecisionTrace {id: $id}) "
                        "ON CREATE SET t.started_at = datetime() "
                        "SET t.task = $task, t.outcome = $outcome, t.domain = $domain, "
                        "t.completed_at = datetime()",
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
                            "ON CREATE SET s.timestamp = datetime() "
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


def _get_pole_type(label: str, ontology: DomainOntology) -> str:
    """Map an entity label to its POLE+O type."""
    for et in ontology.entity_types:
        if et.label == label:
            return et.pole_type
    return "OBJECT"


def reset_neo4j(
    neo4j_uri: str,
    neo4j_username: str,
    neo4j_password: str,
) -> None:
    """Clear all data from Neo4j."""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_username, neo4j_password))
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")
    driver.close()


def ingest_data(
    fixture_path: Path,
    ontology: DomainOntology,
    neo4j_uri: str,
    neo4j_username: str,
    neo4j_password: str,
) -> None:
    """Ingest fixture data into Neo4j.

    Tries neo4j-agent-memory first, falls back to direct driver.
    """
    if not fixture_path.exists():
        console.print(f"[red]Fixture file not found:[/red] {fixture_path}")
        return

    fixture_data = json.loads(fixture_path.read_text())

    console.print(f"\n  Ingesting {ontology.domain.name} data into Neo4j...")

    try:
        import neo4j_agent_memory  # noqa: F401
        asyncio.run(
            _ingest_with_memory_client(
                fixture_data, ontology,
                neo4j_uri, neo4j_username, neo4j_password,
            )
        )
    except ImportError:
        console.print("  [yellow]neo4j-agent-memory not installed, using direct driver[/yellow]")
        asyncio.run(
            _ingest_with_driver(
                fixture_data, ontology,
                neo4j_uri, neo4j_username, neo4j_password,
            )
        )
