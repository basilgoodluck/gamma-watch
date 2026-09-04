"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  createChart,
  LineSeries,
  AreaSeries,
  ColorType,
  LineType,
  type AreaData,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import { COLORS, REGIME_COLORS, REGIME_COLORS_ON_WHITE } from "@/lib/theme";
import type { DecisionEvent, PriceBar, RegimePoint } from "@/lib/types";
import EventPopover, { type PopoverAnchor } from "./EventPopover";

// The confidence skyline lives in the SAME pane as the price line, pinned to
// the bottom of it via a dedicated overlay price scale - not a second
// lightweight-charts pane. See the note below on why a second pane is
// avoided entirely.
const CONFIDENCE_SCALE_ID = "confidence";
const CONFIDENCE_SCALE_MARGIN_TOP = 0.8; // skyline occupies the bottom 20% of the pane
const CONFIDENCE_LABEL_MIN_WIDTH_PX = 56; // skip labeling segments too narrow to fit legibly
const SKYLINE_HIT_AREA_PCT = 26; // generous hover/click band over the skyline, doc/gamma_watch_polish.md item 2

function hexToRgba(hex: string, alpha: number): string {
  const n = parseInt(hex.replace("#", ""), 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function toUnixTs(iso: string): UTCTimestamp {
  return Math.floor(Date.parse(iso) / 1000) as UTCTimestamp;
}

/** The regime in effect at time t (the most recent regime point at or before t). */
function regimeAt(sortedPoints: RegimePoint[], t: number): RegimePoint | null {
  let current: RegimePoint | null = null;
  for (const p of sortedPoints) {
    if (Date.parse(p.ts) <= t) current = p;
    else break;
  }
  return current;
}

interface Tooltip {
  x: number;
  y: number;
  price: number;
  regime: RegimePoint | null;
}

export default function DashboardChart({
  bars,
  regimePoints,
  decisionEvents,
  interval,
}: {
  bars: PriceBar[];
  regimePoints: RegimePoint[];
  decisionEvents: DecisionEvent[];
  interval?: string | null;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const lineSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const regimeSeriesRef = useRef<ISeriesApi<"Area">[]>([]);
  const [tooltip, setTooltip] = useState<Tooltip | null>(null);
  const [regimeLabels, setRegimeLabels] = useState<{ key: string; x: number; label: string; color: string }[]>([]);
  const [popoverAnchor, setPopoverAnchor] = useState<PopoverAnchor | null>(null);
  const [pinned, setPinned] = useState(false);
  // doc/chart_bugs_diagnose.md item 1: the real "1 Issue" error, confirmed
  // via a real browser stack trace (not guessed) -
  //   ensureNotNull -> ChartWidget._private__adjustSizeImpl
  // - the library's own internal draw path throws if the chart was ever
  // given a zero width/height. The container can genuinely measure 0 for a
  // frame before the flex/grid layout settles - right at mount. So the
  // chart must not be CREATED until the container has a real measured
  // size, not just resized defensively afterward. chartReady flips once
  // that's happened, and the data-syncing effects below re-run off it so
  // they don't miss the window between mount and the container actually
  // getting a size.
  //
  // Also confirmed via the same real-browser testing: a SECOND lightweight-
  // charts pane (an earlier histogram-based regime strip attempt) hits a
  // genuine library bug in this version - even with a correctly-registered
  // priceScaleId and every creation-order/timing fix tried, mutating pane 1
  // still corrupted the chart's internal draw state (reproduced in both dev
  // AND a production build, so it's not a React Strict Mode artifact
  // either). doc/gamma_watch_polish.md item 2 removed the old per-event dot
  // markers entirely (a plain DOM overlay on the price line) - the anchored
  // popover now opens from hovering/clicking the confidence skyline itself
  // instead. Positions are computed from
  // in sync on pan/zoom/resize.
  //
  // The confidence skyline (doc/chart_full_fix.md item 3) is real
  // lightweight-charts Area series - deliberately kept in PANE 0 (the same
  // one the price line lives in), given its own overlay price scale
  // (CONFIDENCE_SCALE_ID) pinned to the bottom 20% via scaleMargins, instead
  // of a second pane. That sidesteps the confirmed pane-1 bug entirely
  // while still rendering as a real chart series that pans/zooms in lockstep
  // with the price line - no separate DOM strip to keep in sync by hand.
  const [chartReady, setChartReady] = useState(false);

  // Create the chart once the container has a real size - never before.
  useEffect(() => {
    if (!containerRef.current) return;
    let chart: IChartApi | null = null;
    // React Strict Mode runs this effect's mount -> cleanup -> mount again
    // in dev - a callback scheduled during the FIRST mount could otherwise
    // still fire after that mount's cleanup already disposed its chart.
    let disposed = false;

    const init = (width: number, height: number) => {
      if (disposed || chart || width <= 0 || height <= 0 || !containerRef.current) return;

      chart = createChart(containerRef.current, {
        width,
        height,
        layout: {
          background: { type: ColorType.Solid, color: COLORS.chartBg },
          textColor: COLORS.chartText,
          fontFamily: "var(--font-roboto), ui-sans-serif, sans-serif",
        },
        grid: {
          vertLines: { color: COLORS.chartGrid, style: 1 },
          horzLines: { color: COLORS.chartGrid, style: 1 },
        },
        // The price line's own scale is pulled up off the very bottom of the
        // pane (bottom: 0.24) to leave room for the confidence skyline's
        // overlay scale (pinned to the bottom 20%, with a small gap between
        // the two so the price line and the skyline never visually collide).
        rightPriceScale: { borderColor: COLORS.chartGrid, scaleMargins: { top: 0.08, bottom: 0.24 } },
        timeScale: { borderColor: COLORS.chartGrid, timeVisible: true, secondsVisible: false },
        crosshair: { vertLine: { color: COLORS.chartText }, horzLine: { color: COLORS.chartText } },
      });

      const lineSeries = chart.addSeries(LineSeries, {
        color: COLORS.chartLine,
        lineWidth: 2,
        lastValueVisible: false,
        priceLineVisible: false,
      });

      chartRef.current = chart;
      lineSeriesRef.current = lineSeries;
      setChartReady(true);
    };

    const resize = (width: number, height: number) => {
      if (width <= 0 || height <= 0) return;
      if (!chart) {
        init(width, height);
      } else {
        chart.resize(width, height);
      }
    };

    const ro = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      if (box) resize(box.width, box.height);
    });
    ro.observe(containerRef.current);
    // In case the container already has a size before the observer's first
    // callback fires.
    resize(containerRef.current.clientWidth, containerRef.current.clientHeight);

    return () => {
      disposed = true;
      ro.disconnect();
      chart?.remove(); // also disposes every Area series still attached to it
      chartRef.current = null;
      lineSeriesRef.current = null;
      regimeSeriesRef.current = [];
    };
  }, []);

  // Line - close price only. lightweight-charts positions everything off a
  // discrete array of known time-scale points, not continuous/proportional
  // time - timeToCoordinate() only resolves times that are (at or very near)
  // an actual point already in the series, it does NOT interpolate across a
  // gap between two far-apart points. The decision loop keeps firing
  // "watching" events in real time between bar closes (bars only update
  // once per completed candle), so every recent event's timestamp falls
  // into the gap after the last bar and would resolve to null. Fix: add a
  // whitespace point (no value, draws nothing) at every event's own
  // timestamp past the last bar, so each one has an exact point to key off.
  useEffect(() => {
    if (!lineSeriesRef.current) return;
    const data: (LineData<Time> | WhitespaceData<Time>)[] = bars.map((b) => ({
      time: toUnixTs(b.ts),
      value: b.close,
    }));
    const lastBarTime = bars.length > 0 ? toUnixTs(bars[bars.length - 1].ts) : 0;
    const extraTimes = new Set<number>();
    for (const e of decisionEvents) {
      const t = Math.floor(Date.parse(e.ts) / 1000);
      if (t > lastBarTime) extraTimes.add(t);
    }
    extraTimes.add(Math.floor(Date.now() / 1000));
    for (const t of [...extraTimes].sort((a, b) => a - b)) {
      if (t > lastBarTime) data.push({ time: t as UTCTimestamp });
    }
    lineSeriesRef.current.setData(data);
  }, [bars, decisionEvents, chartReady]);

  const sortedRegimePoints = useMemo(
    () => [...regimePoints].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts)),
    [regimePoints]
  );

  // Confidence skyline (doc/chart_full_fix.md item 3): one Area series per
  // contiguous same-regime segment, height driven by confidence, colored by
  // that segment's regime, smooth-curved within a segment - a hard edge
  // between segments (not a blend), same as the old strip's boundaries,
  // because each segment is a genuinely separate series rather than one
  // continuous line changing color. All share CONFIDENCE_SCALE_ID so they
  // stack into a single band instead of each getting its own scale.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !chartReady) return;

    const render = () => {
      const sorted = sortedRegimePoints;

      for (const s of regimeSeriesRef.current) chart.removeSeries(s);
      regimeSeriesRef.current = [];

      if (sorted.length === 0 || bars.length === 0) {
        setRegimeLabels([]);
        return;
      }

      const lastBarTime = toUnixTs(bars[bars.length - 1].ts);
      const timeScale = chart.timeScale();
      const labels: { key: string; x: number; label: string; color: string }[] = [];

      // Group into contiguous runs of the same regime - each run becomes
      // its own Area series so the color change at a boundary is a hard
      // edge, while the curve WITHIN a run is smooth (LineType.Curved).
      let i = 0;
      while (i < sorted.length) {
        let j = i;
        while (j + 1 < sorted.length && sorted[j + 1].regime === sorted[i].regime) j++;
        const segment = sorted.slice(i, j + 1);
        const regime = segment[0].regime;
        const color = REGIME_COLORS_ON_WHITE[regime];

        const data: AreaData<Time>[] = segment.map((p) => ({
          time: toUnixTs(p.ts),
          value: p.confidence,
        }));
        const lastPointTime = toUnixTs(segment[segment.length - 1].ts);
        // Extend flat to the boundary with the next segment (or the last
        // bar, for the final segment) so segments meet edge-to-edge with no
        // gap, rather than stopping dead at the last point's own timestamp.
        const boundaryT = j + 1 < sorted.length ? toUnixTs(sorted[j + 1].ts) : lastBarTime;
        if (boundaryT > lastPointTime) {
          data.push({ time: boundaryT, value: segment[segment.length - 1].confidence });
        }

        const series = chart.addSeries(AreaSeries, {
          priceScaleId: CONFIDENCE_SCALE_ID,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
          lineType: LineType.Curved,
          lineWidth: 2,
          lineColor: color,
          topColor: hexToRgba(color, 0.55),
          bottomColor: hexToRgba(color, 0.05),
        });
        series.setData(data);
        regimeSeriesRef.current.push(series);
        // lightweight-charts throws "incorrect ID" if a custom price scale
        // is configured before any series references it - so this can only
        // be done here, after the first Area series using it exists, not at
        // chart-creation time. Cheap and idempotent to reapply on every
        // segment.
        series.priceScale().applyOptions({
          scaleMargins: { top: CONFIDENCE_SCALE_MARGIN_TOP, bottom: 0 },
          visible: false,
          borderVisible: false,
        });

        const x1 = timeScale.timeToCoordinate(toUnixTs(segment[0].ts));
        const x2 = timeScale.timeToCoordinate(boundaryT);
        if (x1 !== null && x2 !== null && x2 - x1 >= CONFIDENCE_LABEL_MIN_WIDTH_PX) {
          labels.push({ key: `${regime}-${segment[0].ts}`, x: (x1 + x2) / 2, label: regime, color });
        }

        i = j + 1;
      }

      setRegimeLabels(labels);
    };

    render();
    chart.timeScale().subscribeVisibleTimeRangeChange(render);
    window.addEventListener("resize", render);
    return () => {
      chart.timeScale().unsubscribeVisibleTimeRangeChange(render);
      window.removeEventListener("resize", render);
    };
  }, [bars, sortedRegimePoints, chartReady]);

  // Combined tooltip: price + regime + confidence together on crosshair hover.
  useEffect(() => {
    const chart = chartRef.current;
    const lineSeries = lineSeriesRef.current;
    if (!chart || !lineSeries) return;

    const handler = (param: MouseEventParams<Time>) => {
      if (!param.time || !param.point) {
        setTooltip(null);
        return;
      }
      const point = param.seriesData.get(lineSeries) as { value?: number } | undefined;
      if (!point || point.value === undefined) {
        setTooltip(null);
        return;
      }
      const regime = regimeAt(sortedRegimePoints, (param.time as number) * 1000);
      setTooltip({ x: param.point.x, y: param.point.y, price: point.value, regime });
    };

    chart.subscribeCrosshairMove(handler);
    return () => chart.unsubscribeCrosshairMove(handler);
  }, [sortedRegimePoints, chartReady]);

  const closePopover = () => {
    setPopoverAnchor(null);
    setPinned(false);
  };

  // doc/gamma_watch_polish.md item 2: the popover now opens from hovering or
  // clicking the confidence skyline strip itself, not from per-event dot
  // markers on the price line (removed entirely). It shows the regime
  // active at the hovered point - the same "regime" info the old dot marker
  // used to carry - reusing EventPopover unchanged via a synthetic
  // regime_classify-shaped event built straight from the regime-point data
  // the skyline is already drawn from.
  const skylinePointAt = (clientX: number): RegimePoint | null => {
    const chart = chartRef.current;
    const container = containerRef.current;
    if (!chart || !container) return null;
    const rect = container.getBoundingClientRect();
    const time = chart.timeScale().coordinateToTime(clientX - rect.left);
    if (time === null) return null;
    return regimeAt(sortedRegimePoints, (time as number) * 1000);
  };

  const openSkylinePopover = (clientX: number, pin: boolean) => {
    const container = containerRef.current;
    const point = skylinePointAt(clientX);
    if (!container || !point) return;
    const rect = container.getBoundingClientRect();
    const event: DecisionEvent = {
      ts: point.ts,
      trace_id: null,
      step: "regime_classify",
      input: {},
      output: { regime: point.regime, confidence: point.confidence },
      reason: "",
    };
    const y = container.clientHeight * (1 - SKYLINE_HIT_AREA_PCT / 100);
    setPopoverAnchor({ x: clientX - rect.left + 10, y: y - 10, event });
    setPinned(pin);
  };

  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden">
      <div className="relative min-h-0 flex-1">
        <div ref={containerRef} className="absolute inset-0" />

        {/* Timeframe/interval - moved here from the header on request. Small,
            unobtrusive, top-left of the chart itself (a common convention for
            charting UIs - TradingView, etc. - rather than living as header
            chrome). Derived from real bar spacing, not a hardcoded label. */}
        {interval && (
          <div
            className="pointer-events-none absolute left-2 top-2 z-10 rounded bg-black/5 px-1.5 py-0.5 text-[11px] font-medium"
            style={{ color: COLORS.chartText }}
          >
            {interval}
          </div>
        )}

        {/* Skyline hover/click hit area - doc/gamma_watch_polish.md item 2. */}
        <div
          className="absolute inset-x-0 bottom-0 z-10 cursor-pointer"
          style={{ height: `${SKYLINE_HIT_AREA_PCT}%` }}
          onMouseMove={(ev) => !pinned && openSkylinePopover(ev.clientX, false)}
          onMouseLeave={() => !pinned && closePopover()}
          onClick={(ev) => {
            ev.stopPropagation();
            openSkylinePopover(ev.clientX, true);
          }}
        />

        {popoverAnchor && (
          <div
            className="absolute inset-0 z-10"
            onClick={closePopover}
            style={{ pointerEvents: pinned ? "auto" : "none" }}
          />
        )}
        <EventPopover anchor={popoverAnchor} onClose={closePopover} />

        {tooltip && !popoverAnchor && (
          <div
            className="pointer-events-none absolute z-10 rounded border border-border bg-surface/95 px-3 py-2 text-sm shadow-sm"
            style={{ left: tooltip.x + 12, top: tooltip.y + 12 }}
          >
            <div className="font-tabular font-medium text-text">${tooltip.price.toFixed(2)}</div>
            {tooltip.regime && (
              <div className="mt-1 flex items-center gap-1.5 text-text-muted">
                <span
                  className="inline-block h-2 w-2 rounded-sm"
                  style={{ backgroundColor: REGIME_COLORS[tooltip.regime.regime] }}
                />
                {tooltip.regime.regime} · {(tooltip.regime.confidence * 100).toFixed(0)}%
              </div>
            )}
          </div>
        )}

        {/* Confidence skyline regime labels - doc/chart_full_fix.md item 3.
            The skyline itself is drawn by the Area series above (inside the
            chart canvas); these are just the inline text labels, skipped for
            segments too narrow to fit one legibly. */}
        <div className="pointer-events-none absolute inset-x-0 bottom-4 z-10">
          {regimeLabels.map((l) => (
            <span
              key={l.key}
              className="absolute -translate-x-1/2 whitespace-nowrap text-[11px] font-semibold uppercase tracking-wide"
              style={{ left: l.x, color: l.color }}
            >
              {l.label}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
