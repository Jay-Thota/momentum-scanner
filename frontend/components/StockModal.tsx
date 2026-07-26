"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import type { StockMeta, WatchlistEntry } from "@/lib/types";
import { getStockDetail } from "@/lib/api";
import ArchetypeBadge from "./ArchetypeBadge";
import RegimeBadge from "./RegimeBadge";
import ScoreBar from "./ScoreBar";

interface Props {
  entry: WatchlistEntry;
  scanDate: string;
  onClose: () => void;
}

function DataCard({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-gray-500 text-xs uppercase tracking-wider font-mono">
        {label}
      </span>
      <span className="text-white text-sm font-mono">{children}</span>
    </div>
  );
}

function scoreColor(total: number): string {
  if (total >= 5.0) return "text-green-400";
  if (total >= 4.0) return "text-blue-400";
  if (total >= 3.0) return "text-amber-400";
  return "text-gray-400";
}

const LAYER_DEFS = [
  { key: "l1" as const, label: "L1 Vol",    max: 1.0, color: "text-cyan-400"   },
  { key: "l2" as const, label: "L2 Struct",  max: 2.0, color: "text-blue-400"   },
  { key: "l3" as const, label: "L3 Opts",    max: 1.0, color: "text-violet-400" },
  { key: "l4" as const, label: "L4 Short",   max: 1.0, color: "text-amber-400"  },
  { key: "l5" as const, label: "L5 Sector",  max: 1.5, color: "text-green-400"  },
];

