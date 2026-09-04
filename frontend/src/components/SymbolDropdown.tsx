"use client";

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { SPRING_SNAPPY } from "@/lib/motion";

export default function SymbolDropdown({
  symbols,
  value,
  onChange,
}: {
  symbols: string[];
  value: string;
  onChange: (symbol: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [open]);

  return (
    <div ref={rootRef} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="flex h-9 items-center gap-2 rounded-md border border-border bg-black px-4 text-sm font-medium text-white outline-none transition-colors hover:opacity-85"
      >
        {value}
        <svg
          width="10"
          height="10"
          viewBox="0 0 10 10"
          fill="none"
          className={`transition-transform duration-150 ${open ? "rotate-180" : ""}`}
        >
          <path d="M2 3.5L5 6.5L8 3.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {/* Spring open/close, same physical system as the panels drawer and
          popovers (doc/visual_reasoning_redesign.md). */}
      <AnimatePresence>
        {open && (
          <motion.div
            role="listbox"
            initial={{ opacity: 0, scale: 0.9, y: -4 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.92, y: -2 }}
            transition={SPRING_SNAPPY}
            className="absolute left-0 top-full z-20 mt-1.5 min-w-full origin-top overflow-hidden rounded-md border border-border bg-surface shadow-lg"
          >
            {symbols.map((s) => (
              <button
                key={s}
                role="option"
                aria-selected={s === value}
                onClick={() => {
                  onChange(s);
                  setOpen(false);
                }}
                className={`block w-full whitespace-nowrap px-4 py-2 text-left text-sm font-medium ${
                  s === value ? "bg-accent text-cta-text" : "text-text hover:bg-surface-2"
                }`}
              >
                {s}
              </button>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
