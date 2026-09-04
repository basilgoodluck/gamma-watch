import type { Regime } from "./types";

// Full blue/white theme - see globals.css for the CSS custom properties.
// Duplicated here as plain hex because lightweight-charts draws to
// <canvas>, which can't read CSS variables.
export const COLORS = {
  chartBg: "#ffffff",     // the chart itself is white - everything outside it stays dark navy
  chartText: "#334155",   // dark slate - COLORS.textMuted (white) would be invisible on a white chart
  chartGrid: "#e2e8f0",
  chartLine: "#ef4444",   // red price line
  bg: "#0f2050",
  surface: "#16295e",
  surface2: "#1d3470",
  border: "rgba(255, 255, 255, 0.22)",
  text: "#ffffff",
  textMuted: "rgba(255, 255, 255, 0.75)",
  accent: "#93c5fd",
  accentHover: "#bfdbfe",
};

// Brightened for legibility against the app's dark navy background - used by
// the regime chip, tooltip, and legend (all sit on dark surfaces).
export const REGIME_COLORS: Record<Regime, string> = {
  TREND_CALM: "#34d399",
  TREND_VOLATILE: "#fb923c",
  RANGE_CALM: "#c4b5fd",
  RANGE_VOLATILE: "#f472b6",
  BREAKOUT: "#fbbf24",
};

// The SAME bright colors above would wash out to near-invisible pastel tints
// on the chart's white background (this was the literal "I don't see the
// regime" bug) - darker, more saturated versions for that one white surface.
export const REGIME_COLORS_ON_WHITE: Record<Regime, string> = {
  TREND_CALM: "#1f7a5c",
  TREND_VOLATILE: "#b6541c",
  RANGE_CALM: "#6b5b95",
  RANGE_VOLATILE: "#a13d5c",
  BREAKOUT: "#b8860b",
};

export const REGIME_ORDER: Regime[] = ["TREND_CALM", "TREND_VOLATILE", "RANGE_CALM", "RANGE_VOLATILE", "BREAKOUT"];

export function hexToRgba(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
