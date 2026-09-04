"use client";

import type { DecisionEvent, Regime } from "@/lib/types";

const REGIME_TEXT_CLASS: Record<Regime, string> = {
  TREND_CALM: "text-trend-calm",
  TREND_VOLATILE: "text-trend-volatile",
  RANGE_CALM: "text-range-calm",
  RANGE_VOLATILE: "text-range-volatile",
  BREAKOUT: "text-breakout",
};

// doc/gamma_watch_polish.md item 6: color the step label so category is
// scannable at a glance without reading every line - not a re-typography
// pass, just which existing color a given row's label uses.
function categoryColorClass(event: DecisionEvent): string {
  const out = event.output as Record<string, unknown>;
  switch (event.step) {
    case "watching": {
      const pnl = out?.unrealized_pnl as number | undefined;
      if (typeof pnl === "number" && pnl > 0) return "text-trend-calm";
      if (typeof pnl === "number" && pnl < 0) return "text-range-volatile";
      return "text-text";
    }
    case "regime_classify": {
      const regime = out?.regime as Regime | undefined;
      return regime ? REGIME_TEXT_CLASS[regime] : "text-text";
    }
    case "ai_review": {
      const verdicts = (out?.verdicts as Array<{ approve: boolean }> | undefined) ?? [];
      if (verdicts.length === 0) return "text-text";
      return verdicts.some((v) => !v.approve) ? "text-range-volatile" : "text-trend-calm";
    }
    case "execute_order":
    case "close_position":
      return "text-accent";
    // iv_rank, strategy_generate, risk_sizing, skipped_ai_review,
    // outcome_labeled: no strong positive/negative signal - stay neutral.
    default:
      return "text-text";
  }
}

const STEP_LABELS: Record<string, string> = {
  regime_classify: "Regime",
  iv_rank: "IV Rank",
  strategy_generate: "Strategy",
  risk_sizing: "Risk Sizing",
  ai_review: "AI Review",
  skipped_ai_review: "AI Review (skipped)",
  execute_order: "Execution",
  close_position: "Close",
  outcome_labeled: "Outcome",
  watching: "Watching",
};

function summarize(event: DecisionEvent): string {
  const out = event.output as Record<string, unknown>;
  const input = event.input as Record<string, unknown>;
  switch (event.step) {
    case "regime_classify":
      return `${out.regime} (confidence ${((out.confidence as number) ?? 0).toFixed(2)})`;
    case "iv_rank":
      return `atm_iv=${out.atm_iv} iv_rank=${out.iv_rank}`;
    case "strategy_generate": {
      const candidates = out as unknown as Array<{ type: string }>;
      return Array.isArray(candidates) && candidates.length > 0 ? candidates.map((c) => c.type).join(", ") : "no candidates";
    }
    case "execute_order":
      return (out.status as string) ?? "";
    // doc/chart_scroll_and_pnl_flap.md item 2: this row used to show nothing
    // but the raw P&L number - with multiple open positions, their
    // "Watching" ticks for DIFFERENT positions interleave a few seconds
    // apart, and two different positions' perfectly-stable PnL values back
    // to back read as one position's PnL "flapping" between two fixed
    // numbers. It never was - each position's own PnL is stable; the feed
    // just never said which position each line was about. Naming the
    // symbol/type here is the actual fix, not a PnL calculation change.
    case "watching":
      return input?.symbol && input?.type ? `${input.symbol} ${input.type}` : "";
    case "close_position": {
      const position = input?.position as Record<string, unknown> | undefined;
      return position?.symbol && position?.type ? `${position.symbol} ${position.type}` : "";
    }
    default:
      return "";
  }
}

export default function ReasoningFeed({ events }: { events: DecisionEvent[] }) {
  const ordered = [...events].sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts));

  return (
    <div className="styled-scrollbar flex h-full flex-col overflow-y-auto">
      {ordered.length === 0 && <div className="p-3 text-base text-text-muted">No events yet.</div>}
      {ordered.map((e, i) => (
        <div key={i} className="border-b border-border px-3 py-3 last:border-b-0">
          <div className="flex items-center justify-between text-sm text-text-muted">
            <span className={`font-medium ${categoryColorClass(e)}`}>{STEP_LABELS[e.step] ?? e.step}</span>
            <span className="font-tabular">{new Date(e.ts).toLocaleTimeString()}</span>
          </div>
          <div className="mt-1 text-sm text-text-muted">{summarize(e)}</div>
          {e.reason && <div className="mt-1 text-base leading-snug text-text">{e.reason}</div>}
        </div>
      ))}
    </div>
  );
}
