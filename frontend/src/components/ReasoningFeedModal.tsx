"use client";

import { AnimatePresence, motion } from "framer-motion";
import ReasoningFeed from "./ReasoningFeed";
import { SPRING } from "@/lib/motion";
import type { DecisionEvent } from "@/lib/types";

export default function ReasoningFeedModal({
  open,
  onClose,
  events,
}: {
  open: boolean;
  onClose: () => void;
  events: DecisionEvent[];
}) {
  return (
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-6">
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={SPRING}
            className="absolute inset-0 bg-black/50"
            onClick={onClose}
          />

          {/* Centered card - spring transition (overshoots slightly on open,
              settles), not a linear ease. */}
          <motion.div
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.92 }}
            transition={SPRING}
            className="relative flex h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-2xl"
          >
            <div className="flex items-center justify-between border-b border-border px-6 py-4">
              <span className="text-lg font-medium text-text">Live Reasoning Feed</span>
              <button
                onClick={onClose}
                aria-label="Close reasoning feed"
                className="flex h-8 w-8 items-center justify-center rounded-md text-text-muted transition-colors hover:bg-accent hover:text-cta-text"
              >
                ✕
              </button>
            </div>
            <div className="min-h-0 flex-1">
              <ReasoningFeed events={events} />
            </div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  );
}
