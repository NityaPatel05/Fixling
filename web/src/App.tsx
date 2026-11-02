import { EvalsPage } from "./pages/EvalsPage";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";
import { Link, usePath } from "./router";

export function App() {
  const path = usePath();
  const run = /^\/runs\/([A-Za-z0-9_-]+)\/?$/.exec(path);
  const runId = run?.[1];

  let page = <p className="muted">Page not found.</p>;
  if (path === "/" || path === "") page = <RunsPage />;
  else if (runId) page = <RunPage key={runId} runId={runId} />;
  else if (path === "/evals") page = <EvalsPage />;

  return (
    <>
      <header className="topbar">
        <Link to="/" className="brand">
          Fixling
        </Link>
        <nav aria-label="main">
          <Link to="/">Runs</Link>
          <Link to="/evals">Evals</Link>
        </nav>
      </header>
      <div className="page">{page}</div>
    </>
  );
}
