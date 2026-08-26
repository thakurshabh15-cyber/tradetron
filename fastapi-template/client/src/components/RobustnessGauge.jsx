/**
 * TradeThrone Robustness Gauge — circular 0-100 score dial (AI Strategy Doctor).
 */
const _colorFor = (score) =>
  score >= 80 ? "#34d399" : score >= 65 ? "#22d3ee" : score >= 50 ? "#fbbf24" : score >= 35 ? "#fb923c" : "#f43f5e";

export default function RobustnessGauge({ score = 0, grade = "", size = 168 }) {
  const clamped = Math.max(0, Math.min(100, Number(score) || 0));
  const color = _colorFor(clamped);
  const r = 54;
  const c = 2 * Math.PI * r;
  const filled = (clamped / 100) * c;

  return (
    <div className="flex flex-col items-center gap-1 select-none">
      <svg width={size} height={size} viewBox="0 0 140 140" role="img" aria-label={`Robustness score ${clamped} of 100`}>
        <circle cx="70" cy="70" r={r} fill="none" stroke="rgba(148,163,184,0.15)" strokeWidth="11" />
        <circle
          cx="70" cy="70" r={r} fill="none"
          stroke={color}
          strokeWidth="11"
          strokeLinecap="round"
          strokeDasharray={`${filled} ${c - filled}`}
          transform="rotate(-90 70 70)"
          style={{ filter: `drop-shadow(0 0 6px ${color}66)`, transition: "stroke-dasharray .6s ease" }}
        />
        <text x="70" y="66" textAnchor="middle" fontSize="30" fontWeight="800" fill="#fff" fontFamily="monospace">
          {Math.round(clamped)}
        </text>
        <text x="70" y="86" textAnchor="middle" fontSize="11" fill="#94a3b8" letterSpacing="1">
          ROBUSTNESS
        </text>
        {grade && (
          <text x="70" y="106" textAnchor="middle" fontSize="15" fontWeight="700" fill={color}>
            GRADE {grade}
          </text>
        )}
      </svg>
    </div>
  );
}