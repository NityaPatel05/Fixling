const MAX_LINES = 1500;

function lineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "d-file";
  if (line.startsWith("diff ") || line.startsWith("index ") || line.startsWith("new file") || line.startsWith("deleted file")) return "d-meta";
  if (line.startsWith("@@")) return "d-hunk";
  if (line.startsWith("+")) return "d-add";
  if (line.startsWith("-")) return "d-del";
  return "d-ctx";
}

/** A plain unified-diff renderer. Every line is a text node: nothing from a patch is ever interpreted as HTML. */
export function DiffView({ diff }: { diff: string }) {
  const lines = diff.split("\n");
  const shown = lines.slice(0, MAX_LINES);
  return (
    <pre className="diff" aria-label="diff">
      {shown.map((line, i) => (
        <span key={i} className={`dline ${lineClass(line)}`}>
          {line}
          {"\n"}
        </span>
      ))}
      {lines.length > shown.length ? <span className="d-meta">... {lines.length - shown.length} more lines</span> : null}
    </pre>
  );
}
