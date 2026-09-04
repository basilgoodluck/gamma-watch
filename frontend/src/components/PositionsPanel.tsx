"use client";

import PositionProgressBar from "./PositionProgressBar";
import type { Position } from "@/lib/types";

export default function PositionsPanel({ positions }: { positions: Position[] }) {
  const open = positions.filter((p) => p.status === "open");

  return (
    <div>
      <div className="mb-2 text-sm font-medium uppercase tracking-wide text-text-muted">
        Open Positions ({open.length})
      </div>
      {open.length === 0 && <div className="text-base text-text-muted">No open positions.</div>}
      <div className="space-y-3">
        {open.map((p) => {
          const pct = p.current_pnl_pct ?? 0;
          const color = pct > 0 ? "text-trend-calm" : pct < 0 ? "text-range-volatile" : "text-text-muted";
          const targetPct = p.profit_target_pct;
          return (
            <div key={p.id} className="space-y-1.5">
              <div className="flex items-center justify-between text-base">
                <div>
                  <div className="text-text">
                    {p.symbol} {p.type}
                  </div>
                  <div className="text-sm text-text-muted">
                    {p.contracts}x, exp {p.expiry}
                  </div>
                </div>
                <div className="text-right">
                  <div className={`font-tabular font-medium ${color}`}>{(pct * 100).toFixed(1)}% of max risk</div>
                  {/* doc/source_of_truth_and_race_conditions.md item 1: the number
                      above is relative to max_loss, NOT the same scale as the
                      profit target below (which is relative to max_gain / premium
                      paid) - showing both, separately labeled, so "past 100%" of
                      one is never mistaken for "past the target" of the other. */}
                  {targetPct !== null && (
                    <div className="text-xs text-text-muted">{(targetPct * 100).toFixed(0)}% of profit target</div>
                  )}
                </div>
              </div>
              <PositionProgressBar pnlPct={pct} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
