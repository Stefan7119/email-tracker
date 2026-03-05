(function () {
  "use strict";

  const TRACKER_URL_KEY = "tracker_base_url";
  let trackerBaseUrl = "";

  chrome.storage.sync.get([TRACKER_URL_KEY], (result) => {
    trackerBaseUrl = result[TRACKER_URL_KEY] || "";
    if (trackerBaseUrl) {
      console.log("[Email Tracker] Active — server:", trackerBaseUrl);
      initTracker();
    } else {
      console.log("[Email Tracker] No server URL configured. Open the extension popup to set it up.");
    }
  });

  chrome.storage.onChanged.addListener((changes) => {
    if (changes[TRACKER_URL_KEY]) {
      trackerBaseUrl = changes[TRACKER_URL_KEY].newValue || "";
      if (trackerBaseUrl) {
        console.log("[Email Tracker] Server URL updated:", trackerBaseUrl);
        initTracker();
      }
    }
  });

  function initTracker() {
    const observer = new MutationObserver(debounce(scanForSendButtons, 500));
    observer.observe(document.body, { childList: true, subtree: true });
    scanForSendButtons();
  }

  function debounce(fn, delay) {
    let timer;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), delay);
    };
  }

  const hookedButtons = new WeakSet();

  function scanForSendButtons() {
    const allButtons = document.querySelectorAll(
      'div[role="button"][data-tooltip*="Send"], div[role="button"][aria-label*="Send"]'
    );

    allButtons.forEach((btn) => {
      if (hookedButtons.has(btn)) return;

      const tooltip = btn.getAttribute("data-tooltip") || "";
      const ariaLabel = btn.getAttribute("aria-label") || "";
      const text = (tooltip + " " + ariaLabel).toLowerCase();

      if (text.includes("send") && !text.includes("archive") && !text.includes("schedule")) {
        hookedButtons.add(btn);
        hookSendButton(btn);
      }
    });
  }

  function hookSendButton(sendBtn) {
    sendBtn.addEventListener(
      "click",
      async (e) => {
        const composeWindow = findComposeWindow(sendBtn);
        if (!composeWindow) return;
        if (composeWindow.dataset.trackerProcessed === "true") return;

        e.stopImmediatePropagation();
        e.preventDefault();

        try {
          await injectTracking(composeWindow);
          composeWindow.dataset.trackerProcessed = "true";
          sendBtn.click();
        } catch (err) {
          console.error("[Email Tracker] Error:", err);
          composeWindow.dataset.trackerProcessed = "true";
          sendBtn.click();
        }
      },
      true
    );
    console.log("[Email Tracker] Hooked send button");
  }

  function findComposeWindow(sendBtn) {
    let el = sendBtn;
    for (let i = 0; i < 20; i++) {
      el = el.parentElement;
      if (!el) return null;
      if (
        el.querySelector('div[aria-label="Message Body"][contenteditable="true"]') ||
        el.querySelector('div[g_editable="true"]') ||
        el.querySelector("div.Am.Al.editable")
      ) {
        return el;
      }
    }
    return null;
  }

  function getEmailBody(composeWindow) {
    return (
      composeWindow.querySelector('div[aria-label="Message Body"][contenteditable="true"]') ||
      composeWindow.querySelector('div[g_editable="true"]') ||
      composeWindow.querySelector("div.Am.Al.editable") ||
      composeWindow.querySelector('div[contenteditable="true"]')
    );
  }

  function getRecipient(composeWindow) {
    const toField =
      composeWindow.querySelector('input[aria-label="To"]') ||
      composeWindow.querySelector('input[name="to"]') ||
      composeWindow.querySelector("span[email]");
    if (toField) return toField.getAttribute("email") || toField.value || toField.textContent || "unknown";
    const chips = composeWindow.querySelectorAll("div[data-hovercard-id]");
    if (chips.length > 0) return chips[0].getAttribute("data-hovercard-id") || "unknown";
    return "unknown";
  }

  function getSubject(composeWindow) {
    const subjectField =
      composeWindow.querySelector('input[aria-label="Subject"]') ||
      composeWindow.querySelector('input[name="subjectbox"]');
    return subjectField ? subjectField.value || "(no subject)" : "(no subject)";
  }

  async function injectTracking(composeWindow) {
    if (!trackerBaseUrl) return;

    const body = getEmailBody(composeWindow);
    if (!body) { console.warn("[Email Tracker] Could not find email body"); return; }

    const recipient = getRecipient(composeWindow);
    const subject = getSubject(composeWindow);

    let emailId;
    try {
      const res = await fetch(`${trackerBaseUrl}/api/track`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recipient, subject }),
      });
      const data = await res.json();
      emailId = data.email_id;
    } catch (err) {
      console.error("[Email Tracker] Could not register email:", err);
      return;
    }

    const links = body.querySelectorAll("a[href]");
    for (const link of links) {
      const href = link.getAttribute("href");
      if (!href || href.startsWith("mailto:") || href.startsWith("#") || href.includes(trackerBaseUrl)) continue;
      try {
        const res = await fetch(`${trackerBaseUrl}/api/link`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email_id: emailId, url: href, label: link.textContent.substring(0, 50) || href.substring(0, 50) }),
        });
        const data = await res.json();
        link.setAttribute("href", data.tracked_url);
      } catch (err) {
        console.error("[Email Tracker] Could not wrap link:", err);
      }
    }

    const pixelUrl = `${trackerBaseUrl}/p/${emailId}.gif`;
    const pixel = document.createElement("img");
    pixel.src = pixelUrl;
    pixel.width = 1;
    pixel.height = 1;
    pixel.style.display = "none";
    pixel.alt = "";
    body.appendChild(pixel);

    console.log(`[Email Tracker] Injected: "${subject}" → ${recipient} (${emailId})`);
  }
})();
