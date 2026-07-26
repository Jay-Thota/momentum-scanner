export interface WatchlistEntry {
  ticker: string;
  total_score: number;
  price: number | null;
  l1: number | null;
  l2: number | null;
  l3: number | null;
  l4: number | null;
  l5: number | null;
  archetype: string | null;
  regime: string | null;
  timestamp: string | null;
  signals: Record<string, unknown>;
}

export interface ScanResult {
  scan_date: string;
  results: WatchlistEntry[];
}

export interface HistoryEntry {
  scan_date: string;
  stock_count: number;
  top_ticker: string | null;
  top_score: number | null;
  regime: string | null;
}

export interface StockAppearance {
  scan_date: string;
  ticker: string;
  total_score: number;
  price: number | null;
  l1: number | null;
  l2: number | null;
  l3: number | null;
  l4: number | null;
  l5: number | null;
  archetype: string | null;
  regime: string | null;
  timestamp: string | null;
  signals: Record<string, unknown>;
}

export interface StockMeta {
  sector: string | null;
  etf_symbol: string | null;
  si_pct: number | null;
  days_to_cover: number | null;
}

export interface StockDetail {
  ticker: string;
  appearances: StockAppearance[];
  meta: StockMeta;
}

export interface OptionsHistoryEntry {
  scan_date: string;
  put_call_ratio: number | null;
  call_vol_oi_ratio: number | null;
  iv_slope: number | null;
}

export interface OptionsHistory {
  ticker: string;
  history: OptionsHistoryEntry[];
}

export interface HealthResponse {
  status: string;
  last_scan: string | null;
  universe_size: number;
  regime: string | null;
}
