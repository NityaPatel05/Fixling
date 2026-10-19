import { api } from "../api";
import { usePoll } from "../hooks";
import { Badge } from "./Badge";

export function HealthBar() {
  const { data, error } = usePoll(() => api.health(), 10_000);
  if (error && !data) return <p className="error">The API is not reachable: {error}</p>;
  if (!data) return null;
  return (
    <ul className="health" aria-label="system health">
      {Object.entries(data).map(([name, item]) => (
        <li key={name} title={item.detail}>
          <Badge tone={item.ok ? "ok" : name === "github" ? "muted" : "bad"}>{name}</Badge>
          {item.ok ? null : <span className="muted"> {item.detail}</span>}
        </li>
      ))}
    </ul>
  );
}
