"use client";

import { COLORS, REGIME_COLORS } from "@/lib/theme";

// doc/visual_reasoning_redesign.md item 3: horizontal bar showing proximity
// to the profit target on one side and max loss on the other, not just a
// percentage in text. pnlPct is current_pnl_pct (can be negative); the bar
// is centered at 0 (entry), filling toward +1 (full profit target reached)
// or -1 (full max loss reached).
export default function PositionProgressBar({ pnlPct }: { pnlPct: number }) {
  const clamped = Math.max(-1, Math.min(1, pnlPct));
  const isGain = clamped >= 0;
  const widthPct = Math.abs(clamped) * 50; // half the bar's width per side

  return (
    <div className="relative h-1.5 w-full overflow-hidden rounded-full" style={{ backgroundColor: COLORS.chartGrid }}>
      {/* Center marker (entry point) */}
      <div className="absolute left-1/2 top-0 h-full w-px" style={{ backgroundColor: COLORS.border }} />
      <div
        className="absolute top-0 h-full rounded-full transition-all duration-300"
        style={{
          left: isGain ? "50%" : `${50 - widthPct}%`,
          width: `${widthPct}%`,
          backgroundColor: isGain ? REGIME_COLORS.TREND_CALM : REGIME_COLORS.RANGE_VOLATILE,
        }}
      />
    </div>
  );
}
