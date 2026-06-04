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

"""Unit tests for the generator module."""

import json
import uuid
from pathlib import Path

import pytest

from create_context_graph.generator import generate_fixture_data
from create_context_graph.ontology import load_domain


class TestGenerateFixtureData:
    def test_generates_fixture_file(self, tmp_path):
        ontology = load_domain("financial-services")
        output = tmp_path / "fixtures.json"
        data = generate_fixture_data(ontology, output)

        assert output.exists()
        assert isinstance(data, dict)

    def test_fixture_has_entities(self, tmp_path):
        ontology = load_domain("financial-services")
        output = tmp_path / "fixtures.json"
        data = generate_fixture_data(ontology, output)

        assert "entities" in data
        assert len(data["entities"]) > 0
        # Should have entities for domain-specific types
        assert "Account" in data["entities"]
        assert "Transaction" in data["entities"]
        # Should also have base POLE+O types
        assert "Person" in data["entities"]

    def test_fixture_has_relationships(self, tmp_path):
        ontology = load_domain("financial-services")
        output = tmp_path / "fixtures.json"
        data = generate_fixture_data(ontology, output)

        assert "relationships" in data
        assert len(data["relationships"]) > 0
        rel = data["relationships"][0]
        assert "type" in rel
        assert "source_label" in rel
        assert "source_name" in rel
        assert "target_label" in rel
        assert "target_name" in rel

    def test_fixture_has_documents(self, tmp_path):
        ontology = load_domain("financial-services")
        output = tmp_path / "fixtures.json"
        data = generate_fixture_data(ontology, output)

        assert "documents" in data
        assert len(data["documents"]) > 0
        doc = data["documents"][0]
        assert "template_id" in doc
        assert "title" in doc
        assert "content" in doc

    def test_static_document_content_uses_markdown(self, tmp_path):
        """Static-generated document content should use Markdown headings."""
        ontology = load_domain("healthcare")
        output = tmp_path / "fixtures.json"
        data = generate_fixture_data(ontology, output)

        for doc in data.get("documents", []):
            content = doc.get("content", "")
            assert "===" not in content, f"Document should not use RST === separators: {doc['title']}"
            assert "## " in content, f"Static document should use markdown ## headings: {doc['title']}"

    def test_static_document_titles_reference_entities(self, tmp_path):
        """Static-generated document titles should reference primary entities."""
        ontology = load_domain("healthcare")
        output = tmp_path / "fixtures.json"
        data = generate_fixture_data(ontology, output)

        documents = data.get("documents", [])
        # At least some titles should reference entities (colon separator)
        entity_titles = [d for d in documents if ": " in d.get("title", "")]
        assert len(entity_titles) > 0, (
            f"Expected entity-derived titles with ': ', got: {[d['title'] for d in documents[:3]]}"
        )

    def test_fixture_has_traces(self, tmp_path):
        ontology = load_domain("financial-services")
        output = tmp_path / "fixtures.json"
        data = generate_fixture_data(ontology, output)

        assert "traces" in data
        assert len(data["traces"]) > 0
        trace = data["traces"][0]
        uuid.UUID(trace["id"])
        assert "task" in trace
        assert "steps" in trace
        assert len(trace["steps"]) > 0
        step = trace["steps"][0]
        assert "thought" in step
        assert "action" in step

    def test_fixture_is_valid_json(self, tmp_path):
        ontology = load_domain("financial-services")
        output = tmp_path / "fixtures.json"
        generate_fixture_data(ontology, output)

        parsed = json.loads(output.read_text())
        assert parsed["domain"] == "financial-services"

    def test_entities_have_names(self, tmp_path):
        ontology = load_domain("healthcare")
        output = tmp_path / "fixtures.json"
        data = generate_fixture_data(ontology, output)

        for label, items in data["entities"].items():
            for item in items:
                assert "name" in item, f"{label} entity missing 'name'"

    def test_no_self_relationships(self, tmp_path):
        ontology = load_domain("software-engineering")
        output = tmp_path / "fixtures.json"
        data = generate_fixture_data(ontology, output)

        for rel in data["relationships"]:
            if rel["source_label"] == rel["target_label"]:
                assert rel["source_name"] != rel["target_name"], (
                    f"Self-relationship found: {rel}"
                )


