"use client";

import { AnimatePresence, motion } from "framer-motion";
import { SPRING_SNAPPY } from "@/lib/motion";
import type { DecisionEvent } from "@/lib/types";

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
    case "watching": {
      const pnl = out.unrealized_pnl as number | undefined;
      return pnl !== undefined ? `unrealized P&L $${pnl.toFixed(2)}` : "";
    }
    default:
      return "";
  }
}

export interface PopoverAnchor {
  x: number;
  y: number;
  event: DecisionEvent;
}

export default function EventPopover({ anchor, onClose }: { anchor: PopoverAnchor | null; onClose: () => void }) {
  return (
    <AnimatePresence>
      {anchor && (
        <motion.div
          key={anchor.event.ts + anchor.event.step}
          initial={{ opacity: 0, scale: 0.85, y: 6 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.9, y: 4 }}
          transition={SPRING_SNAPPY}
          className="pointer-events-auto absolute z-20 w-64 rounded-lg border border-border bg-surface p-3 text-sm shadow-2xl"
          style={{ left: anchor.x, top: anchor.y }}
        >
          <div className="flex items-start justify-between gap-2">
            <span className="font-medium text-text">{STEP_LABELS[anchor.event.step] ?? anchor.event.step}</span>
            <button
              onClick={onClose}
              aria-label="Close"
              className="text-text-muted transition-colors hover:text-accent"
            >
              ✕
            </button>
          </div>
          <div className="mt-1 font-tabular text-xs text-text-muted">
            {new Date(anchor.event.ts).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
          </div>
          {summarize(anchor.event) && <div className="mt-2 text-text-muted">{summarize(anchor.event)}</div>}
          {anchor.event.reason && <div className="mt-2 leading-snug text-text">{anchor.event.reason}</div>}
          {anchor.event.step === "ai_review" && (anchor.event.output as { model?: string })?.model && (
            <div className="mt-2 text-xs text-text-muted">
              Reviewed by {(anchor.event.output as { model?: string }).model}
            </div>
          )}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
