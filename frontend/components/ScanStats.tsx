"use client";

import { useEffect, useState } from "react";
import { getHealth } from "@/lib/api";
import type { HealthResponse } from "@/lib/types";

interface Props {
  universeSize?: number;
  scored?: number;
  elapsed?: number | null;
  lastUpdated?: string | null;
}

function isAfter355ET(): boolean {
  const now = new Date();
  const et = new Date(
    now.toLocaleString("en-US", { timeZone: "America/New_York" })
  );
  return et.getHours() > 15 || (et.getHours() === 15 && et.getMinutes() >= 55);
}

export default function ScanStats({ universeSize, scored, elapsed, lastUpdated }: Props) {
  const [health, setHealth] = useState<HealthResponse | null>(null);

  useEffect(() => {
    getHealth().then(setHealth).catch(() => {});

    const id = setInterval(() => {
      if (isAfter355ET()) {
        getHealth().then(setHealth).catch(() => {});
      }
    }, 60_000);
    return () => clearInterval(id);
  }, []);

  const lastScan = lastUpdated ?? health?.last_scan ?? null;
  const size = universeSize ?? health?.universe_size ?? null;

  return (
    <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-gray-500 font-mono">
      {size != null && (
        <span>
          <span className="text-gray-400">universe</span> {size.toLocaleString()}
        </span>
      )}
      {scored != null && (
        <span>
          <span className="text-gray-400">scored</span> {scored.toLocaleString()}
        </span>
      )}
      {elapsed != null && (
        <span>
          <span className="text-gray-400">elapsed</span> {elapsed}s
        </span>
      )}
      {lastScan && (
        <span>
          <span className="text-gray-400">updated</span>{" "}
          {lastScan.replace("T", " ").slice(0, 19)}
        </span>
      )}
    </div>
  );
}
