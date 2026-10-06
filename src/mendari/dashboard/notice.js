// The notice a signed-in person acknowledges before taking data in (mendari.notices): asked once
// per notice version, when the dashboard opens. The server refuses their decisions and reviews
// until they have, so nothing they could type in is usable before.

import { api, esc, toastError, whileBusy } from "./ui.js";

export async function acknowledgeNotice() {
  const notice = await api("/api/notice").catch(() => null);
  if (!notice?.required || notice.acknowledged) return;
  await new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "confirm acknowledge";
    dialog.setAttribute("aria-labelledby", "acknowledge-title");
    dialog.innerHTML = `<form method="dialog">
      <h2 id="acknowledge-title">Before you start</h2>
      <p><strong>${esc(notice.text)}</strong></p>
      <p>${esc(notice.acknowledgement)}</p>
      <div class="row mt-4"><span class="grow"></span><button type="button" class="primary" id="acknowledge">I understand: synthetic data only</button></div>
    </form>`;
    document.body.append(dialog);
    dialog.addEventListener("cancel", (event) => event.preventDefault()); // Escape does not skip it
    dialog.querySelector("#acknowledge").addEventListener("click", async (event) => {
      try {
        await whileBusy(event.currentTarget, "Saving…", () => api("/api/notice/acknowledge", { method: "POST" }));
        dialog.close();
        dialog.remove();
        resolve();
      } catch (error) {
        toastError(error.message);
      }
    });
    dialog.showModal();
  });
}
