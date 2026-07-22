"""Subscription-backed alternative to ConsequencePredictionClient: shells
out to the `claude` CLI (authenticated via CLAUDE_CODE_OAUTH_TOKEN, from
`claude setup-token`) instead of calling the Anthropic Python SDK
directly. Same public interface (model, knowledge_cutoff, is_forward_safe,
analyze) so engine.prediction.pipeline never needs to know which backend
is active -- see engine.prediction.factory.

STATUS as of 2026-07-21 (verified live, real token, real headlines --
see JOURNAL.md for the full account): the transport layer works --
subprocess invocation, envelope parsing, and OAuth auth via
CLAUDE_CODE_OAUTH_TOKEN are all confirmed correct. Two real problems
surfaced that are NOT yet solved:

1. Running with cwd inside this repo leaks project context (Claude Code
   auto-discovers CLAUDE.md/.env) and the model responds *about this
   codebase* instead of analyzing the headline. Fixed by running with
   cwd set to a neutral directory outside the repo (see analyze() below).
2. UNRESOLVED: even with a neutral cwd and a completely ordinary,
   unambiguous headline ("Fed raises rates 0.25pp"), `claude -p` responds
   with a clarifying question about user intent instead of following the
   system prompt + --json-schema constraint. This reproduced on both an
   evocative headline and a mundane one, so it isn't about sensitive
   content -- it looks like Claude Code's own intent-classification layer
   (see `claude auto-mode`) intercepts short, non-coding-task-shaped
   prompts before the system prompt gets to drive behavior the way the
   raw Messages API does. --json-schema constrains the output *if* the
   model answers directly; it does not force a direct answer.

Net effect: this client will very likely raise ClaudeCLIError (via the
"result text was not valid JSON" path below) on most or all real
headlines right now, not crash the service (predict-loop's per-cycle
exception handling logs and continues) but also not produce real
predictions. Left in place because it's the credential path actually
configured, and the failure mode is safe (loud, logged, non-destructive)
rather than silent -- but do not read "the service is running" as "this
backend works." Needs either a different invocation approach (a
--permission-mode or prompt structure that suppresses the intent
classifier) or falling back to ANTHROPIC_API_KEY (engine.prediction.client,
unaffected by any of this -- it calls the Messages API directly).
"""

from __future__ import annotations

import json
import os
import tempfile
import shutil
import subprocess
from datetime import date, datetime

from pydantic import BaseModel

from engine.config.settings import Settings
from engine.prediction.client import (
    HYPOTHESIS_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    PredictionConfigError,
    build_hypothesis_prompt,
    build_prompt,
    is_forward_safe,
    parse_knowledge_cutoff,
)
from engine.prediction.schema import ConsequenceAnalysis, HypothesisEstimate

_CLAUDE_TIMEOUT_SECONDS = 180


class ClaudeCLIError(RuntimeError):
    pass


def _parse_cli_output(stdout: str, schema_cls: type[BaseModel]) -> BaseModel:
    """`claude -p --output-format json` wraps the model's final text in an
    envelope (`{"result": "...", ...}`); with --json-schema that text is
    itself the schema-validated JSON. Handles both a JSON-string result
    (documented shape) and an already-parsed dict (defensive, in case
    --json-schema changes the envelope to embed structured data directly).
    Generic over schema_cls since both the reactive (ConsequenceAnalysis)
    and anticipatory (HypothesisEstimate) calls share this same envelope
    shape -- only the payload schema differs."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCLIError(f"claude CLI did not return valid JSON envelope: {stdout[:500]!r}") from exc

    result = envelope.get("result") if isinstance(envelope, dict) else None
    if result is None:
        raise ClaudeCLIError(f"claude CLI JSON envelope had no 'result' field: {envelope!r}")

    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except json.JSONDecodeError as exc:
            # Observed in practice, not just a theoretical case: the model
            # can respond with plain prose (e.g. a clarifying question)
            # instead of schema JSON even with --json-schema set, when a
            # headline reads as ambiguous/sensitive out of context (see
            # JOURNAL.md 2026-07-21). --json-schema constrains the shape
            # *if* the model answers directly; it doesn't force it to.
            raise ClaudeCLIError(
                f"claude CLI's result text was not valid JSON (model likely didn't answer "
                f"directly -- see JOURNAL.md 2026-07-21): {result[:500]!r}"
            ) from exc
    else:
        payload = result
    return schema_cls.model_validate(payload)


class ClaudeCLIPredictionClient:
    def __init__(self, settings: Settings):
        if not settings.claude_code_oauth_token:
            raise PredictionConfigError(
                "CLAUDE_CODE_OAUTH_TOKEN is not set -- refusing to construct a prediction "
                "client rather than silently doing nothing."
            )
        self.model = settings.anthropic_model
        self.knowledge_cutoff: date = parse_knowledge_cutoff(settings)
        self._env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": settings.claude_code_oauth_token}
        # Bare "claude" isn't reliably resolvable via subprocess.run's argv[0]
        # lookup on Windows (npm installs a .CMD shim there); shutil.which
        # does the same PATHEXT-aware resolution a shell would. Resolved
        # once here so a missing CLI fails fast at construction, not on the
        # first analyze() call.
        claude_path = shutil.which("claude")
        if not claude_path:
            raise ClaudeCLIError("claude CLI not found on PATH -- install with `npm install -g @anthropic-ai/claude-code`")
        self._claude_path = claude_path

    def is_forward_safe(self, decision_timestamp: datetime) -> bool:
        return is_forward_safe(self.knowledge_cutoff, decision_timestamp)

    def analyze(
        self,
        headline: str,
        tracked_symbols: list[str],
        past_cases: list[str] | None = None,
    ) -> ConsequenceAnalysis:
        user_prompt = build_prompt(headline, tracked_symbols, past_cases or [])
        schema = ConsequenceAnalysis.model_json_schema()
        result = subprocess.run(
            [
                self._claude_path,
                "-p",
                user_prompt,
                "--append-system-prompt",
                SYSTEM_PROMPT,
                "--model",
                self.model,
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(schema),
                "--no-session-persistence",
            ],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_TIMEOUT_SECONDS,
            env=self._env,
            check=False,
            # Neutral cwd, deliberately outside the repo: running from
            # inside it lets Claude Code auto-discover CLAUDE.md/.env and
            # respond about this codebase instead of the headline. See
            # module docstring for the (separate, unresolved) issue this
            # does NOT fix.
            cwd=tempfile.gettempdir(),
        )
        if result.returncode != 0:
            raise ClaudeCLIError(
                f"claude CLI exited {result.returncode}: {result.stderr[:2000]}"
            )
        return _parse_cli_output(result.stdout, ConsequenceAnalysis)

    def estimate_hypothesis(self, question: str, description: str = "") -> HypothesisEstimate:
        user_prompt = build_hypothesis_prompt(question, description)
        schema = HypothesisEstimate.model_json_schema()
        result = subprocess.run(
            [
                self._claude_path,
                "-p",
                user_prompt,
                "--append-system-prompt",
                HYPOTHESIS_SYSTEM_PROMPT,
                "--model",
                self.model,
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(schema),
                "--no-session-persistence",
            ],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_TIMEOUT_SECONDS,
            env=self._env,
            check=False,
            cwd=tempfile.gettempdir(),
        )
        if result.returncode != 0:
            raise ClaudeCLIError(
                f"claude CLI exited {result.returncode}: {result.stderr[:2000]}"
            )
        return _parse_cli_output(result.stdout, HypothesisEstimate)
