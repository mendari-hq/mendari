// SPDX-License-Identifier: Apache-2.0
"use client";

import { useState } from "react";

export function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);

  async function onCopy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="copy-cmd">
      <div className="cmd-shell">
        <span className="cmd-prompt" aria-hidden="true">$</span>
        <code className="pull">{command}</code>
      </div>
      <button type="button" onClick={onCopy} aria-live="polite">
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
