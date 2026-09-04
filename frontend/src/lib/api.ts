import type { DecisionEvent, IvHistoryPoint, IvResult, LoopStatus, Position, PriceBar, RegimePoint } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json() as Promise<T>;
}

export function getPriceHistory(symbol: string, days: number) {
  return getJson<{ symbol: string; bars: PriceBar[] }>(`/api/price_history?symbol=${symbol}&days=${days}`);
}

export function getRegimeHistory(symbol: string, days: number) {
  return getJson<{ symbol: string; points: RegimePoint[] }>(`/api/regime_history?symbol=${symbol}&days=${days}`);
}

export function getWatchlist() {
  return getJson<{ symbols: string[] }>(`/api/watchlist`);
}

export function getIvHistory(symbol: string) {
  return getJson<{ symbol: string; history: IvHistoryPoint[]; current: IvResult }>(`/api/iv_history?symbol=${symbol}`);
}

export function getDecisionLog(limit = 100) {
  return getJson<{ events: DecisionEvent[] }>(`/api/decision_log?limit=${limit}`);
}

export function getOpenPositions() {
  return getJson<{ positions: Position[] }>(`/api/open_positions`);
}

export function getLoopStatus() {
  return getJson<LoopStatus>(`/api/loop/status`);
}

async function postJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method: "POST" });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json() as Promise<T>;
}

export function startLoop() {
  return postJson<LoopStatus>(`/api/loop/start`);
}

export function stopLoop() {
  return postJson<LoopStatus>(`/api/loop/stop`);
}