class TestGenerateMultipleDomains:
    """Test that fixture generation works for a sample of domains."""

    @pytest.mark.parametrize("domain_id", [
        "financial-services",
        "healthcare",
        "software-engineering",
        "wildlife-management",
        "gaming",
        "manufacturing",
    ])
    def test_domain_generates(self, domain_id, tmp_path):
        ontology = load_domain(domain_id)
        output = tmp_path / "fixtures.json"
        data = generate_fixture_data(ontology, output)

        assert output.exists()
        assert len(data["entities"]) > 0
        assert len(data["relationships"]) > 0
        assert len(data["documents"]) > 0
        assert len(data["traces"]) > 0


def _all_fixture_domain_ids():
    """List all domain IDs that have shipped fixture files."""
    from importlib.resources import files
    fixtures_dir = Path(str(files("create_context_graph") / "fixtures"))
    return [p.stem for p in sorted(fixtures_dir.glob("*.json"))]


class TestShippedFixtureQuality:
    """Validate that shipped fixture files meet demo quality standards."""

    @pytest.fixture
    def fixture_data(self, request):
        from importlib.resources import files
        fixtures_dir = Path(str(files("create_context_graph") / "fixtures"))
        path = fixtures_dir / f"{request.param}.json"
        return json.loads(path.read_text())

    @pytest.mark.parametrize("fixture_data", _all_fixture_domain_ids(), indirect=True)
    def test_no_placeholder_entity_names(self, fixture_data):
        """No entity should have the 'Label N' placeholder pattern."""
        import re
        for label, items in fixture_data.get("entities", {}).items():
            for item in items:
                name = item.get("name")
                assert name is not None, f"{label} entity has name=None"
                assert not re.match(
                    r"^(Person|Organization|Location|Event|Object)\s+\d+$", str(name)
                ), f"Placeholder entity name found: {name}"

    @pytest.mark.parametrize("fixture_data", _all_fixture_domain_ids(), indirect=True)
    def test_documents_have_substantial_content(self, fixture_data):
        """Every document should have at least 100 characters of content."""
        for doc in fixture_data.get("documents", []):
            content = doc.get("content", "")
            assert len(content) >= 100, (
                f"Document '{doc.get('title', '?')}' too short: {len(content)} chars"
            )

    @pytest.mark.parametrize("fixture_data", _all_fixture_domain_ids(), indirect=True)
    def test_document_titles_not_just_sequential_numbers(self, fixture_data):
        """Document titles should reference entities, not just be 'Template #N'."""
        documents = fixture_data.get("documents", [])
        if not documents:
            pytest.skip("No documents generated")
        # Titles should NOT be just "{template} #N" — they should include entity names
        # LLM fixtures use " - " separator, static fallback uses ": "
        sequential_only = [d for d in documents if d.get("title", "").endswith((" #1", " #2", " #3", " #4", " #5"))]
        assert len(sequential_only) < len(documents), (
            f"Most document titles should reference entities, not just sequential numbers: "
            f"{[d['title'] for d in documents[:5]]}"
        )

    @pytest.mark.parametrize("fixture_data", _all_fixture_domain_ids(), indirect=True)
    def test_trace_ids_are_uuids(self, fixture_data):
        """Native neo4j-agent-memory reasoning traces require UUID IDs."""
        for trace in fixture_data.get("traces", []):
            uuid.UUID(trace["id"])

    @pytest.mark.parametrize("fixture_data", _all_fixture_domain_ids(), indirect=True)
    def test_traces_no_template_variables(self, fixture_data):
        """No decision trace should have uninterpolated {{variable}} patterns."""
        for trace in fixture_data.get("traces", []):
            task = trace.get("task", "")
            assert "{{" not in task, (
                f"Trace '{trace.get('id', '?')}' has uninterpolated task: {task[:80]}"
            )
            outcome = trace.get("outcome", "")
            assert "{{" not in outcome, (
                f"Trace '{trace.get('id', '?')}' has uninterpolated outcome: {outcome[:80]}"
            )
            for step in trace.get("steps", []):
                obs = step.get("observation", "")
                assert not obs.startswith("Results retrieved for:"), (
                    f"Trace '{trace.get('id', '?')}' has placeholder observation"
                )
