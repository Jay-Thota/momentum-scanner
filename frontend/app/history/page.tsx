"use client";

import { useEffect, useState } from "react";
import { getWatchlistHistory, getWatchlistByDate } from "@/lib/api";
import type { HistoryEntry, ScanResult } from "@/lib/types";
import RegimeBadge from "@/components/RegimeBadge";
import WatchlistTable from "@/components/WatchlistTable";

export default function HistoryPage() {
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [expandedData, setExpandedData] = useState<Record<string, ScanResult>>({});
  const [expandLoading, setExpandLoading] = useState<string | null>(null);

  useEffect(() => {
    getWatchlistHistory()
      .then(setHistory)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load history"))
      .finally(() => setLoading(false));
  }, []);

  async function toggleDate(date: string) {
    if (expanded === date) {
      setExpanded(null);
      return;
    }
    setExpanded(date);
    if (expandedData[date]) return;

    setExpandLoading(date);
    try {
      const result = await getWatchlistByDate(date);
      setExpandedData((prev) => ({ ...prev, [date]: result }));
    } catch {
      // leave expandedData empty for this date
    } finally {
      setExpandLoading(null);
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold tracking-tight font-mono text-white">
        Scan History
      </h1>

      {loading && (
        <div className="text-gray-500 text-sm font-mono py-12 text-center">
          Loading history...
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-red-800/40 bg-red-900/10 px-4 py-3 text-red-400 text-sm font-mono">
          {error}
        </div>
      )}

      {!loading && history.length === 0 && !error && (
        <div className="text-gray-500 text-sm font-mono py-8 text-center">
          No scans recorded yet.
        </div>
      )}

      <div className="space-y-2">
        {history.map((entry) => (
          <div key={entry.scan_date} className="rounded-lg border border-gray-800 overflow-hidden">
            <button
              onClick={() => toggleDate(entry.scan_date)}
              className="w-full px-5 py-4 flex items-center justify-between text-left hover:bg-gray-800/40 transition-colors"
            >
              <div className="flex items-center gap-5">
                <span className="font-mono text-white font-semibold text-sm">
                  {entry.scan_date}
                </span>
                <span className="text-gray-500 text-xs font-mono">
                  {entry.stock_count} stocks
                </span>
                {entry.top_ticker && (
                  <span className="text-xs font-mono text-gray-400">
                    top: <span className="text-white">{entry.top_ticker}</span>{" "}
                    {entry.top_score != null && (
                      <span className="text-green-400">{entry.top_score.toFixed(2)}</span>
                    )}
                  </span>
                )}
              </div>
              <div className="flex items-center gap-3">
                {entry.regime && <RegimeBadge regime={entry.regime} />}
                <span className="text-gray-600 text-xs font-mono">
                  {expanded === entry.scan_date ? "▲ hide" : "▼ expand"}
                </span>
              </div>
            </button>

            {expanded === entry.scan_date && (
              <div className="border-t border-gray-800 bg-gray-900/20">
                {expandLoading === entry.scan_date ? (
                  <div className="text-gray-500 text-sm font-mono px-5 py-6 text-center">
                    Loading...
                  </div>
                ) : expandedData[entry.scan_date] ? (
                  <WatchlistTable entries={expandedData[entry.scan_date].results} />
                ) : (
                  <div className="text-gray-600 text-sm font-mono px-5 py-4">
                    Could not load data for this date.
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
