import type { ReactNode } from "react";
import type { ApplicationStatus } from "../types";

const LABELS: Record<ApplicationStatus, string> = {
  applied: "Applied", interviewing: "Interviewing", offer: "Offer", rejected: "Rejected", withdrawn: "Withdrawn",
};
export const statusLabel = (s: ApplicationStatus) => LABELS[s];

export function StatusBadge({ status }: { status: ApplicationStatus }) {
  return <span className={`badge badge-${status}`}>{LABELS[status]}</span>;
}

export function Loading({ what }: { what: string }) {
  return <p className="muted" role="status">Loading {what}…</p>;
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error" role="alert">
      <span>{message}</span>
      {onRetry && <button type="button" className="btn btn-small" onClick={onRetry}>Retry</button>}
    </div>
  );
}

/** Empty state: a short message plus an optional next step (link or button). */
export function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <p>{children}</p>
      {action}
    </div>
  );
}

/** Returns the URL only if it is a well-formed http(s) URL; anything else (javascript:, data:, junk) is null. */
export function safeExternalUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? url : null;
  } catch {
    return null;
  }
}

/** External link in a new tab; noopener/noreferrer stop the new tab from reaching back into this page. */
export function ExternalLink({ href, className, children }: { href: string; className?: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className={className}>
      {children}<span className="sr-only"> (opens in new tab)</span>
    </a>
  );
}

/** External job-posting link. */
export function PostingLink({ url }: { url: string | null }) {
  const safe = safeExternalUrl(url);
  if (!safe) return <span className="muted">No link</span>;
  return <ExternalLink href={safe}>Open posting</ExternalLink>;
}

export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
