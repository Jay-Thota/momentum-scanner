"use client";

import { useRouter } from "next/navigation";
import ArchetypeBadge from "./ArchetypeBadge";
import ScoreBar from "./ScoreBar";
import type { WatchlistEntry } from "@/lib/types";

interface Props {
  entries: WatchlistEntry[];
  onRowClick?: (entry: WatchlistEntry) => void;
}

function scoreClass(total: number): string {
  if (total >= 5.0) return "text-green-400 font-bold";
  if (total >= 4.0) return "text-blue-400 font-bold";
  if (total >= 3.0) return "text-amber-400 font-semibold";
  return "text-gray-400";
}

function fmt(v: number | null): string {
  return v == null ? "--" : v.toFixed(2);
}

function fmtPrice(v: number | null): string {
  if (v == null) return "--";
  return `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export default function WatchlistTable({ entries, onRowClick }: Props) {
  const router = useRouter();

  if (entries.length === 0) {
    return (
      <div className="text-gray-500 text-sm py-8 text-center">
        No results for this scan.
      </div>
    );
  }

  // Break ties: total_score desc → l2 desc → l1 desc
  const sorted = [...entries].sort((a, b) => {
    const dTotal = b.total_score - a.total_score;
    if (Math.abs(dTotal) > 0.001) return dTotal;
    const dL2 = (b.l2 ?? 0) - (a.l2 ?? 0);
    if (Math.abs(dL2) > 0.001) return dL2;
    return (b.l1 ?? 0) - (a.l1 ?? 0);
  });

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="text-gray-500 text-xs uppercase tracking-wider border-b border-gray-800">
            <th className="py-3 px-3 w-10 text-left">#</th>
            <th className="py-3 px-3 text-left">Ticker</th>
            <th className="py-3 px-3 text-left">Price</th>
            <th className="py-3 px-3 text-left">Score</th>
            <th className="py-3 px-3 min-w-[180px] text-left">Layers</th>
            <th className="py-3 px-3 text-left">Archetype</th>
            <th className="py-3 px-3 text-center">L1</th>
            <th className="py-3 px-3 text-center">L2</th>
            <th className="py-3 px-3 text-center">L3</th>
            <th className="py-3 px-3 text-center">L4</th>
            <th className="py-3 px-3 text-center">L5</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, idx) => {
            const rank = idx + 1;
            const isTop = rank === 1;
            const isEven = rank % 2 === 0;
            const price = row.price;
            const l3IsZero = (row.l3 ?? 0) === 0;

            return (
              <tr
                key={row.ticker}
                onClick={() =>
                  onRowClick
                    ? onRowClick(row)
                    : router.push(`/stock/${row.ticker}`)
                }
                className={`cursor-pointer border-b border-gray-800/60 transition-colors hover:bg-gray-700/30 ${
                  isTop
                    ? "bg-yellow-900/10 hover:bg-yellow-900/20"
                    : isEven
                    ? "bg-gray-800/20"
                    : ""
                }`}
                style={isTop ? { borderLeft: "2px solid #f59e0b" } : { borderLeft: "2px solid transparent" }}
              >
                <td className="py-3 px-3 text-gray-500 text-xs font-mono">
                  {isTop ? (
                    <span className="text-yellow-400 font-bold">1</span>
                  ) : (
                    rank
                  )}
                </td>
                <td className="py-3 px-3 font-mono font-semibold text-white tracking-wider">
                  {row.ticker}
                </td>
                <td className="py-3 px-3 font-mono text-xs text-gray-300">
                  {fmtPrice(price)}
                </td>
                <td className="py-3 px-3">
                  <span className={`font-mono text-sm ${scoreClass(row.total_score)}`}>
                    {row.total_score.toFixed(2)}
                  </span>
                </td>
                <td className="py-3 px-3">
                  <ScoreBar
                    l1={row.l1}
                    l2={row.l2}
                    l3={row.l3}
                    l4={row.l4}
                    l5={row.l5}
                  />
                </td>
                <td className="py-3 px-3">
                  <ArchetypeBadge archetype={row.archetype} />
                </td>
                <td className="py-3 px-3 font-mono text-xs text-gray-400 text-center">{fmt(row.l1)}</td>
                <td className="py-3 px-3 font-mono text-xs text-gray-400 text-center">{fmt(row.l2)}</td>
                <td className="py-3 px-3 font-mono text-xs text-center">
                  <span className={l3IsZero ? "text-gray-700" : "text-gray-400"}>
                    {l3IsZero ? "--" : fmt(row.l3)}
                  </span>
                </td>
                <td className="py-3 px-3 font-mono text-xs text-gray-400 text-center">{fmt(row.l4)}</td>
                <td className="py-3 px-3 font-mono text-xs text-gray-400 text-center">{fmt(row.l5)}</td>
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={11} className="px-3 py-2 text-xs text-gray-700 font-mono border-t border-gray-800/60">
              L3 (options flow) shows -- when unavailable on free data stack
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
