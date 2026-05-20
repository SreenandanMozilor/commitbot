"""
LLM provider abstraction for the agentic commitment classifier.

Two providers ship in-tree:

  * AnthropicProvider — production. Uses the official `anthropic` SDK,
    batches a list of messages into a single Messages API call, prefills
    the assistant turn with `[` so Claude returns a well-formed JSON list
    we can parse without prose. Configured via ANTHROPIC_API_KEY and
    AGENT_MODEL.

  * StubProvider — deterministic offline fallback. Matches a handful of
    high-precision commitment patterns ("I'll ...", "I will ...", "I can
    pick up ...") and emits ClassifiedCandidate objects with confidence
    in the 0.6–0.95 range. Used automatically whenever the configured
    provider is unavailable (no API key, network down, SDK missing).
    Good enough for tests + dev; not a real classifier.

Callers go through `get_provider()` — they don't import either
implementation directly. That keeps the agent service ignorant of which
backend it's talking to.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol, Sequence

from app.config import get_settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data shapes shared across providers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HarvestedMessage:
    """A Slack message the agent is about to look at.

    `id` is an opaque correlation key (the AgentMessageBuffer row id) — the
    provider returns it verbatim so the agent service can match verdicts
    back to buffer rows without us trusting LLM-generated identifiers.
    """
    id: str
    text: str
    sent_at: Optional[datetime] = None
    channel_id: Optional[str] = None


@dataclass
class ClassifiedCandidate:
    """A provider's verdict on a single message."""
    message_id: str
    is_commitment: bool
    confidence: float
    rationale: str = ""
    # Suggested deadline as an ISO-8601 string, if the model could infer
    # one ("by Friday EOD" → next-Friday 17:00 in the user's tz). NULL
    # means the model declined to guess; the agent service then leaves
    # the deadline unset (the user can edit it from Home).
    deadline_hint: Optional[str] = None
    # Free-text recipient hints ("priya", "the design team"). The agent
    # service decides how to map these onto Slack user IDs — usually we
    # just drop them on the floor and rely on @-mention extraction from
    # the original message text.
    recipient_hints: list[str] = field(default_factory=list)


class LLMProvider(Protocol):
    """Minimal protocol every classifier implementation satisfies."""
    name: str

    def classify(
        self,
        messages: Sequence[HarvestedMessage],
        *,
        now: Optional[datetime] = None,
        tz: Optional[str] = None,
    ) -> list[ClassifiedCandidate]:
        ...


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

# Kept terse: this prompt gets re-sent for every batch, so verbosity is
# pure cost. The examples are the load-bearing part — they teach the
# model the false-positive shape (vague intent, hypothetical, past tense,
# third-party).
SYSTEM_PROMPT = """\
You are a commitment-detection classifier for a personal productivity bot.

You will be given Slack messages the user typed. For each one, decide whether
the SENDER is making a personal commitment — a clear statement that THEY
(the sender) will do a specific thing.

COUNT AS A COMMITMENT
  - "I'll send the spec by Friday"
  - "I can pick up that bug"
  - "I'll review your PR tonight"
  - "Will get back to you with the report tomorrow"
  - "Remind me to send the report tomorrow"        (self-directed reminder)
  - "Don't let me forget to call the vendor"       (self-directed reminder)
  - "I should remember to file the expense report" (self-directed reminder)

Self-directed reminders ("remind me to X", "don't let me forget to X",
"I should remember to X") DO count — the sender is committing to their
future self with this bot as the reminder mechanism. Treat them the same
as first-person promises: extract the action and deadline if any.

DO NOT COUNT
  - "I'll think about it"               (too vague to track)
  - "Maybe I'll grab lunch"             (low-conviction)
  - "John should fix that"              (about someone else)
  - "I'll buy you lunch IF X happens"   (hypothetical)
  - "I was going to send it yesterday"  (past, not a new promise)
  - "We need to ship this"              (intent, not personal commitment)

For each message return JSON:
  {
    "message_id": "<the id you were given, verbatim>",
    "is_commitment": true | false,
    "confidence": 0.0..1.0,
    "rationale": "<one short sentence, <= 200 chars>",
    "deadline_hint": "ISO-8601 datetime with offset, or null",
    "recipient_hints": ["names mentioned as the audience", ...]
  }

Deadline extraction rules (important):
  - Resolve relative times against the "Current time" given in the user prompt.
    "tomorrow" → next calendar day; "tonight" → today ~21:00; "by Friday" →
    next upcoming Friday at 17:00; "end of day" / "EOD" → today 17:00;
    "next week" → next Monday 17:00.
  - Emit deadline_hint in the sender's local timezone (also given in the user
    prompt) as a full ISO-8601 string WITH offset, e.g. "2026-05-21T17:00:00+05:30".
  - The deadline MUST be strictly in the future relative to Current time.
  - If the message has no temporal cue at all, return deadline_hint: null.

Confidence calibration:
  >= 0.90 — unambiguous first-person promise with a clear action
  0.75..0.90 — clear commitment, slightly fuzzy action or deadline
  0.50..0.75 — leaning commitment but real ambiguity; rationale should explain
  <  0.50 — likely NOT a commitment; is_commitment should be false

Return ONLY a JSON array. No prose, no markdown fences."""


