// SPDX-License-Identifier: Apache-2.0
// Forms generated from a pack's input schema (the JSON Schema subset packs use), so Try it edits a
// record in fields and tables instead of raw JSON, and every pack gets a form without UI code of its
// own. The record lives in memory (`model`); each field writes to its path in it, so the form and
// the JSON view are two views of the same record.
//
//   let form = schemaForm(schema, record, "try")      // { html, model, render, value }
//   container.innerHTML = form.html
//   wireSchemaForm(container, () => form)             // once; follows `form` when it is replaced
//   form.value()                                      // the record as the fields hold it
//
// objects -> fieldsets; arrays of objects -> editable tables (add / remove rows); arrays of text ->
// lists of inputs; enums -> selects; `format: date` -> date inputs; long text (maxLength >= 1000)
// -> text areas; numbers -> number inputs with the schema's bounds. Anything else is edited as JSON
// in place, so no schema can make the form unusable.

import { esc, icon } from "./ui.js";

const LONG_TEXT = 1000;

const humanize = (key) => {
  const words = String(key).replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
};
const clone = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

// "#/$defs/items" -> the definition, keeping the referring schema's own description.
function resolveSchema(schema, root) {
  if (!schema?.$ref) return schema ?? {};
  const target = schema.$ref.replace(/^#\//, "").split("/").reduce((node, key) => node?.[key], root) ?? {};
  return { ...target, description: schema.description ?? target.description };
}

function getAt(value, path) {
  return path.reduce((node, key) => (node === undefined || node === null ? undefined : node[key]), value);
}

function setAt(model, path, value) {
  let node = model;
  path.slice(0, -1).forEach((key, i) => {
    if (node[key] === undefined || node[key] === null) node[key] = typeof path[i + 1] === "number" ? [] : {};
    node = node[key];
  });
  const last = path[path.length - 1];
  if (value === undefined && !Array.isArray(node)) delete node[last];
  else node[last] = value;
}

function schemaAt(schema, path, root) {
  let node = resolveSchema(schema, root);
  for (const key of path) {
    node = resolveSchema(typeof key === "number" ? node.items : node.properties?.[key], root);
  }
  return node;
}

// The kind of editor a schema gets.
function kindOf(schema) {
  const type = Array.isArray(schema.type) ? schema.type.find((t) => t !== "null") : schema.type;
  if (schema.enum) return "enum";
  if (type === "object" && schema.properties) return "object";
  if (type === "array") {
    const items = schema.items ?? {};
    if (items.type === "object" && items.properties) return "table";
    if (["string", "number", "integer"].includes(items.type) && !items.enum) return "list";
    return "json";
  }
  if (type === "string") return schema.format === "date" ? "date" : (schema.maxLength ?? 0) >= LONG_TEXT ? "text" : "string";
  if (type === "integer" || type === "number") return type;
  if (type === "boolean") return "boolean";
  return "json";
}

export function schemaForm(schema, record, prefix) {
  const root = schema;
  const model = isObject(clone(record)) ? clone(record) : {};
  const ids = { n: 0 };
  const id = () => `${prefix}-f${++ids.n}`;
  const pathAttr = (path) => esc(JSON.stringify(path));

  // One scalar input. `label` is visible text, or an aria-label inside tables and lists.
  function input(node, path, { label, labelled = true, required = false }) {
    const kind = kindOf(node);
    const value = getAt(model, path);
    const field = id();
    const hint = node.description ? `${field}-hint` : "";
    const common = `id="${field}" data-path="${pathAttr(path)}" data-kind="${kind}"${required ? " required" : ""}${hint && labelled ? ` aria-describedby="${hint}"` : ""}${labelled ? "" : ` aria-label="${esc(label)}"`}`;
    let control;
    if (kind === "enum") {
      // Always a blank choice: a required select then blocks Decide until one is picked, instead of
      // showing a first option the record doesn't hold. A numeric enum reads back as a number.
      const type = Array.isArray(node.type) ? node.type.find((t) => t !== "null") : node.type;
      const numeric = type === "integer" || type === "number" ? ' data-numeric=""' : "";
      control = `<select ${common}${numeric}><option value=""></option>${node.enum.map((option) =>
        `<option ${option === value ? "selected" : ""}>${esc(option)}</option>`).join("")}</select>`;
    } else if (kind === "text") {
      control = `<textarea ${common} rows="3" maxlength="${node.maxLength}">${esc(value ?? "")}</textarea>`;
    } else if (kind === "boolean") {
      control = `<input type="checkbox" ${common} ${value ? "checked" : ""}>`;
    } else if (kind === "integer" || kind === "number") {
      const bounds = `${node.minimum !== undefined ? ` min="${node.minimum}"` : ""}${node.maximum !== undefined ? ` max="${node.maximum}"` : ""}`;
      control = `<input type="number" step="${kind === "integer" ? 1 : "any"}"${bounds} ${common} value="${esc(value ?? "")}">`;
    } else if (kind === "date") {
      control = `<input type="date" ${common} value="${esc(value ?? "")}">`;
    } else if (kind === "json") {
      control = `<textarea class="code sf-json" ${common} rows="3" spellcheck="false">${esc(value === undefined ? "" : JSON.stringify(value, null, 2))}</textarea>`;
    } else {
      control = `<input type="text" ${common} value="${esc(value ?? "")}"${node.maxLength ? ` maxlength="${node.maxLength}"` : ""}>`;
    }
    if (!labelled) return control;
    return `<div class="sf-field${kind === "text" || kind === "json" ? " wide" : ""}">
      <label for="${field}">${esc(label)}${required ? ` <span class="sf-required" aria-hidden="true">*</span><span class="sr-only">(required)</span>` : ""}</label>
      ${control}${hint ? `<span class="sf-hint" id="${hint}">${esc(node.description)}</span>` : ""}</div>`;
  }

  // Rows of an array of records: one column per property, a remove button per row.
  function table(node, path, label) {
    const items = resolveSchema(node.items, root);
    const columns = Object.entries(items.properties).map(([key, column]) => [key, resolveSchema(column, root)]);
    const rows = getAt(model, path) ?? [];
    const noun = label.toLowerCase();
    return `<div class="sf-group" data-array="${pathAttr(path)}">
      <div class="sf-group-head"><span class="sf-legend" id="${esc(prefix)}-${esc(path.join("-"))}">${esc(label)}</span>
        ${node.description ? `<span class="sf-hint">${esc(node.description)}</span>` : ""}</div>
      ${rows.length ? `<div class="table-scroll"><table class="data sf-table" aria-labelledby="${esc(prefix)}-${esc(path.join("-"))}">
        <thead><tr>${columns.map(([key, column]) => `<th scope="col"${column.description ? ` title="${esc(column.description)}"` : ""}>${esc(humanize(key))}${(items.required ?? []).includes(key) ? " *" : ""}</th>`).join("")}<th scope="col"><span class="sr-only">Remove</span></th></tr></thead>
        <tbody>${rows.map((_, index) => `<tr>${columns.map(([key, column]) => `<td>${input(column, [...path, index, key], {
          label: `${label} ${index + 1}: ${humanize(key)}`, labelled: false, required: (items.required ?? []).includes(key),
        })}</td>`).join("")}<td><button type="button" class="icon-button" data-remove="${index}" aria-label="Remove ${esc(noun)} ${index + 1}">${icon("x")}</button></td></tr>`).join("")}</tbody>
      </table></div>` : ""}
      ${addRow(rows.length, noun)}
    </div>`;
  }

  // "Add to …", beside "None." while the list is empty.
  const addRow = (count, noun) => `<div class="sf-add-row">${count ? "" : `<span class="muted">None.</span>`}
    <button type="button" class="sf-add" data-add>Add to ${esc(noun)}</button></div>`;

  // An array of short text items: one input per item.
  function list(node, path, label) {
    const items = resolveSchema(node.items, root);
    const values = getAt(model, path) ?? [];
    const noun = label.toLowerCase();
    return `<div class="sf-group" data-array="${pathAttr(path)}">
      <div class="sf-group-head"><span class="sf-legend">${esc(label)}</span>${node.description ? `<span class="sf-hint">${esc(node.description)}</span>` : ""}</div>
      ${values.length ? `<ul class="sf-list">${values.map((_, index) => `<li>${input(items, [...path, index], { label: `${label} ${index + 1}`, labelled: false })}
        <button type="button" class="icon-button" data-remove="${index}" aria-label="Remove ${esc(noun)} ${index + 1}">${icon("x")}</button></li>`).join("")}</ul>` : ""}
      ${addRow(values.length, noun)}
    </div>`;
  }

  function fields(node, path, required = []) {
    return Object.entries(node.properties).map(([key, child]) => {
      const resolved = resolveSchema(child, root);
      const label = resolved.title ?? humanize(key);
      const kind = kindOf(resolved);
      if (kind === "object") {
        return `<fieldset class="sf-fieldset"><legend>${esc(label)}</legend>
          ${resolved.description ? `<p class="sf-hint">${esc(resolved.description)}</p>` : ""}
          <div class="sf-grid">${fields(resolved, [...path, key], resolved.required ?? [])}</div></fieldset>`;
      }
      if (kind === "table") return table(resolved, [...path, key], label);
      if (kind === "list") return list(resolved, [...path, key], label);
      return input(resolved, [...path, key], { label, required: required.includes(key) });
    }).join("");
  }

  const top = resolveSchema(schema, root);
  const render = () => `<div class="sf-grid">${fields(top, [], top.required ?? [])}</div>`;
  return {
    root, model, render, html: render(),
    // The record as the fields hold it: rows or items left empty are dropped.
    value: () => prune(clone(model)),
  };
}

function prune(value) {
  if (Array.isArray(value)) {
    return value.map(prune).filter((item) => item !== undefined && item !== "" && !(isObject(item) && !Object.keys(item).length));
  }
  if (isObject(value)) {
    for (const [key, child] of Object.entries(value)) {
      const kept = prune(child);
      if (kept === undefined || (isObject(kept) && !Object.keys(kept).length)) delete value[key];
      else value[key] = kept;
    }
  }
  return value;
}

// Field edits write into the model; add / remove redraw the form with focus kept in place.
export function wireSchemaForm(container, current) {
  container.addEventListener("input", (event) => {
    const el = event.target.closest("[data-path]");
    if (!el) return;
    const form = current();
    const path = JSON.parse(el.dataset.path);
    const kind = el.dataset.kind;
    if (kind === "json") {
      try {
        setAt(form.model, path, el.value.trim() ? JSON.parse(el.value) : undefined);
        el.removeAttribute("aria-invalid");
        el.setCustomValidity("");
      } catch (error) {
        el.setAttribute("aria-invalid", "true");
        el.setCustomValidity(`Not valid JSON: ${error.message}`);
      }
      return;
    }
    let value;
    if (kind === "boolean") value = el.checked;
    else if (el.value === "") value = undefined;
    else if (kind === "integer" || kind === "number") value = Number(el.value);
    else if (kind === "enum" && el.dataset.numeric !== undefined) value = Number(el.value);
    else value = el.value;
    // An emptied list item stays in place as "" (dropped on read), so item numbers don't shift; an
    // emptied field of an object (a table cell included) is removed.
    setAt(form.model, path, value === undefined && typeof path[path.length - 1] === "number" ? "" : value);
  });
  container.addEventListener("click", (event) => {
    const button = event.target.closest("[data-add], [data-remove]");
    if (!button) return;
    const form = current();
    const group = button.closest("[data-array]");
    const path = JSON.parse(group.dataset.array);
    const items = getAt(form.model, path) ?? [];
    let focusIndex;
    if (button.hasAttribute("data-add")) {
      const node = resolveSchema(schemaAt(form.root, path, form.root).items, form.root);
      items.push(node.type === "object" ? {} : "");
      focusIndex = items.length - 1;
    } else {
      items.splice(Number(button.dataset.remove), 1);
      focusIndex = Math.min(Number(button.dataset.remove), items.length - 1);
    }
    setAt(form.model, path, items);
    container.innerHTML = form.render();
    // Focus follows the change: the new row's first field, or the row that took the removed one's place.
    const regroup = container.querySelector(`[data-array="${CSS.escape(JSON.stringify(path))}"]`);
    const target = focusIndex >= 0
      ? regroup?.querySelector(`[data-path^="${CSS.escape(JSON.stringify([...path, focusIndex]).slice(0, -1))}"]`)
      : regroup?.querySelector("[data-add]");
    (target ?? regroup?.querySelector("[data-add]"))?.focus();
  });
}
