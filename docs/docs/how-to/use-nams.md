---
title: Use the Neo4j Agent Memory Service (NAMS)
slug: /how-to/use-nams
---

# Use the Neo4j Agent Memory Service (NAMS)

NAMS is the **default memory backend** for projects scaffolded by create-context-graph. Memory state lives in the cloud and is shared across processes (web app, MCP server, batch ingestion).

## When to use NAMS

- You want to deploy your context graph without provisioning and operating a Neo4j instance.
- You want the web app and `make mcp-server` to share the same memory graph across machines.
- You're building toward a production agent and want hosted memory from day one.

## When to use `--self-hosted` instead

- **Workshop / demo / screen recording** — you want the full pre-populated 80-entity, 180-relationship demo experience.
- **Air-gapped or regulated data** — NAMS is a hosted service; data leaves the local machine.
- **Custom Cypher writes** — your code needs to write arbitrary Cypher (not currently supported by NAMS REST).
- **GDS algorithms** — community detection, PageRank, and other GDS calls require bolt.

## Setting up NAMS

### 1. Provision an API key

Sign up at [memory.neo4jlabs.com](https://memory.neo4jlabs.com). Provision an API key from your dashboard.

:::note
At the time of writing, NAMS does not offer device-code or browser-based OAuth. API key paste is the only supported flow. We'll update this page once additional auth flows ship.
:::

### 2. Scaffold a project

```bash
uvx create-context-graph my-app \
  --domain healthcare \
  --framework strands \
  --nams-api-key sk-nams-...
```

Or pass via env:

```bash
export MEMORY_API_KEY=sk-nams-...
uvx create-context-graph my-app --domain healthcare --framework strands
```

### 3. Use the project

```bash
cd my-app
# ANTHROPIC_API_KEY is needed for the agent — set it in .env
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env
make install
make start
```

The agent populates memory through conversation. There's no separate seeding step on NAMS.

## What's in your `.env`

```bash
MEMORY_BACKEND=nams
MEMORY_API_KEY=sk-nams-...
# MEMORY_NAMS_ENDPOINT=https://memory.neo4jlabs.com/v1   # override if pointing at a self-hosted gateway
```

When `MEMORY_API_KEY` is set, the library auto-selects the NAMS backend even if `MEMORY_BACKEND` is unset.

## NAMS write-path limits (v0.4)

The NAMS REST API exposes a narrower write surface than bolt Cypher. The CLI does best-effort ingest with these documented gaps:

| Operation | NAMS | Self-hosted bolt |
|---|---|---|
| Entity create (`add_entity`) | ✅ name + type + description only — other properties dropped | ✅ full properties |
| Entity properties | ⚠️ Serialized into `description` field as markdown | ✅ first-class on the node |
| Relationships | ❌ Not yet exposed | ✅ Cypher MERGE |
| Preferences / facts | ❌ Not yet exposed | ✅ `add_preference`, `add_fact` |
| Decision traces | ✅ via `reasoning.start_trace` | ✅ via Cypher |
| Documents | ✅ as `short_term.add_message(role="document")` | ✅ as `:Document` nodes |
| Schema DDL | ❌ NAMS owns schema | ✅ `CREATE CONSTRAINT` etc. |
| GDS algorithms | ❌ 501 Not Implemented | ✅ |
| Arbitrary Cypher writes | ❌ Read-only | ✅ |

The frontend handles these gaps transparently — graph view shows entities without edges, document browser pulls from the `documents` session, decision trace panel reads via the NAMS reasoning API.

## Switching backends after scaffold

The memory backend is a runtime choice driven by `MEMORY_BACKEND` and `MEMORY_API_KEY`. To switch a project from NAMS to self-hosted (or vice versa):

1. Update `.env`:
   ```bash
   MEMORY_BACKEND=bolt
   NEO4J_URI=neo4j://localhost:7687
   NEO4J_PASSWORD=...
   ```
2. Restart the backend.

Note: the generated `pyproject.toml` ships with NAMS-appropriate extras by default. To enable local entity extraction on the bolt path, you'll also need to install `neo4j-agent-memory[extraction,fuzzy]`:

```bash
cd backend
uv pip install 'neo4j-agent-memory[litellm,sentence-transformers,extraction,fuzzy]>=0.4.0,<0.6.0'
```

## Resetting NAMS state

`make reset` on a NAMS project enumerates all entities via REST and deletes them one by one. Slow but correct:

```bash
make reset
```

For fast resets, use a self-hosted scaffold — `MATCH (n) DETACH DELETE n` runs in milliseconds.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `MEMORY_BACKEND=nams but MEMORY_API_KEY is not set` | Set `MEMORY_API_KEY` in `.env` or export it. |
| 401 Unauthorized from NAMS | Verify the key is current. Regenerate at the [dashboard](https://memory.neo4jlabs.com). |
| Graph view shows no edges | Expected — NAMS REST does not yet expose `add_relationship`. Use `--self-hosted` for the relationship-rich demo. |
| Entity property pane is just one markdown block | Expected — NAMS REST stores only `description`. The CLI serializes other properties as markdown so they remain readable. |
| `/gds/*` endpoints return 501 | GDS requires bolt. Use `--self-hosted` for GDS workflows. |

## Further Reading

- [Memory Backends](/docs/explanation/memory-backends) — conceptual NAMS vs self-hosted comparison
- [Configure Memory Providers](/docs/how-to/configure-memory-providers) — LiteLLM, native adapters
- [Connect Claude Desktop](/docs/how-to/connect-claude-desktop) — MCP server (NAMS shape included)
