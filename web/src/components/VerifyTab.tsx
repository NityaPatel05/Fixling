import type { FixEvent } from "../api";
import { arr, bool, num, str, strs } from "../format";
import { Badge } from "./Badge";

function yesNo(v: boolean | null): JSX.Element {
  if (v === null) return <Badge tone="muted">n/a</Badge>;
  return v ? <Badge tone="ok">yes</Badge> : <Badge tone="bad">no</Badge>;
}

/** Baseline versus after-fix results from the clean-container verification. */
export function VerifyTab({ latest }: { latest: Record<string, FixEvent> }) {
  const env = latest.env?.payload;
  const verify = latest.verify?.payload;
  const repro = latest.repro?.payload;
  const strength = latest.strength?.payload;
  if (!verify) return <p className="muted">Clean verification has not run (yet).</p>;

  const findings = arr(verify.findings).map((f) => f as Record<string, unknown>);
  const regressions = strs(verify.regressions);
  const skips = strs(verify.new_skips);
  return (
    <div className="verify">
      <table className="grid">
        <thead>
          <tr>
            <th />
            <th>Before (baseline)</th>
            <th>After (clean container)</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">passed</th>
            <td>{env ? str(env.passed) : "-"}</td>
            <td>{str(verify.passed)}</td>
          </tr>
          <tr>
            <th scope="row">failed</th>
            <td>{env ? str(env.failed) : "-"}</td>
            <td>{str(verify.failed)}</td>
          </tr>
          <tr>
            <th scope="row">skipped</th>
            <td>{env ? str(env.skipped) : "-"}</td>
            <td>{str(verify.skipped)}</td>
          </tr>
          <tr>
            <th scope="row">flaky (excluded)</th>
            <td>{env ? strs(env.flaky).length : "-"}</td>
            <td>-</td>
          </tr>
        </tbody>
      </table>

      <dl className="facts">
        <dt>reproduction test now passes</dt>
        <dd>{yesNo(bool(verify.f2p_ok))}</dd>
        <dt>no regressions</dt>
        <dd>{yesNo(bool(verify.p2p_ok))}</dd>
        <dt>passes in shuffled order</dt>
        <dd>{yesNo(bool(verify.shuffled_ok))}</dd>
        <dt>reproduction before the fix</dt>
        <dd>{repro ? yesNo(bool(repro.ok)) : <Badge tone="muted">n/a</Badge>}</dd>
        {strength ? (
          <>
            <dt>independent tests</dt>
            <dd>{strength.status ? <Badge tone={strength.status === "pass" ? "ok" : "bad"}>{str(strength.status)}</Badge> : <Badge tone="muted">n/a</Badge>}</dd>
          </>
        ) : null}
      </dl>

      {regressions.length > 0 ? (
        <section>
          <h4>Regressions</h4>
          <ul>
            {regressions.map((t) => (
              <li key={t}>
                <code>{t}</code>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {skips.length > 0 ? (
        <section>
          <h4>New skipped tests</h4>
          <ul>
            {skips.map((t) => (
              <li key={t}>
                <code>{t}</code>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <section>
        <h4>Integrity findings</h4>
        {findings.length === 0 ? (
          <p className="muted">None.</p>
        ) : (
          <ul className="findings">
            {findings.map((f, i) => (
              <li key={i}>
                <Badge tone={f.severity === "block" ? "bad" : "warn"}>{str(f.severity)}</Badge> {str(f.rule)}
                <span className="muted"> - {str(f.detail)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <p className="muted">Collected {num(verify.collected) ?? "-"} tests in the clean run.</p>
    </div>
  );
}
