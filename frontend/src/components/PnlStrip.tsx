"use client";

import Sparkline from "./Sparkline";
import { REGIME_COLORS } from "@/lib/theme";
import type { Position } from "@/lib/types";

export default function PnlStrip({ positions, pnlHistory }: { positions: Position[]; pnlHistory: number[] }) {
  const closed = positions.filter((p) => p.status === "closed");
  const realized = closed.reduce((sum, p) => sum + (p.realized_pnl ?? 0), 0);
  const wins = closed.filter((p) => p.outcome === "WIN").length;
  const losses = closed.filter((p) => p.outcome === "LOSS").length;
  const neutral = closed.filter((p) => p.outcome === "NEUTRAL").length;
  const trendingUp = pnlHistory.length < 2 || pnlHistory[pnlHistory.length - 1] >= pnlHistory[0];

  return (
    <div className="rounded border border-border bg-surface-2/60 px-3 py-2 text-sm text-text-muted">
      <div className="flex items-center justify-between">
        <span>
          Total P&amp;L:{" "}
          <span className={`font-tabular font-medium ${realized >= 0 ? "text-trend-calm" : "text-range-volatile"}`}>
            ${realized.toFixed(2)}
          </span>
        </span>
        <span className="font-tabular">
          W {wins} / L {losses} / N {neutral}
        </span>
      </div>
      {/* doc/visual_reasoning_redesign.md item 3: a sparkline, not a static
          number - a rolling client-side history of total (realized +
          unrealized) P&L across this session's polls, since there's no
          backend equity-curve endpoint. */}
      <div className="mt-2">
        <Sparkline
          values={pnlHistory}
          width={200}
          height={28}
          color={trendingUp ? REGIME_COLORS.TREND_CALM : REGIME_COLORS.RANGE_VOLATILE}
        />
      </div>
    </div>
  );
}
