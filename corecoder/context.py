"""Multi-layer context compression.

Claude Code uses a 4-layer strategy:
  1. Complete read-only tool-group projection with source hashes
  2. Microcompact   - LLM-powered summary of old turns (cached)
  3. CONTEXT_COLLAPSE - aggressive compression when nearing hard limit
  4. Autocompact    - periodic background compaction

CoreCoder implements the same idea in 3 layers:
  Layer 1 (tool_group_projection) - retain exact bounded JSON facts and call/reply IDs
  Layer 2 (summarize)   - LLM-powered summary of old conversation
  Layer 3 (hard_collapse) - last resort: drop everything except summary + recent
"""

from __future__ import annotations

import math
import copy
import hashlib
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm import LLM

_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿　-〿＀-･]")
_SYMBOL_RE = re.compile(r"[^\w\s]")


def _approx_tokens(text: str) -> int:
    """Rough token count: CJK ~1.5 chars/token, symbol-dense code ~2.8,
    other prose ~3.4. Flat 3-chars counting reads a Chinese-heavy session at
    half its real size, so compression would trigger too late; round up."""
    cjk = len(_CJK_RE.findall(text))
    rest = len(text) - cjk
    dense = rest > 0 and len(_SYMBOL_RE.findall(text)) / len(text) > 0.25
    return math.ceil(cjk / 1.5) + int(rest / (2.8 if dense else 3.4))


def estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        if m.get("content"):
            total += _approx_tokens(m["content"])
        if m.get("tool_calls"):
            total += _approx_tokens(str(m["tool_calls"]))
    return total


def tool_groups(messages: list[dict]) -> list[tuple[int, int, list[str]]] | None:
    """Return complete assistant/reply groups; reject orphan, duplicate or partial replies."""
    groups = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") == "tool":
            return None
        calls = message.get("tool_calls") or []
        if calls:
            if message.get("role") != "assistant" or not isinstance(calls, list):
                return None
            ids = [call.get("id") for call in calls if isinstance(call, dict)]
            if len(ids) != len(calls) or any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
                return None
            end = index + 1 + len(ids)
            replies = messages[index + 1:end]
            if len(replies) != len(ids) or any(reply.get("role") != "tool" for reply in replies):
                return None
            if sorted(reply.get("tool_call_id") for reply in replies) != sorted(ids):
                return None
            groups.append((index, end, ids))
            index = end
        else:
            index += 1
    return groups


def _read_only(name: str) -> bool:
    return name in {"read_file", "glob", "grep"} or name.startswith(
        ("query_", "get_", "list_", "search_", "read_"))


def _project_json_result(content: str) -> str | None:
    """Lossy only for long prose; preserve every bounded JSON scalar exactly."""
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return None
    fields = {}

    def collect(item, path=""):
        if len(fields) > 64:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                collect(child, f"{path}.{key}" if path else str(key))
        elif isinstance(item, list):
            for number, child in enumerate(item):
                collect(child, f"{path}[{number}]")
        elif item is None or isinstance(item, (str, int, float, bool)):
            fields[path] = item

    collect(value)
    if len(fields) > 64 or any(isinstance(v, str) and len(v) > 500 for v in fields.values()):
        return None
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    compact = json.dumps({"compressed_tool_result": True, "source_sha256": digest,
                          "fields": fields, "untrusted_data": True}, ensure_ascii=False)
    return compact if len(compact) < len(content) else None


_DECISION_KEYS = {"order_id", "query_receipt", "refund_status", "payment", "status",
                  "allow_refund", "eligible_until", "as_of_date", "error", "ok",
                  "rollback_state", "request_id", "change_id"}


def _decision_facts(messages: list[dict]) -> set[tuple[str, str]]:
    facts = set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if (key in _DECISION_KEYS and
                        (item is None or isinstance(item, (str, int, float, bool)))):
                    facts.add((key, json.dumps(item, ensure_ascii=False).strip('"')))
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for message in messages:
        if message.get("role") == "tool":
            try:
                walk(json.loads(message.get("content", "")))
            except (TypeError, ValueError):
                pass
    return facts


