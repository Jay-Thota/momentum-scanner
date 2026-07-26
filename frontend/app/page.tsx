"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getWatchlistToday } from "@/lib/api";
import type { ScanResult, WatchlistEntry } from "@/lib/types";
import RegimeBadge from "@/components/RegimeBadge";
import ScanStats from "@/components/ScanStats";
import StockModal from "@/components/StockModal";
import WatchlistTable from "@/components/WatchlistTable";

function isAutoRefreshWindow(): boolean {
  const now = new Date();
  const et = new Date(now.toLocaleString("en-US", { timeZone: "America/New_York" }));
  const h = et.getHours();
  const m = et.getMinutes();
  return (h === 16 && m >= 0 && m <= 15);
}

export default function TodayPage() {
  const [data, setData] = useState<ScanResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedEntry, setSelectedEntry] = useState<WatchlistEntry | null>(null);
  const lastScanDateRef = useRef<string | null>(null);

  const fetchData = useCallback(async () => {
    try {
      const result = await getWatchlistToday();
      setData(result);
      setError(null);
      lastScanDateRef.current = result.scan_date;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load watchlist");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();

    const id = setInterval(() => {
      if (isAutoRefreshWindow()) fetchData();
    }, 60_000);
    return () => clearInterval(id);
  }, [fetchData]);

  const today = new Date().toLocaleDateString("en-US", {
    timeZone: "America/New_York",
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  });

  // Compare scan_date against the ET calendar date (not UTC) to avoid false
  // "stale data" banners late at night when UTC has already rolled over.
  const todayET = new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  const isToday = data?.scan_date === todayET;

  return (
    <>
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold tracking-tight font-mono text-white">
            Today&apos;s Watchlist
          </h1>
          <p className="text-gray-500 text-sm mt-1 font-mono">{today}</p>
        </div>
        <div className="flex flex-col items-end gap-2">
          {data && (
            <RegimeBadge
              regime={data.results[0]?.regime ?? null}
            />
          )}
        </div>
      </div>

      {data && (
        <ScanStats
          lastUpdated={data.results[0]?.timestamp ?? null}
        />
      )}

      {loading && (
        <div className="text-gray-500 text-sm font-mono py-12 text-center">
          Loading watchlist...
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-red-800/40 bg-red-900/10 px-4 py-3 text-red-400 text-sm font-mono">
          {error}
        </div>
      )}

      {!loading && data && !isToday && (
        <div className="rounded-lg border border-amber-800/40 bg-amber-900/10 px-4 py-3 text-amber-400 text-sm font-mono">
          Scan runs at 3:30 PM ET -- last scan was {data.scan_date}
        </div>
      )}

      {!loading && data && (
        <div className="rounded-lg border border-gray-800 bg-gray-900/40">
          <div className="px-4 py-3 border-b border-gray-800 flex items-center justify-between">
            <span className="text-xs font-mono text-gray-500 uppercase tracking-wider">
              {data.results.length} results &bull; scan {data.scan_date}
            </span>
          </div>
          <WatchlistTable
            entries={data.results}
            onRowClick={setSelectedEntry}
          />
        </div>
      )}
    </div>

    {selectedEntry && data && (
      <StockModal
        entry={selectedEntry}
        scanDate={data.scan_date}
        onClose={() => setSelectedEntry(null)}
      />
    )}
    </>
  );
}
