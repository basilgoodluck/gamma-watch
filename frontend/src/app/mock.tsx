import Link from "next/link";

export default function Home() {
  return (
    <div className="min-h-screen bg-marketing-bg text-marketing-text">
      <div className="mx-auto max-w-[640px] px-6 py-24 sm:py-32">
        <div className="mb-16 text-sm font-semibold uppercase tracking-widest text-marketing-text">Gamma Watch</div>

        <section>
          <h1 className="text-4xl font-semibold leading-tight tracking-tight sm:text-5xl">
            Options trading rewards a discipline most people don&apos;t have.
          </h1>
          <p className="mt-6 text-lg leading-relaxed text-marketing-text-muted">
            It requires reading market structure most traders were never trained to read, and it punishes the two
            things people are worst at under pressure: sizing risk consistently, and not trading on feeling. Most
            retail options activity fails for the same handful of reasons — a position sized on conviction instead of
            a rule, a losing streak chased instead of stood down from, and no real read on whether the market is
            trending, ranging, or about to break, before the trade goes on.
          </p>
        </section>

        <div className="my-16 h-px bg-marketing-divider" />

        <section>
          <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">
            A systematic layer between judgment and execution.
          </h2>
          <p className="mt-6 text-lg leading-relaxed text-marketing-text-muted">
            This system replaces in-the-moment judgment calls with rules applied the same way every time. Every trade
            carries a defined maximum loss set before entry, never adjusted after. A drawdown ceiling forces a full
            stand-down before losses compound. And because the decision to size or take a trade never depends on a
            person&apos;s attention in that moment, it can watch several assets at once, continuously, with the same
            discipline on the fifth one as the first.
          </p>
        </section>

        <div className="my-16 h-px bg-marketing-divider" />

        <section>
          <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Attention doesn&apos;t scale. A system does.</h2>
          <p className="mt-6 text-lg leading-relaxed text-marketing-text-muted">
            A person watching one ticker closely can&apos;t watch five as closely — attention is the constraint, not
            capital. A system that enforces the same risk rule and reads the same regime signal on every asset removes
            that ceiling: monitoring a wider book costs no more discipline than monitoring a single position, and the
            risk limits hold exactly as well on trade fifty as on trade one.
          </p>
        </section>

        <div className="my-16 h-px bg-marketing-divider" />

        <section>
          <h3 className="text-sm font-medium uppercase tracking-wide text-marketing-text-muted">Under the hood</h3>
          <p className="mt-3 text-base leading-relaxed text-marketing-text-muted">
            It runs on live market and options data, classifying the current regime and sizing every trade against a
            hard per-trade and portfolio risk cap before anything is placed.
          </p>
        </section>

        <div className="mt-16">
          <Link
            href="/demo"
            className="inline-block border border-marketing-text bg-marketing-text px-6 py-3 text-sm font-medium text-marketing-bg transition-opacity hover:opacity-90"
          >
            See it running →
          </Link>
        </div>
      </div>
    </div>
  );
}


