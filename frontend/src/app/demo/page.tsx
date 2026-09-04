"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import DashboardChart from "@/components/DashboardChart";
import PanelsDrawer from "@/components/PanelsDrawer";
import ReasoningFeedModal from "@/components/ReasoningFeedModal";
import SymbolDropdown from "@/components/SymbolDropdown";
import { REGIME_COLORS } from "@/lib/theme";
import { SPRING } from "@/lib/motion";
import {
  getDecisionLog,
  getIvHistory,
  getLoopStatus,
  getOpenPositions,
  getRegimeHistory,
  getWatchlist,
  startLoop,
  stopLoop,
} from "@/lib/api";
import type { DecisionEvent, IvResult, Position, PriceBar, RegimePoint } from "@/lib/types";

const FALLBACK_WATCHLIST = ["SPY", "QQQ"];
const CHART_DAYS = 100; // bumped from 14 - matches the backend's DEFAULT_CHART_DAYS / REGIME_COMPUTE_LOOKBACK_DAYS
const POLL_MS = 5000;
const WS_RECONNECT_MAX_MS = 30_000;

// doc/zerodte_and_watchlist.md item 3 / doc/source_of_truth_and_race_
// conditions.md: same env var the REST calls already use (NEXT_PUBLIC_API_BASE)
// so deploying the frontend only ever needs ONE setting changed (on Vercel,
// pointed at the Railway backend), not a separate ws:// one to keep in sync.
// http(s) -> ws(s) so it correctly upgrades to wss:// once that base is a
// real https:// Railway URL, not just for the ws://localhost dev case.
function wsUrlFor(symbol: string): string {
  const apiBase = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
  const wsBase = apiBase.replace(/^http/, "ws");
  return `${wsBase}/ws/price?symbol=${encodeURIComponent(symbol)}&days=${CHART_DAYS}`;
}

