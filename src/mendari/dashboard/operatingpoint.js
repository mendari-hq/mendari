// SPDX-License-Identifier: Apache-2.0
// An evaluation's operating point (mendari.operating-point/1), as its run worked it out: what the
// agent's automation threshold lets through, each risk with its exact bound, and the threshold
// the cases support when the run was given targets. The sentences are the engine's, shown as
// they are (the command line prints the same ones); a report without one shows nothing.

import { esc } from "./ui.js";

// A sentence as HTML: escaped, with what it writes between backticks as code.
const sentence = (text) => esc(text).replace(/`([^`]+)`/g, "<code>$1</code>");

export function operatingPointView(report) {
  const point = (report.run ?? report).operating_point;
  if (typeof point?.summary !== "string") return "";
  const lines = Array.isArray(point.lines) ? point.lines.filter((line) => typeof line === "string") : [];
  return `<div class="panel" data-operating-point="${esc(point.status ?? "")}"><h2>Operating point</h2>
    <p><b>${sentence(point.summary)}</b></p>
    ${lines.map((line) => `<div class="finding">${sentence(line)}</div>`).join("")}
  </div>`;
}
