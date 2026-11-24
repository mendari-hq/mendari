// SPDX-License-Identifier: Apache-2.0
"use client";

import { useEffect, useRef, useState } from "react";

// A line to copy: a shell command (with its `$`), or any other line, such as Markdown (`prompt` false).
export function CopyCommand({ command, prompt = true }: { command: string; prompt?: boolean }) {
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  async function onCopy() {
    if (timerRef.current) clearTimeout(timerRef.current);
    try {
      await navigator.clipboard.writeText(command);
      setStatus("copied");
    } catch {
      setStatus("failed");
    }
    timerRef.current = setTimeout(() => setStatus("idle"), 1800);
  }

  return (
    <div className="copy-cmd">
      <div className="cmd-shell">
        {prompt ? <span className="cmd-prompt" aria-hidden="true">$</span> : null}
        <code className="pull">{command}</code>
      </div>
      <button type="button" onClick={onCopy} aria-live="polite">
        {status === "copied" ? "Copied" : status === "failed" ? "Copy failed" : "Copy"}
      </button>
    </div>
  );
}
