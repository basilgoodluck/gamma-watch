"use client";

function timeUntil(iso: string | null): string | null {
  if (!iso) return null;
  const ms = Date.parse(iso) - Date.now();
  if (ms <= 0) return "momentarily";
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.floor((ms % 60000) / 1000);
  return minutes > 0 ? `~${minutes}m ${seconds}s` : `~${seconds}s`;
}

export default function LoopControl({
  running,
  alive,
  nextFireAt,
  lastCycleAt,
  onToggle,
  toggling,
}: {
  running: boolean;
  alive: boolean;
  nextFireAt: string | null;
  lastCycleAt: string | null;
  onToggle: () => void;
  toggling: boolean;
}) {
  // doc/fix_dead_loop.md item 5: a stuck loop must be visible here, not just
  // in the server log - `running` is only the kill-switch's last-set intent,
  // it kept reading "true" for hours after the loop actually died silently.
  const stuck = running && !alive;
  return (
    <div className="flex items-center gap-2 text-xs">
      <span
        className={`flex items-center gap-1.5 ${stuck ? "font-medium text-range-volatile" : "text-text-muted"}`}
        title={stuck ? `no cycle has completed since ${lastCycleAt ?? "startup"}` : undefined}
      >
        <span
          className={`inline-block h-2 w-2 rounded-full ${
            stuck ? "animate-pulse bg-range-volatile" : running ? "bg-trend-calm" : "bg-range-volatile"
          }`}
          aria-hidden
        />
        {stuck
          ? "stuck - no cycle firing"
          : running
            ? `running · next cycle ${timeUntil(nextFireAt) ?? "soon"}`
            : "stopped"}
      </span>
      {/* Both states use the accent blue (doc/theme_fix_explicit.md - "no
          exceptions"); the 5 regime colors are reserved for regime
          classification, not general UI chrome like this control. */}
      <button
        onClick={onToggle}
        disabled={toggling}
        title={running ? "Stop the autonomous loop" : "Resume the autonomous loop"}
        className={`rounded border border-accent px-3 py-1 font-medium transition-colors disabled:opacity-50 ${
          running ? "text-accent hover:bg-accent/10" : "bg-accent text-surface hover:bg-accent-hover"
        }`}
      >
        {running ? "Stop" : "Start"}
      </button>
    </div>
  );
}
