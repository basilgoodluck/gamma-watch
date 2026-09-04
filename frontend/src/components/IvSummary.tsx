import IvGauge from "./IvGauge";
import type { IvResult } from "@/lib/types";

export default function IvSummary({ current }: { current: IvResult | null }) {
  return (
    <div>
      <div className="mb-2 text-sm font-medium uppercase tracking-wide text-text">IV Rank</div>
      {!current || current.atm_iv === null || current.iv_rank === null ? (
        <div className="text-base text-text-muted">No IV reading yet.</div>
      ) : (
        <div className="flex items-center gap-3 text-text ">
          <IvGauge value={current.iv_rank} />
          <div>
            <div className="font-tabular text-xl text-text">{(current.atm_iv * 100).toFixed(1)}%</div>
            <div className="text-sm text-text-muted">atm IV{current.cold_start && " · cold start"}</div>
          </div>
        </div>
      )}
    </div>
  );
}
