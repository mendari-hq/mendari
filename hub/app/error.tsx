"use client";

export default function ErrorPage({ reset }: { error: Error; reset: () => void }) {
  return (
    <div className="empty" role="alert">
      <h1>The Hub can&apos;t reach the registry</h1>
      <p>Try again in a moment.</p>
      <button type="button" onClick={reset}>Try again</button>
    </div>
  );
}
