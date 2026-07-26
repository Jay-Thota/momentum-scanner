"use client";

import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";
import type { StockAppearance, OptionsHistoryEntry } from "@/lib/types";

interface ScoreChartProps {
  appearances: StockAppearance[];
}

export function ScoreChart({ appearances }: ScoreChartProps) {
  const data = [...appearances]
    .sort((a, b) => a.scan_date.localeCompare(b.scan_date))
    .map((a) => ({ date: a.scan_date, score: a.total_score }));

  return (
    <ResponsiveContainer width="100%" height={200}>
      <LineChart data={data} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          dataKey="date"
          tick={{ fill: "#6b7280", fontSize: 11, fontFamily: "monospace" }}
          tickLine={false}
          axisLine={false}
        />
        <YAxis
          domain={[0, 6.5]}
          tick={{ fill: "#6b7280", fontSize: 11, fontFamily: "monospace" }}
          tickLine={false}
          axisLine={false}
        />
        <Tooltip
          contentStyle={{
            background: "#111827",
            border: "1px solid #374151",
            borderRadius: 6,
            fontSize: 12,
            fontFamily: "monospace",
          }}
          labelStyle={{ color: "#9ca3af" }}
          itemStyle={{ color: "#34d399" }}
        />
        <ReferenceLine y={4.0} stroke="#3b82f6" strokeDasharray="4 4" strokeWidth={1} />
        <ReferenceLine y={5.0} stroke="#22c55e" strokeDasharray="4 4" strokeWidth={1} />
        <Line
          type="monotone"
          dataKey="score"
          stroke="#34d399"
          strokeWidth={2}
          dot={{ fill: "#34d399", r: 3 }}
          activeDot={{ r: 5 }}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

interface PcRatioChartProps {
  history: OptionsHistoryEntry[];
}

export function PcRatioChart({ history }: PcRatioChartProps) {
  const data = [...history]
    .sort((a, b) => a.scan_date.localeCompare(b.scan_date))
    .map((h) => ({ date: h.scan_date, pc: h.put_call_ratio }))
    .filter((h) => h.pc != null);

  if (data.length === 0) return null;

  return (
    <ResponsiveContainer width="100%" height={160}>
      <LineChart data={data} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          dataKey="date"
          tick={{ fill: "#6b7280", fontSize: 11, fontFamily: "monospace" }}
          tickLine={false}
          axisLine={false}
        />
        <YAxis
          tick={{ fill: "#6b7280", fontSize: 11, fontFamily: "monospace" }}
          tickLine={false}
          axisLine={false}
        />
        <Tooltip
          contentStyle={{
            background: "#111827",
            border: "1px solid #374151",
            borderRadius: 6,
            fontSize: 12,
            fontFamily: "monospace",
          }}
          labelStyle={{ color: "#9ca3af" }}
          itemStyle={{ color: "#f59e0b" }}
        />
        <ReferenceLine y={1.0} stroke="#6b7280" strokeDasharray="4 4" strokeWidth={1} />
        <Line
          type="monotone"
          dataKey="pc"
          stroke="#f59e0b"
          strokeWidth={2}
          dot={{ fill: "#f59e0b", r: 3 }}
          activeDot={{ r: 5 }}
          name="P/C Ratio"
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
