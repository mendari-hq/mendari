// The notice a signed-in person acknowledges before taking data in (mendari.notices): asked once
// per notice version, when the dashboard opens, and again if the server refuses for want of it
// (a new version mid-session: `api` in ui.js). The server refuses their decisions and reviews
// until they have. The dialog says its own errors (nothing under a modal is heard), and signing
// out is the way out.

import { api, esc, whileBusy } from "./ui.js";

let asking = null; // one dialog at a time, whoever asks

export function acknowledgeNotice() {
  asking ??= ask().finally(() => { asking = null; });
  return asking;
}

async function ask() {
  const notice = await api("/api/notice").catch(() => null);
  if (!notice?.required || notice.acknowledged) return;
  await new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "confirm acknowledge";
    dialog.setAttribute("aria-labelledby", "acknowledge-title");
    dialog.setAttribute("aria-describedby", "acknowledge-notice acknowledge-text");
    dialog.innerHTML = `<form method="dialog">
      <h2 id="acknowledge-title">Before you start</h2>
      <p id="acknowledge-notice"><strong>${esc(notice.text)}</strong></p>
      <p id="acknowledge-text">${esc(notice.acknowledgement)}</p>
      <p class="error" id="acknowledge-error" role="alert" hidden></p>
      <div class="row mt-4"><span class="grow"></span>
        <button type="button" id="acknowledge-sign-out">Sign out</button>
        <button type="button" class="primary" id="acknowledge">I understand: synthetic data only</button>
      </div>
    </form>`;
    document.body.append(dialog);
    const error = dialog.querySelector("#acknowledge-error");
    dialog.addEventListener("cancel", (event) => event.preventDefault()); // Escape does not skip it
    dialog.querySelector("#acknowledge-sign-out").addEventListener("click", async () => {
      await api("/auth/logout", { method: "POST" }).catch(() => null);
      location.assign("/auth/login");
    });
    dialog.querySelector("#acknowledge").addEventListener("click", async (event) => {
      try {
        await whileBusy(event.currentTarget, "Saving…", () => api("/api/notice/acknowledge", { method: "POST" }));
        dialog.close();
        dialog.remove();
        resolve();
      } catch (failure) {
        error.textContent = `${failure.message} Try again, or sign out.`;
        error.hidden = false;
      }
    });
    dialog.showModal();
  });
}
