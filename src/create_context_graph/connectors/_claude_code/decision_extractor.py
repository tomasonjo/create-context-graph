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

"""Heuristic reasoning trace extraction from Claude Code sessions.

Identifies decision points in conversations using four signal types:

1. **User corrections** — user overrides or redirects Claude's approach.
2. **Deliberation markers** — explicit discussion of alternatives.
3. **Error-resolution cycles** — an error followed by a corrective action.
4. **Dependency changes** — package install/remove commands.

Each detected decision produces :Decision and :Alternative entities plus
reasoning trace entries compatible with ``ingest.py``'s trace format.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# ---------------------------------------------------------------------------
# Signal detection patterns
# ---------------------------------------------------------------------------

# User correction language (case-insensitive).
_CORRECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bno[,.]?\s+(instead|don'?t|do not|use|try|change|switch)", re.I),
    re.compile(r"\bactually[,.]?\s+(let'?s|use|try|change|I want|we should)", re.I),
    re.compile(r"\bthat'?s not (what|right|correct)", re.I),
    re.compile(r"\brevert\b", re.I),
    re.compile(r"\bundo\b", re.I),
    re.compile(r"\bchange it to\b", re.I),
    re.compile(r"\bdon'?t do that\b", re.I),
    re.compile(r"\bwrong\b.*\binstead\b", re.I),
    re.compile(r"\bstop\b.*\binstead\b", re.I),
    re.compile(r"\bnot that\b", re.I),
]

# Deliberation language in assistant messages.
_DELIBERATION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bshould (we|I)\b", re.I),
    re.compile(r"\balternatively\b", re.I),
    re.compile(r"\boption (A|B|1|2)\b", re.I),
    re.compile(r"\btrade-?off\b", re.I),
    re.compile(r"\bpros and cons\b", re.I),
    re.compile(r"\bI'?d recommend\b", re.I),
    re.compile(r"\bwe could (either|also|instead)\b", re.I),
    re.compile(r"\bon the other hand\b", re.I),
    re.compile(r"\bapproach(es)?\b.*\bvs\b", re.I),
    re.compile(r"\bchoice between\b", re.I),
]

# Package install commands in Bash tool calls.
_INSTALL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(pip|pip3)\s+install\b"),
    re.compile(r"\buv\s+(add|pip install)\b"),
    re.compile(r"\bnpm\s+install\b"),
    re.compile(r"\byarn\s+add\b"),
    re.compile(r"\bpnpm\s+(add|install)\b"),
    re.compile(r"\bcargo\s+add\b"),
    re.compile(r"\bgo\s+get\b"),
    re.compile(r"\bgem\s+install\b"),
]

# Minimum confidence to emit a decision.
_MIN_CONFIDENCE = 0.4


def extract_decisions(parsed_session: dict[str, Any]) -> dict[str, Any]:
    """Analyse a parsed session for decision points.

    Parameters
    ----------
    parsed_session:
        Output of ``parser.parse_session()``.

    Returns
    -------
    dict
        ``{"entities": {...}, "relationships": [...], "traces": [...]}``
    """
    entities: dict[str, list[dict[str, Any]]] = {}
    relationships: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []

    session_id = parsed_session["session_id"]
    messages = parsed_session.get("messages", [])
    tool_calls = parsed_session.get("tool_calls", [])
    errors = parsed_session.get("errors", [])

    # Build a quick lookup: tool_use_id -> tool call dict.
    tc_by_id: dict[str, dict[str, Any]] = {
        tc["tool_use_id"]: tc for tc in tool_calls
    }

    # Session name (for relationships). Prefer the canonical name supplied by
    # the caller/connector so relationship endpoints stay aligned with the
    # Session entity name used during ingestion. Fall back to the previous
    # first-user-prompt heuristic for backwards compatibility.
    session_name = (
        parsed_session.get("session_name")
        or parsed_session.get("name")
        or ""
    )
    if not session_name:
        first_prompt = ""
        for m in messages:
            if m["role"] == "user" and m.get("full_content", m.get("content", "")):
                first_prompt = m.get("full_content", m.get("content", ""))[:80]
                break
        session_name = first_prompt or f"Session {session_id[:8]}"

    # --- 1. User correction decisions ---
    _detect_corrections(
        messages, session_id, session_name,
        entities, relationships, traces,
    )

    # --- 2. Deliberation decisions ---
    _detect_deliberations(
        messages, session_id, session_name,
        entities, relationships, traces,
    )

    # --- 3. Error-resolution decisions ---
    _detect_error_resolutions(
        errors, tool_calls, tc_by_id, session_id, session_name,
        entities, relationships, traces,
    )

    # --- 4. Dependency change decisions ---
    _detect_dependency_changes(
        tool_calls, session_id, session_name,
        entities, relationships, traces,
    )

    return {
        "entities": entities,
        "relationships": relationships,
        "traces": traces,
    }


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _detect_corrections(
    messages: list[dict[str, Any]],
    session_id: str,
    session_name: str,
    entities: dict[str, list[dict[str, Any]]],
    relationships: list[dict[str, Any]],
    traces: list[dict[str, Any]],
) -> None:
    """Detect user corrections following assistant actions."""
    for i, msg in enumerate(messages):
        if msg["role"] != "user" or i == 0:
            continue

        text = msg.get("full_content", msg.get("content", ""))
        if not text:
            continue

        # Check if this is a correction.
        matched = any(p.search(text) for p in _CORRECTION_PATTERNS)
        if not matched:
            continue

        # Find the preceding assistant message.
        prev_assistant = None
        for j in range(i - 1, -1, -1):
            if messages[j]["role"] == "assistant":
                prev_assistant = messages[j]
                break

        if not prev_assistant:
            continue

        prev_text = prev_assistant.get("full_content", prev_assistant.get("content", ""))
        decision_id = _make_id(session_id, i, "correction")

        description = f"User correction: {text[:120]}"
        decision = {
            "name": f"decision-{decision_id}",
            "description": description,
            "timestamp": msg["timestamp"],
            "outcome": "REVISED",
            "confidence": 0.75,
            "category": "correction",
            "sessionId": session_id,
        }
        entities.setdefault("Decision", []).append(decision)

        # Original approach = rejected alternative.
        orig_alt = {
            "name": f"alt-{decision_id}-original",
            "description": prev_text[:200] if prev_text else "Original approach",
            "wasChosen": False,
            "reason": "User corrected this approach",
        }
        entities.setdefault("Alternative", []).append(orig_alt)

        # Correction = chosen alternative.
        corr_alt = {
            "name": f"alt-{decision_id}-correction",
            "description": text[:200],
            "wasChosen": True,
            "reason": "User chose this direction",
        }
        entities.setdefault("Alternative", []).append(corr_alt)

        # Relationships.
        relationships.extend([
            {
                "type": "MADE_DECISION",
                "source_name": session_name,
                "source_label": "Session",
                "target_name": decision["name"],
                "target_label": "Decision",
            },
            {
                "type": "REJECTED",
                "source_name": decision["name"],
                "source_label": "Decision",
                "target_name": orig_alt["name"],
                "target_label": "Alternative",
            },
            {
                "type": "CHOSE",
                "source_name": decision["name"],
                "source_label": "Decision",
                "target_name": corr_alt["name"],
                "target_label": "Alternative",
            },
        ])

        # Trace entry.
        traces.append({
            "id": f"claude-decision-{decision_id}",
            "task": f"Decision: {description}",
            "outcome": f"User corrected: {text[:100]}",
            "steps": [
                {
                    "thought": f"Claude proposed: {prev_text[:150]}" if prev_text else "Claude proposed an approach",
                    "action": "Claude executed tools",
                    "observation": "User reviewed the result",
                },
                {
                    "thought": f"User correction: {text[:150]}",
                    "action": "User redirected the approach",
                    "observation": "Direction changed",
                },
            ],
        })


def _detect_deliberations(
    messages: list[dict[str, Any]],
    session_id: str,
    session_name: str,
    entities: dict[str, list[dict[str, Any]]],
    relationships: list[dict[str, Any]],
    traces: list[dict[str, Any]],
) -> None:
    """Detect deliberation patterns in assistant messages."""
    for i, msg in enumerate(messages):
        if msg["role"] != "assistant":
            continue

        text = msg.get("full_content", msg.get("content", ""))
        if not text:
            continue

        # Count how many deliberation patterns match.
        match_count = sum(1 for p in _DELIBERATION_PATTERNS if p.search(text))
        if match_count < 2:
            continue

        decision_id = _make_id(session_id, i, "deliberation")
        confidence = min(0.5 + match_count * 0.1, 0.9)

        description = text[:150]
        decision = {
            "name": f"decision-{decision_id}",
            "description": f"Deliberation: {description}",
            "timestamp": msg["timestamp"],
            "outcome": "ACCEPTED",
            "confidence": confidence,
            "category": "architecture",
            "sessionId": session_id,
        }
        entities.setdefault("Decision", []).append(decision)

        relationships.append({
            "type": "MADE_DECISION",
            "source_name": session_name,
            "source_label": "Session",
            "target_name": decision["name"],
            "target_label": "Decision",
        })

        traces.append({
            "id": f"claude-decision-{decision_id}",
            "task": "Decision: Deliberation in session",
            "outcome": description[:100],
            "steps": [
                {
                    "thought": text[:200],
                    "action": "Alternatives were discussed",
                    "observation": "A direction was chosen",
                },
            ],
        })


def _detect_error_resolutions(
    errors: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    tc_by_id: dict[str, dict[str, Any]],
    session_id: str,
    session_name: str,
    entities: dict[str, list[dict[str, Any]]],
    relationships: list[dict[str, Any]],
    traces: list[dict[str, Any]],
) -> None:
    """Detect error-followed-by-resolution as decision points."""
    for err in errors:
        err_tc = tc_by_id.get(err["tool_use_id"])
        if not err_tc:
            continue

        # Find the next non-error tool call (the "resolution").
        err_idx = next(
            (j for j, tc in enumerate(tool_calls) if tc["tool_use_id"] == err["tool_use_id"]),
            None,
        )
        if err_idx is None:
            continue

        resolution_tc = None
        for j in range(err_idx + 1, min(err_idx + 6, len(tool_calls))):
            if not tool_calls[j].get("is_error", False):
                resolution_tc = tool_calls[j]
                break

        if not resolution_tc:
            continue

        decision_id = _make_id(session_id, err_idx, "error-resolution")
        err_msg = err["message"][:120]

        decision = {
            "name": f"decision-{decision_id}",
            "description": f"Error resolution: {err_msg}",
            "timestamp": err.get("timestamp", ""),
            "outcome": "ACCEPTED",
            "confidence": 0.7,
            "category": "error-fix",
            "sessionId": session_id,
        }
        entities.setdefault("Decision", []).append(decision)

        relationships.append({
            "type": "MADE_DECISION",
            "source_name": session_name,
            "source_label": "Session",
            "target_name": decision["name"],
            "target_label": "Decision",
        })

        # Link decision to the resolution tool call.
        from create_context_graph.connectors.claude_code_connector import _tool_call_name

        resolution_name = _tool_call_name(resolution_tc)
        relationships.append({
            "type": "RESULTED_IN",
            "source_name": decision["name"],
            "source_label": "Decision",
            "target_name": resolution_name,
            "target_label": "ToolCall",
        })

        traces.append({
            "id": f"claude-decision-{decision_id}",
            "task": f"Decision: Fix error — {err_msg}",
            "outcome": f"Resolved via {resolution_tc['tool_name']}",
            "steps": [
                {
                    "thought": f"Error encountered: {err_msg}",
                    "action": f"{err_tc['tool_name']} failed",
                    "observation": "Error output received",
                },
                {
                    "thought": "Attempting resolution",
                    "action": f"{resolution_tc['tool_name']}: {resolution_tc.get('input_summary', '')[:100]}",
                    "observation": "Resolution attempted",
                },
            ],
        })


def _detect_dependency_changes(
    tool_calls: list[dict[str, Any]],
    session_id: str,
    session_name: str,
    entities: dict[str, list[dict[str, Any]]],
    relationships: list[dict[str, Any]],
    traces: list[dict[str, Any]],
) -> None:
    """Detect package install/add commands as dependency decisions."""
    for i, tc in enumerate(tool_calls):
        if tc["tool_name"] != "Bash":
            continue

        cmd = tc.get("input", {}).get("command", "")
        if not cmd:
            continue

        matched = any(p.search(cmd) for p in _INSTALL_PATTERNS)
        if not matched:
            continue

        decision_id = _make_id(session_id, i, "dependency")
        description = f"Dependency: {cmd[:120]}"

        decision = {
            "name": f"decision-{decision_id}",
            "description": description,
            "timestamp": tc["timestamp"],
            "outcome": "ACCEPTED",
            "confidence": 0.6,
            "category": "dependency",
            "sessionId": session_id,
        }
        entities.setdefault("Decision", []).append(decision)

        relationships.append({
            "type": "MADE_DECISION",
            "source_name": session_name,
            "source_label": "Session",
            "target_name": decision["name"],
            "target_label": "Decision",
        })

        from create_context_graph.connectors.claude_code_connector import _tool_call_name

        relationships.append({
            "type": "RESULTED_IN",
            "source_name": decision["name"],
            "source_label": "Decision",
            "target_name": _tool_call_name(tc),
            "target_label": "ToolCall",
        })

        traces.append({
            "id": f"claude-decision-{decision_id}",
            "task": f"Decision: {description}",
            "outcome": f"Installed: {cmd[:100]}",
            "steps": [
                {
                    "thought": f"Need dependency: {cmd[:150]}",
                    "action": f"Ran: {cmd[:100]}",
                    "observation": "Package installed" if not tc.get("is_error") else "Installation failed",
                },
            ],
        })


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _make_id(session_id: str, index: int, signal_type: str) -> str:
    """Generate a deterministic short ID for a decision."""
    raw = f"{session_id}:{index}:{signal_type}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
