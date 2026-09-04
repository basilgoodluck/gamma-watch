"use client";

import Link from "next/link";
import { motion } from "framer-motion";

import type { Variants } from "framer-motion";

// doc-less hotfix: this file imported `lucide-react`, a package never
// installed in this project (frontend/package.json has no such dependency),
// which hard-fails Next.js's build for EVERY route, not just this page - a
// module-not-found error blocks the whole dev server. Replaced the one icon
// usage with an inline SVG (matching how every other icon in this codebase
// is done - see the header's pause/play/hamburger icons) instead of adding
// a new dependency without asking first, per this project's CLAUDE.md.
function ArrowUpRight({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path
        d="M4 12L12 4M12 4H5.5M12 4V10.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

const fadeUp: Variants = {
  hidden: { opacity: 0, y: 16 },
  show: (delay: number) => ({
    opacity: 1,
    y: 0,
    transition: { duration: 0.6, delay, ease: [0.16, 1, 0.3, 1] },
  }),
};

export default function Home() {
  return (
    <div className="relative flex min-h-screen flex-col justify-center overflow-hidden bg-marketing-bg text-marketing-text">
      {/* Ambient moving background - slow drifting glow + a faint live "pulse" line */}
      <div className="pointer-events-none absolute inset-0">
        <motion.div
          className="absolute -right-40 -top-40 h-[36rem] w-[36rem] rounded-full bg-accent/10 blur-3xl"
          animate={{ x: [0, 40, 0], y: [0, 30, 0] }}
          transition={{ duration: 18, repeat: Infinity, ease: "easeInOut" }}
        />
        <motion.div
          className="absolute -bottom-40 -left-40 h-[30rem] w-[30rem] rounded-full bg-accent-light/10 blur-3xl"
          animate={{ x: [0, -30, 0], y: [0, -20, 0] }}
          transition={{ duration: 22, repeat: Infinity, ease: "easeInOut" }}
        />

        <svg
          className="absolute inset-x-0 bottom-0 h-64 w-full opacity-[0.15]"
          viewBox="0 0 1200 200"
          preserveAspectRatio="none"
        >
          <motion.path
            d="M0,150 L80,140 L140,160 L200,90 L260,120 L320,60 L380,100 L440,40 L500,80 L560,50 L620,110 L680,70 L740,130 L800,60 L860,100 L920,45 L980,90 L1040,55 L1100,95 L1160,70 L1200,100"
            fill="none"
            stroke="currentColor"
            className="text-accent-light"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            initial={{ pathLength: 0 }}
            animate={{ pathLength: 1 }}
            transition={{ duration: 3.5, ease: "easeInOut" }}
          />
        </svg>
      </div>

      <div className="relative mx-auto w-full max-w-[720px] px-6">
        <motion.div
          variants={fadeUp}
          initial="hidden"
          animate="show"
          custom={0}
          className="mb-10 text-sm font-semibold uppercase tracking-widest text-marketing-text-muted"
        >
          Gamma Watch
        </motion.div>

        <motion.h1
          variants={fadeUp}
          initial="hidden"
          animate="show"
          custom={0.1}
          className="text-5xl font-semibold leading-[1.05] tracking-tight sm:text-7xl"
        >
          Discipline that
          <br />
          never blinks.
        </motion.h1>

        <motion.p
          variants={fadeUp}
          initial="hidden"
          animate="show"
          custom={0.25}
          className="mt-8 max-w-md text-lg leading-relaxed text-marketing-text-muted"
        >
          An autonomous options agent that reads market regime, sizes every risk before it takes it, and never
          trades on feeling.
        </motion.p>

        <motion.div
          variants={fadeUp}
          initial="hidden"
          animate="show"
          custom={0.4}
          className="mt-12 flex items-center gap-6"
        >
          <Link
            href="/demo"
            className="group inline-flex items-center gap-2 border border-marketing-text bg-marketing-text px-7 py-3.5 text-sm font-medium text-marketing-bg transition-opacity hover:opacity-90"
          >
            See it running
            <ArrowUpRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5 group-hover:-translate-y-0.5" />
          </Link>
          <span className="text-sm text-marketing-text-muted">Live on Alpaca paper trading</span>
        </motion.div>
      </div>
    </div>
  );
}