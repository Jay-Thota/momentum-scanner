import Link from "next/link";
import { getStockDetail, getOptionsHistory } from "@/lib/api";
import ScoreBar from "@/components/ScoreBar";
import ArchetypeBadge from "@/components/ArchetypeBadge";
import RegimeBadge from "@/components/RegimeBadge";
import { ScoreChart, PcRatioChart } from "@/components/StockCharts";

function fmt(v: number | null | undefined): string {
  return v == null ? "--" : v.toFixed(2);
}

function scoreClass(total: number): string {
  if (total >= 5.0) return "text-green-400";
  if (total >= 4.0) return "text-blue-400";
  if (total >= 3.0) return "text-amber-400";
  return "text-gray-400";
}

export default async function StockPage({
  params,
}: {
  params: Promise<{ ticker: string }>;
}) {
  const { ticker } = await params;
  const sym = ticker.toUpperCase();

  const [detail, opts] = await Promise.allSettled([
    getStockDetail(sym),
    getOptionsHistory(sym),
  ]);

  if (detail.status === "rejected") {
    return (
      <div className="space-y-4">
        <Link
          href="/"
          className="text-gray-500 hover:text-white text-sm font-mono transition-colors"
        >
          ← Back to today
        </Link>
        <div className="rounded-lg border border-red-800/40 bg-red-900/10 px-4 py-3 text-red-400 text-sm font-mono">
          {sym} not found in the last 30 days of scans.
        </div>
      </div>
    );
  }

  const stockData = detail.value;
  const optsData = opts.status === "fulfilled" ? opts.value : null;
  const latest = stockData.appearances[0];

  return (
    <div className="space-y-8">
      <div className="flex items-center gap-3">
        <Link
          href="/"
          className="text-gray-500 hover:text-white text-sm font-mono transition-colors"
        >
          ← Back to today
        </Link>
      </div>

      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-bold tracking-tight font-mono text-white">
            {sym}
          </h1>
          {latest && (
            <div className="flex items-center gap-3 mt-2">
              <RegimeBadge regime={latest.regime} />
              <ArchetypeBadge archetype={latest.archetype} />
              <span className="text-gray-500 text-sm font-mono">
                last scanned {latest.scan_date}
              </span>
            </div>
          )}
        </div>
        {latest && (
          <div className="text-right">
            <div className={`text-4xl font-bold font-mono ${scoreClass(latest.total_score)}`}>
              {latest.total_score.toFixed(2)}
            </div>
            <div className="text-gray-500 text-xs font-mono mt-1">total score / 6.5</div>
          </div>
        )}
      </div>

      {/* Most recent ScoreBar */}
      {latest && (
        <div className="rounded-lg border border-gray-800 bg-gray-900/40 p-5">
          <p className="text-xs font-mono text-gray-500 uppercase tracking-wider mb-3">
            Layer scores (most recent scan)
          </p>
          <ScoreBar l1={latest.l1} l2={latest.l2} l3={latest.l3} l4={latest.l4} l5={latest.l5} />
          <div className="grid grid-cols-5 gap-2 mt-3 text-center text-xs font-mono text-gray-500">
            <span>L1 Vol<br />{fmt(latest.l1)}</span>
            <span>L2 Struct<br />{fmt(latest.l2)}</span>
            <span>L3 Opts<br />{fmt(latest.l3)}</span>
            <span>L4 Short<br />{fmt(latest.l4)}</span>
            <span>L5 Sect<br />{fmt(latest.l5)}</span>
          </div>
        </div>
      )}

      {/* Score over time */}
      {stockData.appearances.length > 1 && (
        <div className="rounded-lg border border-gray-800 bg-gray-900/40 p-5">
          <p className="text-xs font-mono text-gray-500 uppercase tracking-wider mb-4">
            Total score over time
          </p>
          <ScoreChart appearances={stockData.appearances} />
          <p className="text-xs font-mono text-gray-600 mt-2">
            Dashed lines: 4.0 threshold (blue) &bull; 5.0 strong (green)
          </p>
        </div>
      )}

      {/* Put/Call ratio chart */}
      {optsData && optsData.history.length > 0 && (
        <div className="rounded-lg border border-gray-800 bg-gray-900/40 p-5">
          <p className="text-xs font-mono text-gray-500 uppercase tracking-wider mb-4">
            Put / Call ratio history
          </p>
          <PcRatioChart history={optsData.history} />
        </div>
      )}

      {/* Appearances table */}
      <div className="rounded-lg border border-gray-800 bg-gray-900/40">
        <div className="px-5 py-3 border-b border-gray-800">
          <p className="text-xs font-mono text-gray-500 uppercase tracking-wider">
            Scan appearances (last 30 days) &bull; {stockData.appearances.length} total
          </p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-gray-500 text-xs uppercase tracking-wider border-b border-gray-800">
                <th className="py-3 px-4">Date</th>
                <th className="py-3 px-4">Score</th>
                <th className="py-3 px-4 min-w-[120px]">Layers</th>
                <th className="py-3 px-4">Arch</th>
                <th className="py-3 px-4">L1</th>
                <th className="py-3 px-4">L2</th>
                <th className="py-3 px-4">L3</th>
                <th className="py-3 px-4">L4</th>
                <th className="py-3 px-4">L5</th>
              </tr>
            </thead>
            <tbody>
              {stockData.appearances.map((row, idx) => (
                <tr
                  key={row.scan_date}
                  className={`border-b border-gray-800/60 ${idx % 2 === 1 ? "bg-gray-800/20" : ""}`}
                >
                  <td className="py-3 px-4 font-mono text-gray-400 text-xs">{row.scan_date}</td>
                  <td className="py-3 px-4 font-mono font-semibold">
                    <span className={scoreClass(row.total_score)}>{row.total_score.toFixed(2)}</span>
                  </td>
                  <td className="py-3 px-4">
                    <ScoreBar l1={row.l1} l2={row.l2} l3={row.l3} l4={row.l4} l5={row.l5} />
                  </td>
                  <td className="py-3 px-4"><ArchetypeBadge archetype={row.archetype} /></td>
                  <td className="py-3 px-4 font-mono text-xs text-gray-400">{fmt(row.l1)}</td>
                  <td className="py-3 px-4 font-mono text-xs text-gray-400">{fmt(row.l2)}</td>
                  <td className="py-3 px-4 font-mono text-xs text-gray-400">{fmt(row.l3)}</td>
                  <td className="py-3 px-4 font-mono text-xs text-gray-400">{fmt(row.l4)}</td>
                  <td className="py-3 px-4 font-mono text-xs text-gray-400">{fmt(row.l5)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
