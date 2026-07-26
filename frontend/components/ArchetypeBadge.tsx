const ARCHETYPES: Record<string, { label: string; className: string; desc: string }> = {
  "1": {
    label: "Uptrend",
    className: "bg-green-500/20 text-green-400 border-green-500/40",
    desc: "Uptrend: sustained price advance above key MAs with accelerating momentum",
  },
  "2": {
    label: "Recovery",
    className: "bg-amber-500/20 text-amber-400 border-amber-500/40",
    desc: "Recovery: bouncing off support with volume confirmation after pullback",
  },
  "3": {
    label: "Breakout",
    className: "bg-blue-500/20 text-blue-400 border-blue-500/40",
    desc: "Breakout: clearing resistance on elevated volume -- first move out of base",
  },
  "4": {
    label: "Rotation",
    className: "bg-purple-500/20 text-purple-400 border-purple-500/40",
    desc: "Rotation: fresh institutional buying rotating into this sector/name",
  },
};

const LEGEND_TOOLTIP =
  "1=Uptrend (sustained trend)\n2=Recovery (support bounce)\n3=Breakout (resistance clear)\n4=Rotation (sector entry)";

interface Props {
  archetype: string | null;
  showHelp?: boolean;
}

export default function ArchetypeBadge({ archetype, showHelp = false }: Props) {
  const info = archetype ? ARCHETYPES[archetype] : null;

  const badge = info ? (
    <span
      title={info.desc}
      className={`inline-block px-2 py-0.5 rounded text-xs border font-medium cursor-default ${info.className}`}
    >
      {info.label}
    </span>
  ) : (
    <span className="inline-block px-2 py-0.5 rounded text-xs border bg-gray-700/40 text-gray-500 border-gray-600/40">
      --
    </span>
  );

  if (!showHelp) return badge;

  return (
    <span className="inline-flex items-center gap-1">
      {badge}
      <span
        title={LEGEND_TOOLTIP}
        className="text-gray-600 hover:text-gray-400 cursor-default text-xs leading-none"
        aria-label="Archetype legend"
      >
        ?
      </span>
    </span>
  );
}
