"use client";

import { COLORS } from "@/lib/theme";

// doc/visual_reasoning_redesign.md item 3: a semicircle progress arc showing
// where current IV sits in its 60-day range, not a bare percentage.
export default function IvGauge({ value, size = 84 }: { value: number; size?: number }) {
  const clamped = Math.max(0, Math.min(1, value));
  const r = size / 2 - 6;
  const cx = size / 2;
  const cy = size / 2;
  const circumference = Math.PI * r;
  const dashoffset = (1 - clamped) * circumference;

  // Track color shifts with the value so a glance tells you "high" vs "low"
  // rank, not just a generic accent color.
  const color = clamped >= 0.7 ? COLORS.chartLine : clamped >= 0.35 ? "#f59e0b" : "#10b981";

  return (
    <svg width={size} height={size / 2 + 8} viewBox={`0 0 ${size} ${size / 2 + 8}`}>
      <path
        d={`M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`}
        fill="none"
        stroke={COLORS.chartGrid}
        strokeWidth={6}
        strokeLinecap="round"
      />
      <path
        d={`M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`}
        fill="none"
        stroke={color}
        strokeWidth={6}
        strokeLinecap="round"
        strokeDasharray={circumference}
        strokeDashoffset={dashoffset}
        style={{ transition: "stroke-dashoffset 0.4s ease, stroke 0.4s ease" }}
      />
      <text x={cx} y={cy - 2} textAnchor="middle" fontSize={16} fontWeight={600} fill={COLORS.text}>
        {(clamped * 100).toFixed(0)}%
      </text>
    </svg>
  );
}
