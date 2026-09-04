export type Regime = "TREND_CALM" | "TREND_VOLATILE" | "RANGE_CALM" | "RANGE_VOLATILE" | "BREAKOUT";

export interface PriceBar {
  ts: string;
  open: number;
  close: number;
  high: number;
  low: number;
}

export interface RegimePoint {
  symbol: string;
  regime: Regime;
  probabilities: Record<Regime, number>;
  confidence: number;
  trend_direction: string;
  ready: boolean;
  ts: string;
}

export interface IvHistoryPoint {
  date: string;
  atm_iv: number;
}

export interface IvResult {
  symbol: string;
  atm_iv: number | null;
  iv_rank: number | null;
  cold_start: boolean;
}

export interface DecisionEvent {
  ts: string;
  trace_id: string | null;
  step: string;
  input: unknown;
  output: unknown;
  reason: string;
}

export interface LoopStatus {
  running: boolean;
  next_fire_at: string | null;
  last_cycle_at: string | null;
  alive: boolean;
}

export interface Position {
  id: string;
  trace_id: string | null;
  symbol: string;
  type: string;
  contracts: number;
  entry_price: number;
  max_loss: number;
  max_gain: number | "unlimited";
  expiry: string;
  status: "open" | "closed" | "cancelled"; // "cancelled": entry order never filled, no real trade occurred
  outcome?: "WIN" | "LOSS" | "NEUTRAL";
  realized_pnl?: number;
  current_pnl_pct: number | null;
  profit_target_pct: number | null; // progress toward the REAL profit target (max_gain-based), not max_loss-relative
}

