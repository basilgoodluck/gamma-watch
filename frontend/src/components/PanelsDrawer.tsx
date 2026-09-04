"use client";

import { AnimatePresence, motion } from "framer-motion";
import PositionsPanel from "./PositionsPanel";
import PnlStrip from "./PnlStrip";
import ReasoningFeed from "./ReasoningFeed";
import RegimeLegend from "./RegimeLegend";
import IvSummary from "./IvSummary";
import { SPRING } from "@/lib/motion";
import type { DecisionEvent, IvResult, Position } from "@/lib/types";

export default function PanelsDrawer({
  open,
  onClose,
  positions,
  events,
  ivCurrent,
  pnlHistory,
  onExpandLogs,
}: {
  open: boolean;
  onClose: () => void;
  positions: Position[];
  events: DecisionEvent[];
  ivCurrent: IvResult | null;
  pnlHistory: number[];
  onExpandLogs: () => void;
}) {
  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            key="scrim"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={SPRING}
            className="fixed inset-0 z-40 bg-text/20"
            onClick={onClose}
          />
          {/* Drawer - floats with margin from every viewport edge, never flush
              against the browser chrome (doc/frontend_overhaul_v2.md). Spring
              motion, not a CSS transition - doc/visual_reasoning_redesign.md
              wants every open/close in the app on the same physical system. */}
          <motion.div
            key="drawer"
            initial={{ x: "110%" }}
            animate={{ x: 0 }}
            exit={{ x: "110%" }}
            transition={SPRING}
            className="fixed top-4 right-4 bottom-4 z-50 flex w-full max-w-sm flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-xl"
          >
            <div className="flex items-center justify-between border-b border-border px-4 py-3">
              <span className="text-base font-medium text-text">Panels</span>
              <button
                onClick={onClose}
                aria-label="Close panels"
                className="rounded px-2 py-1 text-text-muted transition-colors hover:bg-accent hover:text-cta-text"
              >
                ×
              </button>
            </div>

            <div className="styled-scrollbar space-y-5 overflow-y-auto px-4 py-4">
              <PositionsPanel positions={positions} />
              <PnlStrip positions={positions} pnlHistory={pnlHistory} />
              <IvSummary current={ivCurrent} />
              <RegimeLegend />
            </div>

            <div className="flex-1 overflow-hidden border-t border-border">
              <div className="flex items-center justify-between px-4 py-2">
                <span className="text-sm font-medium uppercase tracking-wide text-text-muted">Live Reasoning Feed</span>
                <button
                  onClick={onExpandLogs}
                  aria-label="Expand reasoning feed"
                  title="Expand"
                  className="flex h-7 w-7 items-center justify-center rounded-md text-text-muted transition-colors hover:bg-accent hover:text-cta-text"
                >
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
                    <path
                      d="M6 2H2v4M10 2h4v4M6 14H2v-4M10 14h4v-4"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>
              </div>
              <div className="h-[calc(100%-2.5rem)]">
                <ReasoningFeed events={events} />
              </div>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
