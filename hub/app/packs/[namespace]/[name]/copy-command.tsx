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
      <code className="pull">{command}</code>
      <button type="button" onClick={onCopy} aria-live="polite">
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
