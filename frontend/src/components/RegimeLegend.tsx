import { REGIME_COLORS, REGIME_ORDER } from "@/lib/theme";

export default function RegimeLegend() {
  return (
    <div className="space-y-1.5">
      <div className="mb-2 text-sm font-medium uppercase tracking-wide text-text-muted">Regime Legend</div>
      {REGIME_ORDER.map((r) => (
        <div key={r} className="flex items-center gap-2 text-sm text-text-muted">
          <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: REGIME_COLORS[r] }} />
          {r}
        </div>
      ))}
    </div>
  );
}
