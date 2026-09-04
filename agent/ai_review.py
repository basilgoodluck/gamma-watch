"""AI review (veto/adjust) gate for step05's sized candidates.

Provider: Featherless AI (OpenAI SDK-compatible), model
Qwen/Qwen3.8-Flash-Next (doc/visual_reasoning_redesign.md item 4, switched
from Qwen/Qwen3-32B) - not Anthropic or OpenAI. This is a paid, credit-limited
provider ($25 hackathon sponsor budget), so this module enforces the doc's
four cost-discipline rules before ever making a network call:
1. Batch all surviving candidates into ONE call, never one call per candidate.
2. Deterministic pre-filter (>=1 candidate passed risk sizing AND regime
   confidence >= 0.5) - skip the call entirely otherwise, no charge.
3. Spend ceiling: once tracked cumulative spend crosses 80% of the $25
   budget, stop calling the provider and fail open, same code path as an
   API error.
4. Cadence is the caller's responsibility (step09's run_cycle, 5-15 min) -
   this module has no polling loop of its own.

Reused in *shape* from the old codebase's ai_advisor.py: structured system
prompt, forced JSON-only output, clamped confidence_adjustment, fail-open on
error. Adapted here to review an array of candidates in one call instead of
one call per trade.

Cost tracking is a PROXY, not real billing (Featherless's real-time spend
isn't queried here, per the doc's own allowance) - COST_PER_REQUEST_ESTIMATE
below is a rough placeholder, not a verified Featherless price. Correct it
via the env var if you have real per-request cost data.
"""
import json
import os

from openai import AsyncOpenAI

from agent.config import FEATHERLESS_API_KEY
from agent.db import get_pool
from agent.logs import log_event

FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"
FEATHERLESS_MODEL = "Qwen/Qwen3.8-Flash-Next"  # doc/visual_reasoning_redesign.md item 4

REGIME_CONFIDENCE_THRESHOLD = 0.5
BUDGET_TOTAL = 25.0
BUDGET_CEILING_PCT = 0.8
COST_PER_REQUEST_ESTIMATE = float(os.environ.get("FEATHERLESS_EST_COST_PER_REQUEST", "0.02"))

CONFIDENCE_ADJUSTMENT_MIN = -0.2
CONFIDENCE_ADJUSTMENT_MAX = 0.1
# doc/visual_reasoning_redesign.md item 4: Qwen3.8-Flash-Next is a reasoning
# model - it spends a hidden chain-of-thought pass (returned separately as
# message.reasoning, not used here) before emitting the actual JSON in
# message.content, and BOTH count against max_tokens. Confirmed directly
# against the real prompt: the old 800-token budget (sized for the previous
# non-reasoning Qwen3-32B) sometimes left zero tokens for the actual answer
# (finish_reason="length", empty content, JSON parse failure) - 3000 leaves
# comfortable headroom (a real call used 440 total).
MAX_RESPONSE_TOKENS = 3000

SPEND_SCOPE = "featherless"

SYSTEM_PROMPT = """
You are a disciplined options trading risk officer reviewing candidate trades
before execution. You will receive the current market regime, IV rank, and
one or more candidate options strategies with their legs and sizing. Decide
whether to approve or veto each candidate independently.

Rules:
- Be concise. Each reason must be one sentence, substantive (name the actual
  regime/IV/strikes/risk-reward reasoning), never a generic phrase like "ok"
  or "market uncertainty".
- confidence_adjustment is a float between -0.2 and +0.1 per candidate.
  Negative = you see weakness. Positive = you see extra confluence.
- Only veto (approve=false) for a clearly stated reason: internal
  inconsistency between the regime/IV inputs and the strategy chosen (e.g. an
  iron condor selected while IV rank is actually low, or a directional bet
  against the stated trend direction), or an irrational risk/reward ratio
  (e.g. capped, small max_gain against a much larger max_loss with no
  offsetting edge). Do not veto just because the market is uncertain -
  uncertainty is normal.
- Respond ONLY with a valid JSON array, no markdown, no explanation outside
  the JSON. One object per candidate, in the same order you received them:
  [{"candidate_id": "...", "approve": true, "reason": "...", "confidence_adjustment": 0.0}]
""".strip()