export default function StockModal({ entry, scanDate, onClose }: Props) {
  const { ticker } = entry;
  const [meta, setMeta] = useState<StockMeta | null>(null);
  const [visible, setVisible] = useState(false);
  const chartInitialized = useRef(false);

  // Fade-in on mount; lock body scroll
  useEffect(() => {
    const raf = requestAnimationFrame(() => setVisible(true));
    document.body.style.overflow = "hidden";
    return () => {
      cancelAnimationFrame(raf);
      document.body.style.overflow = "";
    };
  }, []);

  // ESC to close
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  // Fetch sector / SI metadata
  useEffect(() => {
    let cancelled = false;
    getStockDetail(ticker)
      .then((d) => { if (!cancelled) setMeta(d.meta ?? null); })
      .catch(() => { if (!cancelled) setMeta(null); });
    return () => { cancelled = true; };
  }, [ticker]);

  // TradingView chart
  useEffect(() => {
    if (chartInitialized.current) return;
    let cancelled = false;

    const build = () => {
      if (cancelled) return;
      chartInitialized.current = true;
      new (window as any).TradingView.widget({
        container_id: `tv_chart_${ticker}`,
        symbol: ticker,
        interval: "D",
        timezone: "America/New_York",
        theme: "dark",
        style: "1",
        locale: "en",
        toolbar_bg: "#0f1117",
        enable_publishing: false,
        hide_top_toolbar: false,
        hide_legend: false,
        save_image: false,
        height: 450,
        width: "100%",
      });
    };

    if ((window as any).TradingView) {
      build();
    } else {
      let script = document.querySelector<HTMLScriptElement>(
        'script[src="https://s3.tradingview.com/tv.js"]'
      );
      if (!script) {
        script = document.createElement("script");
        script.src = "https://s3.tradingview.com/tv.js";
        script.async = true;
        document.head.appendChild(script);
      }
      script.addEventListener("load", build, { once: true });
    }

    return () => { cancelled = true; };
  }, [ticker]);

  const fmtPct = (v: number | null) =>
    v == null ? "--" : `${(v * 100).toFixed(1)}%`;
  const fmtDtc = (v: number | null) =>
    v == null ? "--" : `${v.toFixed(1)}d`;
  const fmtPrice = (v: number | null) =>
    v == null
      ? "--"
      : `$${v.toLocaleString("en-US", {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })}`;

  return (
    <div
      className={`fixed inset-0 z-50 flex items-start justify-center pt-6 pb-6 px-4 bg-black/75 backdrop-blur-sm transition-opacity duration-200 ${
        visible ? "opacity-100" : "opacity-0"
      }`}
      onClick={onClose}
    >
      <div
        className="relative w-full max-w-4xl max-h-[calc(100vh-3rem)] overflow-y-auto bg-[#0f1117] border border-gray-700 rounded-xl shadow-2xl flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-800 sticky top-0 bg-[#0f1117] z-10">
          <div className="flex items-center gap-3 min-w-0">
            <span className="text-xl font-bold font-mono text-white tracking-wider">
              {ticker}
            </span>
            <span className="text-gray-600 text-xs font-mono">{scanDate}</span>
            {entry.price != null && (
              <span className="text-gray-400 text-sm font-mono">
                {fmtPrice(entry.price)}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-white transition-colors text-lg leading-none p-1.5 rounded hover:bg-gray-800 flex-shrink-0"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        {/* Section 1: TradingView chart */}
        <div className="w-full bg-[#0f1117] flex-shrink-0">
          <div
            id={`tv_chart_${ticker}`}
            className="w-full"
            style={{ minHeight: 450 }}
          />
        </div>

        {/* Section 2: Scanner data */}
        <div className="px-5 py-4 border-t border-gray-800 space-y-4">
          {/* Score row */}
          <div className="flex items-center gap-6 flex-wrap">
            <div className="flex flex-col gap-0.5">
              <span className="text-gray-500 text-xs uppercase tracking-wider font-mono">
                Total Score
              </span>
              <span
                className={`text-2xl font-bold font-mono ${scoreColor(
                  entry.total_score
                )}`}
              >
                {entry.total_score.toFixed(2)}
                <span className="text-gray-700 text-sm font-normal">/6.5</span>
              </span>
            </div>

            <div className="flex flex-col gap-1">
              <span className="text-gray-500 text-xs uppercase tracking-wider font-mono">
                Archetype
              </span>
              <ArchetypeBadge archetype={entry.archetype} />
            </div>

            <div className="flex flex-col gap-1">
              <span className="text-gray-500 text-xs uppercase tracking-wider font-mono">
                Regime
              </span>
              <RegimeBadge regime={entry.regime} />
            </div>
          </div>

          {/* ScoreBar */}
          <div>
            <span className="text-gray-500 text-xs uppercase tracking-wider font-mono block mb-1.5">
              Layer breakdown
            </span>
            <ScoreBar
              l1={entry.l1}
              l2={entry.l2}
              l3={entry.l3}
              l4={entry.l4}
              l5={entry.l5}
            />
          </div>

          {/* Individual layer cards */}
          <div className="grid grid-cols-5 gap-2">
            {LAYER_DEFS.map(({ key, label, max, color }) => {
              const val = entry[key];
              return (
                <div
                  key={key}
                  className="bg-gray-800/50 border border-gray-700/50 rounded-lg px-3 py-2 text-center"
                >
                  <div className="text-gray-500 text-xs uppercase tracking-wider font-mono mb-0.5">
                    {label}
                  </div>
                  <div className={`font-mono text-sm font-semibold ${color}`}>
                    {val == null ? "--" : val.toFixed(2)}
                  </div>
                  <div className="text-gray-700 text-xs font-mono">
                    /{max.toFixed(1)}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Metadata row */}
          <div className="flex flex-wrap gap-x-8 gap-y-3 pt-2 border-t border-gray-800/60">
            <DataCard label="Sector">{meta?.sector ?? "--"}</DataCard>
            <DataCard label="ETF">{meta?.etf_symbol ?? "--"}</DataCard>
            <DataCard label="Short %">{fmtPct(meta?.si_pct ?? null)}</DataCard>
            <DataCard label="Days to Cover">
              {fmtDtc(meta?.days_to_cover ?? null)}
            </DataCard>
          </div>
        </div>

        {/* Section 3: Quick links */}
        <div className="px-5 py-3 border-t border-gray-800 flex flex-wrap gap-2">
          <Link
            href={`/stock/${ticker}`}
            onClick={onClose}
            className="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 hover:text-white text-xs font-mono transition-colors border border-gray-700 hover:border-gray-600"
          >
            View History
          </Link>
          <a
            href={`https://www.tradingview.com/chart/?symbol=${ticker}`}
            target="_blank"
            rel="noopener noreferrer"
            className="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 hover:text-white text-xs font-mono transition-colors border border-gray-700 hover:border-gray-600"
          >
            TradingView ↗
          </a>
          <a
            href={`https://finance.yahoo.com/quote/${ticker}`}
            target="_blank"
            rel="noopener noreferrer"
            className="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 hover:text-white text-xs font-mono transition-colors border border-gray-700 hover:border-gray-600"
          >
            Yahoo Finance ↗
          </a>
          <a
            href={`https://finviz.com/quote.ashx?t=${ticker}`}
            target="_blank"
            rel="noopener noreferrer"
            className="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 hover:text-white text-xs font-mono transition-colors border border-gray-700 hover:border-gray-600"
          >
            Finviz ↗
          </a>
        </div>
      </div>
    </div>
  );
}
