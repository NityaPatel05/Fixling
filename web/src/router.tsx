import { useEffect, useState, type MouseEvent, type ReactNode } from "react";

// A 20-line router: three pages do not need a routing library (and each dependency is supply-chain surface).

export function usePath(): string {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const onPop = (): void => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return path;
}

export function navigate(to: string): void {
  window.history.pushState({}, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function Link({ to, children, className }: { to: string; children: ReactNode; className?: string }) {
  const onClick = (e: MouseEvent<HTMLAnchorElement>): void => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; // let the browser open new tabs
    e.preventDefault();
    navigate(to);
  };
  return (
    <a href={to} onClick={onClick} className={className}>
      {children}
    </a>
  );
}
