// SPDX-License-Identifier: Apache-2.0
// The drawer: one dialog at a time beside the page (full screen on phones).

import { esc, icon, reducedMotion } from "./ui.js";

// One dialog at a time, beside the page (full screen on phones). The page behind is inert, Tab stays
// inside, Escape or the close button closes it, and focus returns to what opened it.
let drawer = null;

// The drawer covers the page, and the notice under the top bar with it: it says it again.
const noticeLine = () => {
  const text = document.querySelector("body > .preview-notice")?.textContent;
  return text ? `<p class="preview-notice drawer-notice" role="note">${esc(text)}</p>` : "";
};

const INERT_SELECTORS = ".topbar, .shell, .session-ended";

export function showDrawer({ title, body, opener, onClose, headActions = "", onKeydown = null, busy = false }) {
  closeDrawer({ restoreFocus: false, closedByUser: false });
  const root = document.createElement("div");
  root.className = "drawer-root";
  root.innerHTML = `<div class="drawer-backdrop" data-close></div>
    <section class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
      <header class="drawer-head"><h2 id="drawer-title" tabindex="-1">${esc(title)}</h2>
        ${headActions ? `<div class="drawer-actions">${headActions}</div>` : ""}
        <button type="button" class="icon-button" data-close aria-label="Close">${icon("x")}</button></header>
      <div class="drawer-body"${busy ? ' aria-busy="true"' : ""}>${noticeLine()}${body}</div>
    </section>`;
  document.body.append(root);
  document.querySelectorAll(INERT_SELECTORS).forEach((el) => (el.inert = true));
  document.body.classList.add("drawer-open");
  drawer = { root, opener, onClose, onKeydown };
  root.addEventListener("click", (event) => {
    if (event.target.closest("[data-close]")) closeDrawer();
  });
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeDrawer();
    } else if (event.key === "Tab") {
      keepFocusInside(event, root.querySelector(".drawer"));
    } else if (drawer?.onKeydown) {
      drawer.onKeydown(event);
    }
  });
  root.querySelector("#drawer-title").focus();
  return root;
}

export function updateDrawer({ title, body, headActions = "", onKeydown = null, busy = false }) {
  if (!drawer) return null;
  const { root } = drawer;
  const titleEl = root.querySelector("#drawer-title");
  if (titleEl && title !== undefined) titleEl.textContent = title;
  const head = root.querySelector(".drawer-head");
  let actions = head?.querySelector(".drawer-actions");
  if (headActions) {
    if (!actions) {
      actions = document.createElement("div");
      actions.className = "drawer-actions";
      head?.querySelector("[data-close]")?.before(actions);
    }
    actions.innerHTML = headActions;
  } else if (actions) {
    actions.remove();
  }
  const bodyEl = root.querySelector(".drawer-body");
  if (bodyEl) {
    if (busy) bodyEl.setAttribute("aria-busy", "true");
    else bodyEl.removeAttribute("aria-busy");
    bodyEl.innerHTML = `${noticeLine()}${body}`;
  }
  drawer.onKeydown = onKeydown;
  return root;
}

// `closedByUser`: a person closed it (button, Escape, backdrop), so its onClose runs (e.g. the URL
// goes back to the list). A drawer replaced by another, or dropped by a view change, closes quietly:
// otherwise the old drawer's onClose would navigate away from the new one.
export function closeDrawer({ restoreFocus = true, closedByUser = true } = {}) {
  if (!drawer) return;
  const { root, opener, onClose } = drawer;
  drawer = null;
  document.querySelectorAll(INERT_SELECTORS).forEach((el) => (el.inert = false));
  document.body.classList.remove("drawer-open");
  if (restoreFocus && opener?.isConnected) opener.focus();
  leave(root, { animate: closedByUser });
  if (closedByUser) onClose?.();
}

export const drawerOpen = () => Boolean(drawer);

// Closed by a person, it slides back out (the page is already usable again: the drawer is inert and
// no longer a dialog while it goes). Replaced or dropped by a view change, it goes at once.
function leave(root, { animate }) {
  if (!animate || reducedMotion()) return root.remove();
  root.inert = true;
  const panel = root.querySelector(".drawer");
  panel.removeAttribute("role");
  panel.removeAttribute("aria-modal");
  root.querySelector("#drawer-title")?.removeAttribute("id");
  root.classList.add("closing");
  panel.addEventListener("animationend", () => root.remove(), { once: true });
  setTimeout(() => root.remove(), 400); // in case no animation runs
}

function keepFocusInside(event, container) {
  const focusable = [
    ...container.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])',
    ),
  ].filter((el) => el.getClientRects().length);
  if (!focusable.length) return;
  const [first, last] = [focusable[0], focusable[focusable.length - 1]];
  const inside = container.contains(document.activeElement) && document.activeElement.id !== "drawer-title";
  if (event.shiftKey && (!inside || document.activeElement === first)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}