def _client() -> AsyncOpenAI:
    # doc/loop_and_ui_fixes.md: the openai SDK's own default is
    # Timeout(connect=5, read=600, write=600, pool=600) with max_retries=2 -
    # up to ~30 minutes worst case for one stalled Featherless call, which
    # was very likely the dominant cause of the decision loop's repeated
    # 20-30 minute freezes (the outer asyncio.wait_for in web_api/server.py
    # has its own 180s cycle timeout, but a request already retrying inside
    # the SDK's own loop doesn't reliably surface that cancellation quickly).
    # Bounded here well under both that outer timeout and the 600s cycle
    # interval, so a stalled call fails fast and the module's own
    # fail-open except-block in review_candidates() handles it normally.
    return AsyncOpenAI(api_key=FEATHERLESS_API_KEY, base_url=FEATHERLESS_BASE_URL, timeout=25.0, max_retries=1)


async def _load_spend() -> dict:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT cumulative_total, request_count FROM inference_spend WHERE scope = $1", SPEND_SCOPE)
    if row is None:
        return {"cumulative_spend": 0.0, "request_count": 0}
    return {"cumulative_spend": float(row["cumulative_total"]), "request_count": row["request_count"]}


async def _record_spend(cost: float) -> dict:
    """Atomic upsert-increment in one round trip (per item 11's spec: single
    running-total row per scope, cumulative_total = cumulative_total + $1) -
    no read-modify-write race like the old file version had."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """INSERT INTO inference_spend (scope, cumulative_total, request_count, updated_at)
           VALUES ($1, $2, 1, now())
           ON CONFLICT (scope) DO UPDATE SET
               cumulative_total = inference_spend.cumulative_total + EXCLUDED.cumulative_total,
               request_count = inference_spend.request_count + 1,
               updated_at = now()
           RETURNING cumulative_total, request_count""",
        SPEND_SCOPE,
        cost,
    )
    return {"cumulative_spend": round(float(row["cumulative_total"]), 4), "request_count": row["request_count"]}


async def _budget_ceiling_reached() -> tuple[bool, float]:
    cumulative = (await _load_spend())["cumulative_spend"]
    return cumulative >= BUDGET_TOTAL * BUDGET_CEILING_PCT, cumulative


def _fail_open(candidates_with_ids: list[tuple[str, dict]], reason: str) -> list[dict]:
    return [
        {"candidate_id": cid, "approve": True, "reason": reason, "confidence_adjustment": 0.0}
        for cid, _ in candidates_with_ids
    ]


def _build_prompt(candidates_with_ids: list[tuple[str, dict]], regime_result: dict, iv_result: dict, account_state: dict) -> str:
    candidate_blocks = []
    for cid, c in candidates_with_ids:
        candidate_blocks.append(
            f"candidate_id={cid}\n"
            f"  type={c.get('type')} direction={c.get('direction')} shape={c.get('shape', 'n/a')}\n"
            f"  legs={c.get('legs')}\n"
            f"  contracts={c.get('contracts')} max_loss=${c.get('max_loss')} max_gain={c.get('max_gain')}\n"
            f"  low_confidence={c.get('low_confidence')}"
        )
    candidates_block = "\n\n".join(candidate_blocks)

    return f"""
--- REGIME ---
regime={regime_result.get('regime')} confidence={regime_result.get('confidence'):.4f}
trend_direction={regime_result.get('trend_direction')}
probabilities={regime_result.get('probabilities')}

--- IV RANK ---
atm_iv={iv_result.get('atm_iv')} iv_rank={iv_result.get('iv_rank')} cold_start={iv_result.get('cold_start')}

--- ACCOUNT ---
equity=${account_state.get('equity')} open_positions={account_state.get('open_positions_count')}

--- CANDIDATES ---
{candidates_block}

