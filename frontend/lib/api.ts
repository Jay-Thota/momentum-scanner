import type {
  HealthResponse,
  HistoryEntry,
  OptionsHistory,
  ScanResult,
  StockDetail,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function apiFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store" });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`API ${res.status}: ${body}`);
  }
  return res.json() as Promise<T>;
}

export function getWatchlistToday(): Promise<ScanResult> {
  return apiFetch<ScanResult>("/watchlist/today");
}

export function getWatchlistByDate(date: string): Promise<ScanResult> {
  return apiFetch<ScanResult>(`/watchlist/${date}`);
}

export function getWatchlistHistory(): Promise<HistoryEntry[]> {
  return apiFetch<HistoryEntry[]>("/watchlist/history");
}

export function getStockDetail(ticker: string): Promise<StockDetail> {
  return apiFetch<StockDetail>(`/stock/${ticker}`);
}

export function getOptionsHistory(ticker: string): Promise<OptionsHistory> {
  return apiFetch<OptionsHistory>(`/stock/${ticker}/options-history`);
}

export function getHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/health");
}
