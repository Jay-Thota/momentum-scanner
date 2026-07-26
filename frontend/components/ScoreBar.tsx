"use client";

interface Props {
  l1: number | null;
  l2: number | null;
  l3: number | null;
  l4: number | null;
  l5: number | null;
}

const MAX_TOTAL = 6.5;

const LAYERS = [
  { key: "l1" as const, label: "L1 Volume",    maxPts: 1.0, color: "bg-cyan-500"    },
  { key: "l2" as const, label: "L2 Structure",  maxPts: 2.0, color: "bg-blue-500"   },
  { key: "l3" as const, label: "L3 Options",    maxPts: 1.0, color: "bg-violet-500"  },
  { key: "l4" as const, label: "L4 Short Int.", maxPts: 1.0, color: "bg-amber-500"  },
  { key: "l5" as const, label: "L5 Sector",     maxPts: 1.5, color: "bg-green-500"  },
];

export default function ScoreBar({ l1, l2, l3, l4, l5 }: Props) {
  const vals: Record<string, number | null> = { l1, l2, l3, l4, l5 };
  const total = (l1 ?? 0) + (l2 ?? 0) + (l3 ?? 0) + (l4 ?? 0) + (l5 ?? 0);

  return (
    <div className="flex items-center gap-1.5">
      {/* Bar -- bordered container anchors the 6.5 max visually */}
      <div className="flex h-3.5 flex-1 rounded overflow-hidden gap-px ring-1 ring-gray-600/50">
        {LAYERS.map(({ key, label, maxPts, color }) => {
          const val = vals[key] ?? 0;
          return (
            <div
              key={key}
              className="relative group"
              style={{ width: `${(maxPts / MAX_TOTAL) * 100}%` }}
            >
              {/* Gray track = this layer's max */}
              <div className="w-full h-full bg-gray-700/70" />
              {/* Colored fill */}
              <div
                className={`absolute inset-y-0 left-0 ${color} transition-all`}
                style={{ width: `${(val / maxPts) * 100}%` }}
              />
              {/* Tooltip */}
              <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 hidden group-hover:block z-10 pointer-events-none">
                <div className="bg-gray-900 border border-gray-700 text-xs text-white px-2 py-1 rounded whitespace-nowrap shadow-lg">
                  {label}: {val.toFixed(2)} / {maxPts.toFixed(1)}
                </div>
              </div>
            </div>
          );
        })}
      </div>
      {/* Max reference label */}
      <span className="text-gray-600 text-xs font-mono whitespace-nowrap tabular-nums">
        {total.toFixed(2)}<span className="text-gray-700">/6.5</span>
      </span>
    </div>
  );
}