def _format_user_prompt(
    messages: Sequence[HarvestedMessage],
    *,
    now: Optional[datetime] = None,
    tz: Optional[str] = None,
) -> str:
    # Use json.dumps to escape user-controlled text. Otherwise a message
    # containing `"` or a fabricated JSON object could close the prompt's
    # quoted string and inject prompt instructions or fake verdicts —
    # since the agent classifies the SENDER's own messages, the blast
    # radius is bounded to the sender's own captures, but it would still
    # let a malicious user auto-create commitments for themselves with
    # arbitrary confidence and rationale.
    header: list[str] = []
    if now is not None:
        header.append(f"Current time: {now.isoformat(timespec='seconds')}")
    if tz:
        header.append(f"Sender timezone: {tz}")
    lines: list[str] = []
    if header:
        lines.append(" | ".join(header))
        lines.append("")
    lines.append("Classify the following messages:\n")
    for m in messages:
        text = (m.text or "").replace("\n", " ").strip()[:500]
        lines.append(
            f"- id: {json.dumps(m.id)}  text: {json.dumps(text)}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stub provider — deterministic, no network
# ---------------------------------------------------------------------------

# High-precision commitment patterns. The order matters: more specific
# patterns earlier so the highest-confidence match wins.
_COMMITMENT_PATTERNS: list[tuple[re.Pattern, float, str]] = [
    (re.compile(r"\bi['’]ll\b\s+\w+", re.I), 0.88,
     "First-person future ('I'll …')"),
    (re.compile(r"\bi\s+will\s+\w+", re.I), 0.86,
     "First-person future ('I will …')"),
    (re.compile(r"\bremind\s+me\s+to\s+\w+", re.I), 0.86,
     "Self-directed reminder ('remind me to …')"),
    (re.compile(r"\bdon['’]?t\s+let\s+me\s+forget\s+(to|about)\b", re.I), 0.85,
     "Self-directed reminder ('don't let me forget …')"),
    (re.compile(r"\bi\s+should\s+(remember|not\s+forget)\s+to\b", re.I), 0.82,
     "Self-directed reminder ('I should remember to …')"),
    (re.compile(r"\bi\s+can\s+(pick\s+up|take|handle|do|own|cover)\b", re.I),
     0.82, "First-person uptake ('I can pick up …')"),
    (re.compile(r"\b(will\s+(get|send|have|share|do)|getting\s+\w+\s+to\s+you)\b", re.I),
     0.78, "Future tense with concrete verb"),
]

# Anti-patterns. If any of these match the message wins NOT-a-commitment
# regardless of pattern matches above.
_REJECT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bi['’]ll\s+(think|consider|see|let\s+you\s+know)\b", re.I),
     "Vague intent ('I'll think about it')"),
    (re.compile(r"\bmaybe\b|\bperhaps\b|\bmight\b", re.I),
     "Low-conviction qualifier"),
    (re.compile(r"\bif\b.+\bthen\b|\bif\b.+,", re.I),
     "Hypothetical conditional"),
    (re.compile(r"\bi\s+was\s+going\s+to\b|\bi\s+had\s+\w+ed\b", re.I),
     "Past tense, not a new promise"),
]

_DEADLINE_TOKENS = (
    "today", "tomorrow", "tonight", "this week", "next week", "monday",
    "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "eod", "by ", "before ",
)


class StubProvider:
    """Deterministic heuristic classifier. Use in tests or as a fallback
    when no real provider is configured."""
    name = "stub"

    def classify(
        self,
        messages: Sequence[HarvestedMessage],
        *,
        now: Optional[datetime] = None,
        tz: Optional[str] = None,
    ) -> list[ClassifiedCandidate]:
        out: list[ClassifiedCandidate] = []
        for m in messages:
            text = (m.text or "").strip()
            if not text:
                out.append(ClassifiedCandidate(
                    message_id=m.id, is_commitment=False, confidence=0.0,
                    rationale="empty message",
                ))
                continue

            # Anti-patterns short-circuit.
            rejected = next(
                ((r, why) for r, why in _REJECT_PATTERNS if r.search(text)),
                None,
            )
            if rejected is not None:
                out.append(ClassifiedCandidate(
                    message_id=m.id, is_commitment=False, confidence=0.2,
                    rationale=rejected[1],
                ))
                continue

            # Find the strongest commitment pattern that matches.
            best: Optional[tuple[float, str]] = None
            for pat, conf, why in _COMMITMENT_PATTERNS:
                if pat.search(text):
                    if best is None or conf > best[0]:
                        best = (conf, why)
            if best is None:
                out.append(ClassifiedCandidate(
                    message_id=m.id, is_commitment=False, confidence=0.1,
                    rationale="No first-person future / uptake pattern",
                ))
                continue

            conf, why = best
            # Boost confidence slightly when the message also includes a
            # deadline token — a concrete time anchor is a strong signal.
            lower = text.lower()
            if any(tok in lower for tok in _DEADLINE_TOKENS):
                conf = min(0.95, conf + 0.05)

            out.append(ClassifiedCandidate(
                message_id=m.id,
                is_commitment=True,
                confidence=conf,
                rationale=why,
                deadline_hint=None,  # stub doesn't try to parse dates
                recipient_hints=[],
            ))
        return out


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------

