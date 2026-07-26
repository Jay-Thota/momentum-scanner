"use client";

interface Props {
  regime: string | null;
  spxPrice?: number | null;
  spx200ma?: number | null;
}

export default function RegimeBadge({ regime, spxPrice, spx200ma }: Props) {
  const isBull = regime?.toUpperCase() === "BULL";
  const label = regime?.toUpperCase() ?? "UNKNOWN";

  const tooltip =
    spxPrice != null && spx200ma != null
      ? `${label} -- SPX ${spxPrice.toLocaleString()} vs 200MA ${spx200ma.toLocaleString()}`
      : label;

  return (
    <span
      title={tooltip}
      className={`inline-block px-3 py-1 rounded-full text-xs font-bold uppercase tracking-wider cursor-default select-none ${
        isBull
          ? "bg-green-500/20 text-green-400 border border-green-500/40"
          : "bg-red-500/20 text-red-400 border border-red-500/40"
      }`}
    >
      {label}
    </span>
  );
}