class ContextManager:
    def __init__(self, max_tokens: int = 128_000):
        self.max_tokens = max_tokens
        # layer thresholds (fraction of max_tokens)
        self._snip_at = int(max_tokens * 0.50)    # 50% -> snip tool outputs
        self._summarize_at = int(max_tokens * 0.70)  # 70% -> LLM summarize
        self._collapse_at = int(max_tokens * 0.90)   # 90% -> hard collapse
        self.last_event = None
        self.original_groups: dict[str, list[dict]] = {}

    def maybe_compress(self, messages: list[dict], llm: LLM | None = None) -> bool:
        """Apply compression layers as needed. Returns True if any compression happened."""
        self.last_event = None
        groups = tool_groups(messages)
        if groups is None:
            self.last_event = {"status": "skipped_invalid_tool_pairing"}
            return False
        before = estimate_tokens(messages)
        if before <= self._snip_at:
            return False
        candidate = copy.deepcopy(messages)
        layers = []
        archived = {}
        try:
            if before > self._snip_at and self._snip_tool_outputs(candidate, archived):
                layers.append("tool_group_projection")
            current = estimate_tokens(candidate)
            if current > self._summarize_at and len(candidate) > 10 and self._summarize_old(candidate, llm, 8):
                layers.append("history_summary")
            current = estimate_tokens(candidate)
            if current > self._collapse_at and len(candidate) > 4 and self._hard_collapse(candidate, llm):
                layers.append("emergency_summary")
        except Exception:  # compression must never damage a valid conversation
            self.last_event = {"status": "skipped_compression_error", "before_tokens": before}
            return False
        after = estimate_tokens(candidate)
        content = "\n".join(str(message.get("content") or "") for message in candidate)
        missing_facts = [(key, value) for key, value in _decision_facts(messages)
                         if key not in content or value not in content]
        if not layers or after >= before or tool_groups(candidate) is None or missing_facts:
            self.last_event = {"status": "skipped_no_safe_reduction", "before_tokens": before}
            return False
        messages[:] = candidate
        self.original_groups.update(archived)
        self.last_event = {"status": "compressed", "layers": layers, "before_tokens": before,
                           "after_tokens": after, "archived_group_hashes": sorted(archived)}
        return True

    @staticmethod
    def _snip_tool_outputs(messages: list[dict], archived=None) -> bool:
        """Project complete, old read-only tool groups; keep call/reply IDs intact."""
        groups = tool_groups(messages)
        if groups is None:
            return False
        changed = False
        for start, end, _ in groups:
            if end > max(0, len(messages) - 4):
                continue
            calls = messages[start]["tool_calls"]
            names = [call.get("function", {}).get("name", "") for call in calls]
            if not names or not all(_read_only(name) for name in names):
                continue
            replies = messages[start + 1:end]
            if not any(len(reply.get("content", "")) > 1500 for reply in replies):
                continue
            projected = [_project_json_result(reply.get("content", ""))
                         if len(reply.get("content", "")) > 1500 else reply.get("content", "")
                         for reply in replies]
            if any(item is None for item in projected):
                continue
            source = copy.deepcopy(messages[start:end])
            key = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if archived is not None:
                archived[key] = source
            for reply, content in zip(replies, projected):
                reply["content"] = content
            changed = True
        return changed

    @staticmethod
    def _safe_split(messages: list[dict], keep_recent: int) -> int:
        """Index where the kept tail should start.

        Walk the boundary back so a 'tool' result is never separated from the
        assistant message whose tool_calls produced it - an orphaned tool
        message has no preceding tool_calls and OpenAI-compatible APIs reject it.
        """
        split = max(0, len(messages) - keep_recent)
        while split > 0 and messages[split].get("role") == "tool":
            split -= 1
        return split

    def _summarize_old(self, messages: list[dict], llm: LLM | None,
                       keep_recent: int = 8) -> bool:
        """Layer 2: Summarize old conversation, keep recent messages intact."""
        if len(messages) <= keep_recent:
            return False

        split = self._safe_split(messages, keep_recent)
        old = messages[:split]
        tail = messages[split:]
        if not old:
            return False
        ledger = self._evidence_ledger(old)
        if ledger is None:
            return False

        summary = self._get_summary(old, llm)
        source_hash = hashlib.sha256(json.dumps(old, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

        messages.clear()
        messages.append({
            "role": "user",
            "content": f"[Context compressed; source_sha256={source_hash}]\n{summary}\n"
                       f"[Exact evidence ledger]\n{ledger}",
        })
        messages.append({
            "role": "assistant",
            "content": "Got it, I have the context from our earlier conversation.",
        })
        messages.extend(tail)
        return True

    def _hard_collapse(self, messages: list[dict], llm: LLM | None) -> bool:
        """Layer 3: Emergency compression. Keep only last 4 messages + summary."""
        split = self._safe_split(messages, 4 if len(messages) > 4 else 2)
        tail = messages[split:]
        old = messages[:split]
        ledger = self._evidence_ledger(old)
        if not old or ledger is None:
            return False
        summary = self._get_summary(old, llm)
        source_hash = hashlib.sha256(json.dumps(old, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

        messages.clear()
        messages.append({
            "role": "user",
            "content": f"[Hard context reset; source_sha256={source_hash}]\n{summary}\n"
                       f"[Exact evidence ledger]\n{ledger}",
        })
        messages.append({
            "role": "assistant",
            "content": "Context restored. Continuing from where we left off.",
        })
        messages.extend(tail)
        return True

    @staticmethod
    def _evidence_ledger(messages: list[dict]) -> str | None:
        """Keep bounded exact tool facts and recent user turns, or refuse lossy collapse."""
        rows = []
        calls = {}
        for message in messages:
            for call in message.get("tool_calls") or []:
                calls[call.get("id")] = call.get("function", {}).get("name", "unknown")
            if message.get("role") != "tool":
                continue
            raw = message.get("content", "")
            if len(raw) > 1500:
                raw = _project_json_result(raw)
            if raw is None or len(raw) > 2000:
                return None
            rows.append(json.dumps({"tool_call_id": message.get("tool_call_id"),
                                    "tool_name": calls.get(message.get("tool_call_id"), "unknown"),
                                    "result": raw}, ensure_ascii=False))
        user_turns = [m.get("content", "") for m in messages if m.get("role") == "user"][-3:]
        if any(len(turn) > 2000 for turn in user_turns):
            return None
        rows += [f"Recent user turn: {turn}" for turn in user_turns]
        ledger = "\n".join(rows) or "(no tool evidence in compressed span)"
        return ledger if len(ledger) <= 8000 else None

    def _get_summary(self, messages: list[dict], llm: LLM | None) -> str:
        """Generate summary via LLM or fallback to extraction."""
        flat = self._flatten(messages)

        if llm:
            try:
                resp = llm.chat(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Compress this conversation into a brief summary. "
                                "Preserve: file paths edited, key decisions made, "
                                "errors encountered, current task state. "
                                "Drop: verbose command output, code listings, "
                                "redundant back-and-forth."
                            ),
                        },
                        {"role": "user", "content": flat[:15000]},
                    ],
                )
                return resp.content
            except Exception:  # noqa: BLE001, S110
                # summarization is best-effort; fall back to extraction below
                pass

        # fallback: extract key lines
        return self._extract_key_info(messages)

    @staticmethod
    def _flatten(messages: list[dict]) -> str:
        parts = []
        for m in messages:
            role = m.get("role", "?")
            text = m.get("content", "") or ""
            if text:
                parts.append(f"[{role}] {text[:400]}")
        return "\n".join(parts)

    @staticmethod
    def _extract_key_info(messages: list[dict]) -> str:
        """Fallback: extract file paths, errors, and decisions without LLM."""
        import re
        files_seen = set()
        errors = []

        for m in messages:
            text = m.get("content", "") or ""
            # extract file paths
            for match in re.finditer(r'[\w./\-]+\.\w{1,5}', text):
                files_seen.add(match.group())
            # extract error lines
            for line in text.splitlines():
                if "error" in line.lower():
                    errors.append(line.strip()[:150])

        parts = []
        if files_seen:
            parts.append(f"Files touched: {', '.join(sorted(files_seen)[:20])}")
        if errors:
            parts.append(f"Errors seen: {'; '.join(errors[:5])}")
        return "\n".join(parts) or "(no extractable context)"