class AnthropicProvider:
    """Production classifier. One Messages API call per batch."""
    name = "anthropic"

    def __init__(self, *, api_key: str, model: str):
        if not api_key:
            raise ValueError("AnthropicProvider requires an API key.")
        # Import lazily so the dep is only required when the provider is
        # actually used (tests + stub-only dev runs don't need it).
        try:
            import anthropic  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "`anthropic` package is not installed. "
                "Run `pip install -r requirements.txt`."
            ) from e
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def classify(
        self,
        messages: Sequence[HarvestedMessage],
        *,
        now: Optional[datetime] = None,
        tz: Optional[str] = None,
    ) -> list[ClassifiedCandidate]:
        if not messages:
            return []

        user_prompt = _format_user_prompt(messages, now=now, tz=tz)
        try:
            resp = self._client.messages.create(
                model=self._model,
                # ~80 tokens per verdict object × _MAX_BATCH_SIZE (30) ≈
                # 2400 tokens, plus a little JSON framing. 2k truncated
                # full batches mid-array; 4k leaves headroom so a verbose
                # rationale doesn't tip into JSON repair territory.
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": user_prompt},
                    # Prefill with `[` so the model continues a JSON array —
                    # eliminates the "Sure, here you go: ```json [..." style
                    # opener that breaks parse.
                    {"role": "assistant", "content": "["},
                ],
            )
        except Exception:
            log.exception("Anthropic classify failed; treating batch as no-op")
            return []

        # `resp.content` is a list of content blocks. For this prompt we
        # expect a single text block.
        try:
            raw_tail = "".join(
                block.text for block in resp.content if getattr(block, "type", None) == "text"
            )
        except Exception:
            log.exception("Unexpected Anthropic response shape: %r", resp)
            return []

        # Glue the prefill `[` back on and parse. If the model ran out of
        # tokens we may have a truncated JSON; try a best-effort recovery
        # by clipping to the last `}` plus a closing bracket.
        body = "[" + raw_tail.strip()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            cleaned = _attempt_json_repair(body)
            if cleaned is None:
                log.warning("Anthropic returned unparseable JSON: %r", body[:500])
                return []
            parsed = cleaned

        out: list[ClassifiedCandidate] = []
        valid_ids = {m.id for m in messages}
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            mid = entry.get("message_id")
            if mid not in valid_ids:
                # The model hallucinated an id we never sent. Skip — we
                # only trust verdicts we can match back to buffer rows.
                continue
            try:
                conf = float(entry.get("confidence") or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            conf = max(0.0, min(1.0, conf))
            out.append(ClassifiedCandidate(
                message_id=mid,
                is_commitment=bool(entry.get("is_commitment")),
                confidence=conf,
                rationale=(entry.get("rationale") or "")[:280],
                deadline_hint=entry.get("deadline_hint"),
                recipient_hints=[
                    str(h) for h in (entry.get("recipient_hints") or [])
                    if isinstance(h, str)
                ][:5],
            ))
        return out


def _attempt_json_repair(body: str) -> Optional[list]:
    """Best-effort: trim trailing garbage and try to close the array.

    Only handles the common truncation case ("ran out of tokens mid-object").
    Returns the parsed list or None if no repair was possible.
    """
    last_brace = body.rfind("}")
    if last_brace == -1:
        return None
    repaired = body[: last_brace + 1] + "]"
    try:
        out = json.loads(repaired)
        return out if isinstance(out, list) else None
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_provider() -> LLMProvider:
    """Return a configured provider, falling back to StubProvider on any
    setup error so the agent path is always callable."""
    s = get_settings()
    provider_name = (s.agent_provider or "").strip().lower()

    if provider_name == "anthropic":
        if not s.anthropic_api_key:
            log.info("No ANTHROPIC_API_KEY set — agent will use StubProvider.")
            return StubProvider()
        try:
            return AnthropicProvider(
                api_key=s.anthropic_api_key, model=s.agent_model,
            )
        except Exception:
            log.exception("AnthropicProvider construction failed — using StubProvider")
            return StubProvider()

    if provider_name == "stub":
        return StubProvider()

    log.warning("Unknown AGENT_PROVIDER=%r — using StubProvider", provider_name)
    return StubProvider()