// doc/gamma_watch_polish.md item 3: derived from the actual gap between the
// two most recent bars, not a hardcoded assumption - self-verifying against
// whatever the backend is really returning instead of a label that could
// silently drift out of sync with it.
function formatInterval(bars: PriceBar[]): string | null {
  if (bars.length < 2) return null;
  const minutes = Math.round((Date.parse(bars[bars.length - 1].ts) - Date.parse(bars[bars.length - 2].ts)) / 60000);
  if (minutes <= 0) return null;
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.round(minutes / 60)}h`;
  return `${Math.round(minutes / 1440)}D`;
}

export default function Home() {
  const [watchlist, setWatchlist] = useState<string[]>(FALLBACK_WATCHLIST);
  const [symbol, setSymbol] = useState<string>(FALLBACK_WATCHLIST[0]);
  const [bars, setBars] = useState<PriceBar[]>([]);
  const [regimePoints, setRegimePoints] = useState<RegimePoint[]>([]);
  const [ivCurrent, setIvCurrent] = useState<IvResult | null>(null);
  const [events, setEvents] = useState<DecisionEvent[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [loopRunning, setLoopRunning] = useState(true);
  const [loopAlive, setLoopAlive] = useState(true);
  const [nextFireAt, setNextFireAt] = useState<string | null>(null);
  const [lastCycleAt, setLastCycleAt] = useState<string | null>(null);
  const [toggling, setToggling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [panelsOpen, setPanelsOpen] = useState(false);
  const [confirmStopOpen, setConfirmStopOpen] = useState(false);
  const [logsModalOpen, setLogsModalOpen] = useState(false);
  const [pnlHistory, setPnlHistory] = useState<number[]>([]);
  const PNL_HISTORY_MAX = 100;

  // Fetch the real watchlist once (config.WATCHLIST_SYMBOLS, not hardcoded).
  useEffect(() => {
    getWatchlist()
      .then((w) => {
        if (w.symbols.length > 0) {
          setWatchlist(w.symbols);
          setSymbol((current) => (w.symbols.includes(current) ? current : w.symbols[0]));
        }
      })
      .catch(() => {
        /* keep the fallback list - the chart still works for its default symbol */
      });
  }, []);

  // doc/source_of_truth_and_race_conditions.md item 2 / doc/zerodte_and_
  // watchlist.md item 3: price data now arrives over a WebSocket push, not
  // polling - the effect below owns the connection lifecycle (connect,
  // reconnect-with-backoff, symbol-switch = close old + open new). Only the
  // panel data (regime, IV, decision log, positions, loop status) still
  // polls, per the doc's explicit scope.
  //
  // The out-of-order guard below still applies to THAT polling, for the
  // same reason it always did: nothing here guards against a stale response
  // landing after a newer one, and /api/regime_history is genuinely slow
  // on a cache miss (~21-23s) - see the prior version of this comment in
  // git history for the full incident writeup. Comparing against the
  // highest sequence number ALREADY COMMITTED (not ISSUED) is what keeps a
  // slow response from either overwriting fresher data OR starving forever.
  const restIssuedRef = useRef(0);
  const restCommittedRef = useRef(0);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let closedIntentionally = false;

    const connect = () => {
      setBars([]); // avoid showing the previous symbol's data on the new symbol's scale while history loads
      socket = new WebSocket(wsUrlFor(symbol));

      socket.onopen = () => {
        attempt = 0;
        setError(null);
      };

      socket.onmessage = (event) => {
        const msg = JSON.parse(event.data) as
          | { type: "history"; bars: PriceBar[] }
          | { type: "bar"; bar: PriceBar };
        if (msg.type === "history") {
          setBars(msg.bars);
        } else if (msg.type === "bar") {
          // Merge: replace the last bar if the new one shares its timestamp
          // (the same in-progress bar updating), otherwise append a new one -
          // matching how a real-time bar stream actually behaves.
          setBars((prev) => {
            if (prev.length > 0 && prev[prev.length - 1].ts === msg.bar.ts) {
              return [...prev.slice(0, -1), msg.bar];
            }
            return [...prev, msg.bar];
          });
        }
      };

      socket.onclose = () => {
        if (closedIntentionally) return;
        // Reconnect with backoff - re-sync happens for free, since the
        // server always sends a fresh "history" message on every new
        // connection instead of trying to replay only what was missed.
        attempt += 1;
        const delay = Math.min(1000 * 2 ** attempt, WS_RECONNECT_MAX_MS);
        setError(`price stream disconnected - reconnecting in ${Math.round(delay / 1000)}s`);
        reconnectTimer = setTimeout(connect, delay);
      };

      socket.onerror = () => {
        socket?.close();
      };
    };

    connect();

    return () => {
      closedIntentionally = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [symbol]);

  const refreshRest = useCallback(async () => {
    const mySeq = ++restIssuedRef.current;
    try {
      const [regimeRes, ivRes, logRes, posRes, loopRes] = await Promise.all([
        getRegimeHistory(symbol, CHART_DAYS),
        getIvHistory(symbol),
        getDecisionLog(200),
        getOpenPositions(),
        getLoopStatus(),
      ]);
      if (mySeq <= restCommittedRef.current) return; // a newer call already committed - this one is stale
      restCommittedRef.current = mySeq;
      setRegimePoints(regimeRes.points);
      setIvCurrent(ivRes.current);
      setEvents(logRes.events);
      setPositions(posRes.positions);
      setLoopRunning(loopRes.running);
      setLoopAlive(loopRes.alive);
      setNextFireAt(loopRes.next_fire_at);
      setLastCycleAt(loopRes.last_cycle_at);

      // No backend equity-curve endpoint exists, so accumulate a rolling
      // client-side total P&L (realized + unrealized, estimated off
      // current_pnl_pct * max_loss the same way PositionProgressBar scales)
      // for the sparkline (doc/visual_reasoning_redesign.md item 3).
      const realized = posRes.positions
        .filter((p) => p.status === "closed")
        .reduce((sum, p) => sum + (p.realized_pnl ?? 0), 0);
      const unrealized = posRes.positions
        .filter((p) => p.status === "open")
        .reduce((sum, p) => sum + (p.current_pnl_pct ?? 0) * p.max_loss, 0);
      setPnlHistory((prev) => [...prev, realized + unrealized].slice(-PNL_HISTORY_MAX));
    } catch (e) {
      if (mySeq <= restCommittedRef.current) return;
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [symbol]);

  useEffect(() => {
    const id = setInterval(refreshRest, POLL_MS);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch, not a synchronous setState
    void refreshRest();
    return () => clearInterval(id);
  }, [refreshRest]);

  const handleToggleLoop = async () => {
    setToggling(true);
    try {
      const result = loopRunning ? await stopLoop() : await startLoop();
      setLoopRunning(result.running);
      setLoopAlive(result.alive);
      setNextFireAt(result.next_fire_at);
      setLastCycleAt(result.last_cycle_at);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setToggling(false);
    }
  };

  const handleStartClick = () => {
    void handleToggleLoop(); // starting needs no confirmation
  };

  const handleStopConfirmed = () => {
    setConfirmStopOpen(false);
    void handleToggleLoop();
  };

  const latestRegime = regimePoints.length > 0 ? regimePoints[regimePoints.length - 1] : null;
  const interval = formatInterval(bars);
  const statusTitle =
    [
      lastCycleAt ? `last cycle: ${new Date(lastCycleAt).toLocaleTimeString()}` : null,
      loopRunning && loopAlive && nextFireAt ? `next cycle: ${new Date(nextFireAt).toLocaleTimeString()}` : null,
    ]
      .filter(Boolean)
      .join(" · ") || undefined;

  return (
    <div className="flex h-screen flex-col bg-bg">
      {/* Unified chip system: every interactive/status element in the header
          shares the same shape language - h-9, rounded-md, bg-surface-2, no
          borders anywhere. Separation comes from background contrast alone
          (bg-surface header on bg-bg page, bg-surface-2 chips on top of
          that). The regime badge stays the one place color is allowed to
          carry meaning, but as a soft tint + colored text rather than a
          fully saturated fill, so it reads as calm status rather than a
          warning light. Play/pause is a square icon button matching the
          hamburger; only the icon tints - a single muted red while a
          stoppable loop is live, neutral gray while stopped - so color
          still signals state without a separate red/green shape language. */}
      <header className="flex h-16 items-center justify-between bg-surface px-6">
        <div className="flex items-center gap-4">
          <span className="text-sm font-semibold tracking-wide text-text">Gamma Watch</span>

          {loopRunning ? (
            <button
              onClick={() => setConfirmStopOpen(true)}
              disabled={toggling}
              aria-label="Pause agent"
              title="Pause agent"
              className="flex h-9 w-9 items-center justify-center rounded-md bg-surface-2 text-[#b5665f] transition-colors hover:bg-surface-2/70 disabled:opacity-40"
            >
              <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                <rect x="3" y="2" width="3.5" height="12" rx="1" fill="currentColor" />
                <rect x="9.5" y="2" width="3.5" height="12" rx="1" fill="currentColor" />
              </svg>
            </button>
          ) : (
            <button
              onClick={handleStartClick}
              disabled={toggling}
              aria-label="Resume agent"
              title="Resume agent"
              className="flex h-9 w-9 items-center justify-center rounded-md bg-surface-2 text-text-muted transition-colors hover:bg-surface-2/70 hover:text-text disabled:opacity-40"
            >
              <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                <path d="M3.5 2 L13.5 8 L3.5 14 Z" fill="currentColor" />
              </svg>
            </button>
          )}

          <SymbolDropdown symbols={watchlist} value={symbol} onChange={setSymbol} />

          {/* Soft-tint regime badge - background is the regime color at low
              opacity (mixed with the surface, not a flat saturated fill),
              text carries the full color instead. Still never silently
              absent - shows a matched "loading" chip while regimePoints
              hasn't arrived yet. */}
          {latestRegime ? (
            <div
              className="flex h-9 items-center gap-2 rounded-md px-3"
              style={{ backgroundColor: `${REGIME_COLORS[latestRegime.regime]}26` }}
            >
              <span className="text-sm font-semibold" style={{ color: REGIME_COLORS[latestRegime.regime] }}>
                {latestRegime.regime}
              </span>
              <span className="text-sm text-text-muted">{(latestRegime.confidence * 100).toFixed(0)}%</span>
            </div>
          ) : (
            <div className="flex h-9 items-center rounded-md bg-surface-2 px-3">
              <span className="text-sm text-text-muted">Regime: loading…</span>
            </div>
          )}
        </div>

        <div className="flex items-center gap-3">
          {/* Live status - same chip treatment as everything else. Still
              surfaces a stuck loop (running=true but alive=false) via
              wording, and the "next cycle" detail is still one hover away
              via the title tooltip. */}
          <div className="flex h-9 items-center rounded-md bg-surface-2 px-3" title={statusTitle}>
            <span className="text-sm text-text">{!loopRunning ? "Stopped" : loopAlive ? "Running" : "Stuck"}</span>
          </div>

          {/* Hamburger - same chip shape as the icon button on the left, so
              the two square icon controls in the header read as one family.
              2-line icon that morphs into an X while the panel is open. */}
          <button
            onClick={() => setPanelsOpen((o) => !o)}
            aria-label={panelsOpen ? "Close panels" : "Open panels"}
            title="Panels"
            className="flex h-9 w-9 items-center justify-center rounded-md bg-surface-2 text-text transition-colors hover:bg-surface-2/70"
          >
            <span className="relative block h-4 w-4">
              <span
                className={`absolute left-0 block h-0.5 w-4 bg-current transition-all duration-200 ${
                  panelsOpen ? "top-[7px] rotate-45" : "top-[4px] rotate-0"
                }`}
              />
              <span
                className={`absolute left-0 block h-0.5 w-4 bg-current transition-all duration-200 ${
                  panelsOpen ? "top-[7px] -rotate-45" : "top-[10px] rotate-0"
                }`}
              />
            </span>
          </button>
        </div>
      </header>

      {error && (
        <div className="bg-range-volatile/10 px-4 py-1.5 text-sm text-range-volatile">{error}</div>
      )}

      <main className="min-h-0 flex-1 p-4">
        {/* Only the chart is white/no border - everything else recedes into
            the darker navy. */}
        <div className="h-full w-full overflow-hidden rounded-lg bg-chart-bg">
          <DashboardChart bars={bars} regimePoints={regimePoints} decisionEvents={events} interval={interval} />
        </div>
      </main>

      <PanelsDrawer
        open={panelsOpen}
        onClose={() => setPanelsOpen(false)}
        positions={positions}
        events={events}
        ivCurrent={ivCurrent}
        pnlHistory={pnlHistory}
        onExpandLogs={() => setLogsModalOpen(true)}
      />

      <ReasoningFeedModal open={logsModalOpen} onClose={() => setLogsModalOpen(false)} events={events} />

      {/* Confirm-stop modal - floats with margin, never touches edges. Same
          spring physical system as every other open/close interaction
          (doc/visual_reasoning_redesign.md item 4). */}
      <AnimatePresence>
        {confirmStopOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-6">
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={SPRING}
              className="absolute inset-0 bg-black/30"
              onClick={() => setConfirmStopOpen(false)}
            />
            <motion.div
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.92 }}
              transition={SPRING}
              className="relative w-full max-w-sm rounded-lg bg-surface p-6 shadow-lg"
            >
              <p className="text-base font-medium text-text">Stop the agent?</p>
              <p className="mt-1 text-sm text-text-muted">
                No new cycles will run until you start it again. Open positions keep being monitored.
              </p>
              <div className="mt-4 flex justify-end gap-2">
                <button
                  onClick={() => setConfirmStopOpen(false)}
                  className="h-9 rounded-md bg-surface-2 px-4 text-sm text-text transition-colors hover:bg-surface-2/70"
                >
                  Cancel
                </button>
                <button
                  onClick={handleStopConfirmed}
                  className="h-9 rounded-md bg-[#b5665f] px-4 text-sm font-medium text-white transition-colors hover:bg-[#a25a54]"
                >
                  Stop agent
                </button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </div>
  );
}