Review each candidate and return the JSON array.
""".strip()


async def review_candidates(
    candidates: list[dict], regime_result: dict, iv_result: dict, account_state: dict, trace_id: str | None = None
) -> dict:
    """Returns {"verdicts": list[dict], "skipped": bool, "skip_reason": str|None}."""
    if not candidates:
        result = {"verdicts": [], "skipped": True, "skip_reason": "no candidates to review"}
        await log_event("skipped_ai_review", {"candidates": candidates}, result, result["skip_reason"], trace_id=trace_id)
        return result

    candidates_with_ids = [(f"{c.get('type', 'candidate')}_{i}", c) for i, c in enumerate(candidates)]

    any_passed_risk = any(c.get("passes_risk") for c in candidates)
    regime_confidence = regime_result.get("confidence", 0.0)
    regime_ok = regime_confidence >= REGIME_CONFIDENCE_THRESHOLD

    if not any_passed_risk:
        skip_reason = "no candidates passed risk sizing"
    elif not regime_ok:
        skip_reason = f"regime confidence {regime_confidence:.2f} below threshold ({REGIME_CONFIDENCE_THRESHOLD})"
    else:
        skip_reason = None

    if skip_reason is not None:
        result = {"verdicts": [], "skipped": True, "skip_reason": skip_reason}
        await log_event(
            "skipped_ai_review",
            {"candidates": candidates, "regime_confidence": regime_confidence},
            result,
            skip_reason,
            trace_id=trace_id,
        )
        return result

    ceiling_reached, cumulative_spend = await _budget_ceiling_reached()
    if ceiling_reached:
        reason = "AI review budget ceiling reached — falling back to auto-approve"
        verdicts = _fail_open(candidates_with_ids, reason)
        result = {"verdicts": verdicts, "skipped": False, "skip_reason": None, "model": None}
        await log_event(
            "ai_review_budget_ceiling",
            {"candidates": candidates, "cumulative_spend": cumulative_spend, "budget_total": BUDGET_TOTAL},
            result,
            reason,
            trace_id=trace_id,
        )
        return result

    prompt = _build_prompt(candidates_with_ids, regime_result, iv_result, account_state)

    try:
        response = await _client().chat.completions.create(
            model=FEATHERLESS_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=MAX_RESPONSE_TOKENS,
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("[") : raw.rfind("]") + 1]
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) != len(candidates_with_ids):
            raise ValueError(f"expected {len(candidates_with_ids)} verdicts, got {data!r}")

        verdicts = []
        for (cid, _), item in zip(candidates_with_ids, data):
            adj = max(CONFIDENCE_ADJUSTMENT_MIN, min(CONFIDENCE_ADJUSTMENT_MAX, float(item.get("confidence_adjustment", 0.0))))
            verdicts.append(
                {
                    "candidate_id": item.get("candidate_id", cid),
                    "approve": bool(item.get("approve", True)),
                    "reason": str(item.get("reason", "")),
                    "confidence_adjustment": round(adj, 4),
                }
            )

        spend = await _record_spend(COST_PER_REQUEST_ESTIMATE)
        # doc/visual_reasoning_redesign.md item 4: the model that actually
        # produced this verdict, shown in the frontend's AI-review popover -
        # only set here, where a model call genuinely happened, not on the
        # fail-open paths above/below where nothing was actually reviewed.
        result = {"verdicts": verdicts, "skipped": False, "skip_reason": None, "model": FEATHERLESS_MODEL}
        await log_event(
            "ai_review",
            {"candidates": candidates, "regime_result": regime_result, "iv_result": iv_result},
            # raw_response kept alongside the parsed verdicts so a degenerate-but-
            # validly-shaped reply (e.g. approve=true with a boilerplate reason on
            # every candidate) can still be audited later - only the parsed
            # verdicts were ever kept before this, with no way to check the
            # model actually reasoned about the specific candidates.
            {**result, "raw_response": raw},
            f"reviewed {len(verdicts)} candidate(s); cumulative spend est. ${spend['cumulative_spend']:.2f} "
            f"of ${BUDGET_TOTAL:.2f} budget",
            trace_id=trace_id,
        )
        return result

    except Exception as e:
        reason = f"AI review unavailable ({e}) - defaulting to approve"
        verdicts = _fail_open(candidates_with_ids, reason)
        result = {"verdicts": verdicts, "skipped": False, "skip_reason": None, "model": None}
        await log_event("ai_review_failed_open", {"candidates": candidates}, result, reason, trace_id=trace_id)
        return result
