interface Props {
  label: string;
  value: string | number;
  sub?: string;
  accent?: boolean;
}

export default function StatCard({ label, value, sub, accent }: Props) {
  return (
    <div className={`stat-card ${accent ? "border-l-4 border-l-primary-600" : ""}`}>
      <p className="stat-label">{label}</p>
      <p className={`stat-value ${accent ? "text-primary-700" : ""}`}>{value}</p>
      {sub && <p className="text-xs text-gray-400">{sub}</p>}
    </div>
  );
}
