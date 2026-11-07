"""
qscript - browser automation scenarios recorded to video.

Public API
----------
PATH_TO_VIDEO_FOLDER      folder where finished videos are stored (main script may override it)
HEADLESS                  run Chromium without a window (main script may override it)
CaptureVideo_Magento(...) run the sample scenario for one CSV row, return the .webm path or None
ShowCallout(...)          show a yellow text callout on the page (blocks until it disappears)

How the visuals work
--------------------
Playwright's screencast never contains the OS cursor, so a small overlay (SVG arrow,
yellow halo, click ripple, callout) is injected into every page with
``context.add_init_script``. The overlay does not have its own animation logic for the
cursor: it simply listens to the *real* ``mousemove`` / ``mousedown`` events that
Playwright's ``page.mouse`` produces. That keeps one source of truth for the pointer
position - what you see in the video is exactly where the browser thinks the mouse is.
"""

import math
import os
import random
import re
import sys
import time
import traceback
from typing import Optional, Tuple

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------------------
# Configuration (module globals, read at call time so the main script can override them)
# --------------------------------------------------------------------------------------

PATH_TO_VIDEO_FOLDER = "./video"

# DuckDuckGo answers searches from headless Chromium ("HeadlessChrome" user agent) with
# "Unexpected error", so the sample scenario needs a real (headed) browser window.
HEADLESS = False

VIDEO_SIZE = {"width": 1280, "height": 720}

SEARCH_BOX = 'textarea[name="q"]'
SEARCH_BUTTON = 'form button[type="submit"]'
FIRST_RESULT = 'a[data-testid="result-title-a"]'

PAGE_LOAD_TIMEOUT_MS = 30_000
RESULTS_TIMEOUT_MS = 45_000  # results are rendered client-side and can be slow
DESTINATION_TIMEOUT_MS = 8_000  # keep dead time short if the target site is slow

CALLOUT_BG = "#FFE600"
CALLOUT_FG = "#000000"


# --------------------------------------------------------------------------------------
# Overlay injected into every document (top frame only)
# --------------------------------------------------------------------------------------

# add_init_script evaluates the string as-is, so this is an IIFE, not a function body.
_OVERLAY_JS = r"""
(() => {
  if (window.top !== window || window.__qOverlay) return;   // top frame only, once

  const CURSOR_SVG =
    '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">' +
    '<path d="M4 2 L4 26 L10 20.5 L14.5 30 L18.6 28.1 L14.2 18.8 L22 18.8 Z" ' +
    'fill="#fff" stroke="#000" stroke-width="1.6" stroke-linejoin="round"/></svg>';

  const CSS = `
    :host { all: initial; }
    .layer   { position: fixed; inset: 0; pointer-events: none; }
    .halo    { position: fixed; left: 0; top: 0; width: 64px; height: 64px;
               margin: -32px 0 0 -32px; border-radius: 50%;
               background: rgba(255, 230, 0, 0.45); will-change: transform; }
    .cursor  { position: fixed; left: 0; top: 0; width: 32px; height: 32px;
               margin: -2px 0 0 -4px;            /* arrow tip (4,2) = hotspot */
               will-change: transform; filter: drop-shadow(0 1px 1px rgba(0,0,0,.35)); }
    .cursor svg { display: block; }
    .ripple  { position: fixed; width: 64px; height: 64px; margin: -32px 0 0 -32px;
               border-radius: 50%; box-sizing: border-box;
               border: 3px solid rgba(255, 170, 0, 0.95);
               background: rgba(255, 220, 0, 0.25); }
    .callout { position: fixed; padding: 8px 16px; width: max-content; max-width: 80vw;
               font: 16pt/1.3 Arial, Helvetica, sans-serif; white-space: pre-wrap;
               border-radius: 2px; box-shadow: 0 2px 8px rgba(0,0,0,.3); }
    .hidden  { visibility: hidden; }
  `;

  let host = null, root = null, halo = null, cursor = null, pos = null;

  // (Re)build the overlay. Called lazily, so it also repairs itself if the page
  // wipes nodes it does not own.
  function ensure() {
    if (host && host.isConnected) return true;
    const parent = document.documentElement;
    if (!parent) return false;
    host = document.createElement('q-overlay');
    host.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
    // Closed shadow root: page CSS cannot restyle the overlay, page JS cannot reach it.
    root = host.attachShadow({ mode: 'closed' });
    root.innerHTML = `<style>${CSS}</style><div class="layer ripples"></div>` +
                     `<div class="halo hidden"></div><div class="cursor hidden">${CURSOR_SVG}</div>`;
    halo = root.querySelector('.halo');
    cursor = root.querySelector('.cursor');
    parent.appendChild(host);   // <html>, not <body>: survives body re-renders
    if (pos) moveTo(pos.x, pos.y);
    return true;
  }

  function moveTo(x, y) {
    pos = { x, y };
    if (!ensure()) return;
    const t = `translate3d(${x}px, ${y}px, 0)`;
    halo.style.transform = t;
    cursor.style.transform = t;
    halo.classList.remove('hidden');
    cursor.classList.remove('hidden');
  }

  function ripple(x, y) {
    if (!ensure()) return;
    const r = document.createElement('div');
    r.className = 'ripple';
    r.style.left = x + 'px';
    r.style.top = y + 'px';
    root.querySelector('.ripples').appendChild(r);
    const anim = r.animate(
      [{ transform: 'scale(0)', opacity: 0.95 }, { transform: 'scale(1)', opacity: 0 }],
      { duration: 650, easing: 'cubic-bezier(.2,.6,.3,1)' });
    anim.onfinish = () => r.remove();
  }

  // Resolves after the callout is gone, so callers can simply await it.
  function showCallout(text, bgColor, textColor, fade, opts) {
    opts = opts || {};
    if (!ensure()) return Promise.resolve();
    const fadeMs = fade ? (opts.fadeMs || 2000) : 0;
    const hold = opts.hold != null ? opts.hold : 1500;

    const el = document.createElement('div');
    el.className = 'callout';
    el.textContent = String(text);          // never innerHTML: text comes from CSV data
    el.style.background = bgColor || '#FFE600';
    el.style.color = textColor || '#000';
    root.appendChild(el);

    // Centered under the anchor element (or top-center), clamped to the viewport.
    let cx = innerWidth / 2, top = 24;
    const anchor = opts.anchor && document.querySelector(opts.anchor);
    if (anchor) {
      const r = anchor.getBoundingClientRect();
      cx = r.left + r.width / 2;
      top = r.bottom + 12;
    }
    const w = el.offsetWidth, h = el.offsetHeight;
    el.style.left = Math.max(8, Math.min(cx - w / 2, innerWidth - w - 8)) + 'px';
    el.style.top = Math.max(8, Math.min(top, innerHeight - h - 8)) + 'px';

    return new Promise(resolve => {
      const done = () => { el.remove(); resolve(); };
      if (!fadeMs) { setTimeout(done, hold); return; }
      const total = 2 * fadeMs + hold;
      el.animate([
        { opacity: 0 },
        { opacity: 1, offset: fadeMs / total },
        { opacity: 1, offset: (fadeMs + hold) / total },
        { opacity: 0 },
      ], { duration: total, fill: 'forwards' }).onfinish = done;
    });
  }

  // Follow the real (trusted) input events coming from Playwright's page.mouse.
  // Capture phase on window: page handlers calling stopPropagation cannot hide them.
  window.addEventListener('mousemove', e => moveTo(e.clientX, e.clientY), { capture: true, passive: true });
  window.addEventListener('mousedown', e => ripple(e.clientX, e.clientY), { capture: true, passive: true });
  document.addEventListener('DOMContentLoaded', ensure);

  window.__qOverlay = { moveTo, ripple, showCallout };
})();
"""


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def _log(msg: str) -> None:
    # Progress goes to stderr; stdout is reserved for results (video paths).
    print("[qscript] " + msg, file=sys.stderr, flush=True)


def _is_navigation_error(err: Exception) -> bool:
    text = str(err).lower()
    return "context was destroyed" in text or "navigat" in text


def _safe_evaluate(page: Page, expression: str, arg=None, retries: int = 3):
    """page.evaluate that survives a navigation happening at the same moment.

    A full page load destroys the JS context; the call is retried on the new document
    (the overlay is re-installed there by the init script).
    """
    for attempt in range(retries + 1):
        try:
            return page.evaluate(expression, arg)
        except PlaywrightError as err:
            if attempt == retries or not _is_navigation_error(err):
                raise
            page.wait_for_load_state("domcontentloaded")


def _safe_filename(name: str) -> str:
    """Make a company name usable as a file name on Windows, macOS and Linux."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)", name):
        name = "_" + name
    return name or "video"


def _smootherstep(t: float) -> float:
    # Minimum-jerk velocity profile (Flash & Hogan): slow start, fast middle, slow stop -
    # the way people move a mouse toward a target.
    return t * t * t * (t * (6 * t - 15) + 10)


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (
        u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
        u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
    )


# --------------------------------------------------------------------------------------
# Human-like mouse + keyboard
# --------------------------------------------------------------------------------------


class _HumanMouse:
    """Drives page.mouse along curved, eased paths and remembers where the pointer is."""

    FPS = 60  # mouse updates per second (the video itself is 25 fps)

    def __init__(self, page: Page, x: float, y: float):
        self.page = page
        self.x, self.y = float(x), float(y)
        page.mouse.move(self.x, self.y)  # one-time placement, before anything is shown

    def resync(self) -> None:
        """Show the overlay cursor at the current position (needed after a page load,
        because the new document has not seen a mousemove yet)."""
        _safe_evaluate(
            self.page,
            "([x, y]) => window.__qOverlay && window.__qOverlay.moveTo(x, y)",
            [self.x, self.y],
        )

    def move_to(self, x: float, y: float) -> None:
        x0, y0 = self.x, self.y
        dx, dy = x - x0, y - y0
        dist = math.hypot(dx, dy)
        if dist < 1:
            return
        # Longer distance -> longer movement (roughly Fitts' law), with some variation.
        duration = min(1.4, 0.35 + dist / 1400) * random.uniform(0.9, 1.1)
        steps = max(10, int(duration * self.FPS))

        # Cubic Bezier bent sideways: hands move in slight arcs, not straight lines.
        nx, ny = -dy / dist, dx / dist
        bend = random.uniform(0.06, 0.18) * dist * random.choice((-1, 1))
        c1 = (x0 + dx * 0.3 + nx * bend, y0 + dy * 0.3 + ny * bend)
        c2 = (x0 + dx * 0.75 + nx * bend * 0.5, y0 + dy * 0.75 + ny * bend * 0.5)

        start = time.perf_counter()
        for i in range(1, steps + 1):
            px, py = _bezier((x0, y0), c1, c2, (x, y), _smootherstep(i / steps))
            self.page.mouse.move(px, py)
            # Schedule against the clock so IPC latency does not stretch the motion.
            delay = start + duration * i / steps - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        self.x, self.y = x, y

    def move_to_element(self, selector: str, timeout_ms: int = 10_000) -> None:
        locator = self.page.locator(selector).first
        locator.wait_for(state="visible", timeout=timeout_ms)
        locator.scroll_into_view_if_needed()
        self.move_to(*self._aim_point(locator.bounding_box()))
        # Layouts shift while results stream in; correct the aim if the target moved.
        box = locator.bounding_box()
        if box and not (box["x"] <= self.x <= box["x"] + box["width"]
                        and box["y"] <= self.y <= box["y"] + box["height"]):
            self.move_to(*self._aim_point(box))

    @staticmethod
    def _aim_point(box) -> Tuple[float, float]:
        if not box:
            raise RuntimeError("target element has no bounding box")
        # Near the centre, but not pixel-perfect every time.
        return (
            box["x"] + box["width"] * random.uniform(0.4, 0.6),
            box["y"] + box["height"] * random.uniform(0.4, 0.6),
        )

    def click(self) -> None:
        # Real mousedown/mouseup -> trusted click: triggers default actions (submit,
        # link navigation) and the overlay's ripple, unlike dispatchEvent().
        time.sleep(random.uniform(0.12, 0.25))  # short aim pause before pressing
        self.page.mouse.down()
        time.sleep(random.uniform(0.06, 0.12))
        self.page.mouse.up()


def _human_type(page: Page, text: str) -> None:
    """Type character by character with irregular, human-like delays."""
    for ch in text:
        page.keyboard.type(ch)  # trusted key events; non-ASCII (ü, ł) via text insertion
        delay = min(0.28, max(0.045, random.gauss(0.11, 0.035)))
        if ch == " ":
            delay += random.uniform(0.05, 0.15)  # tiny pause between words
        elif random.random() < 0.05:
            delay += random.uniform(0.25, 0.5)  # occasional hesitation
        time.sleep(delay)


# --------------------------------------------------------------------------------------
# Public helpers
# --------------------------------------------------------------------------------------


def ShowCallout(text, bgColor=CALLOUT_BG, textColor=CALLOUT_FG, fade=True, *,
                page: Page, hold: float = 1.5, anchor: Optional[str] = None) -> None:
    """Show a rectangle with text (16pt, auto-sized) and wait until it disappears.

    text, bgColor, textColor, fade - as in the task spec; fade=True means 2 s fade-in
    and 2 s fade-out around ``hold`` seconds of full visibility.
    page   - the page to draw on (keyword-only, so the four spec arguments keep their order)
    anchor - optional CSS selector; the callout is centred just below that element,
             otherwise at the top centre of the viewport.
    """
    _safe_evaluate(
        page,
        "([t, bg, fg, fade, opts]) => window.__qOverlay.showCallout(t, bg, fg, fade, opts)",
        [str(text), bgColor, textColor, bool(fade), {"hold": int(hold * 1000), "anchor": anchor}],
    )


# --------------------------------------------------------------------------------------
# Scenario
# --------------------------------------------------------------------------------------


def _run_sample_behavior(page: Page, url: str, company_name: str, city: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
    page.locator(SEARCH_BOX).first.wait_for(state="visible", timeout=PAGE_LOAD_TIMEOUT_MS)

    mouse = _HumanMouse(page, VIDEO_SIZE["width"] * 0.5, VIDEO_SIZE["height"] * 0.62)
    mouse.resync()

    ShowCallout(f"{company_name}: Magento developer search in {city}",
                CALLOUT_BG, CALLOUT_FG, True, page=page, hold=1.0, anchor=SEARCH_BOX)

    # Search box: glide, click (ripple + focus), type.
    mouse.move_to_element(SEARCH_BOX)
    mouse.click()
    time.sleep(0.3)
    _human_type(page, f"Magento developer in {city}")
    time.sleep(0.6)

    # Search button. After typing, the visible submit button is the one in the
    # suggestions dropdown; the header one is collapsed to width 0.
    try:
        mouse.move_to_element(f"{SEARCH_BUTTON} >> visible=true", timeout_ms=5_000)
        mouse.click()
    except PlaywrightTimeoutError:
        _log("no visible search button, submitting with Enter")
        page.keyboard.press("Enter")

    # Full page navigation happens here. wait_for_selector keeps polling across it.
    page.wait_for_selector(FIRST_RESULT, state="visible", timeout=RESULTS_TIMEOUT_MS)
    mouse.resync()  # new document: put the overlay cursor back where the mouse is
    time.sleep(0.8)  # let the first screen of results settle before aiming

    results_url = page.url
    mouse.move_to_element(FIRST_RESULT)
    time.sleep(0.3)
    mouse.click()

    # Show the opened page for a moment. A slow third-party site is not a failure:
    # the required action (clicking the first result) has already happened.
    try:
        page.wait_for_url(lambda u: u != results_url, timeout=DESTINATION_TIMEOUT_MS)
        page.wait_for_load_state("domcontentloaded", timeout=DESTINATION_TIMEOUT_MS)
        mouse.resync()
    except PlaywrightError as err:
        _log(f"first result did not finish loading: {str(err).splitlines()[0]}")
    time.sleep(2.5)


def CaptureVideo_Magento(
    url,
    company_name,
    language,
    country,
    postcode,
    city,
    street,
    house_no,
):
    """Record the sample scenario for one CSV row.

    Returns the absolute path of ``<PATH_TO_VIDEO_FOLDER>/<company_name>.webm``,
    or None on failure. ``language`` is used as the browser locale; ``country``,
    ``postcode``, ``street`` and ``house_no`` are accepted for the real Magento
    scenario and are not needed by the DuckDuckGo sample.
    """
    folder = os.path.abspath(PATH_TO_VIDEO_FOLDER)  # read now: main may have overridden it
    os.makedirs(folder, exist_ok=True)
    final_path = os.path.join(folder, _safe_filename(company_name) + ".webm")

    raw_path = None
    ok = False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            try:
                context = browser.new_context(
                    viewport=VIDEO_SIZE,  # same as video size: no scaling, no grey bars
                    record_video_dir=folder,
                    record_video_size=VIDEO_SIZE,
                    locale=(language or "").strip() or None,
                )
                context.add_init_script(_OVERLAY_JS)
                page = context.new_page()
                try:
                    _run_sample_behavior(page, url, company_name, city)
                    ok = True
                except Exception:
                    _log(f"scenario failed for {company_name!r}:\n{traceback.format_exc()}")
                finally:
                    context.close()  # finalizes the .webm
                    # Ask for the path while Playwright is still running.
                    raw_path = page.video.path() if page.video else None
            finally:
                browser.close()
    except Exception:
        _log(f"browser error for {company_name!r}:\n{traceback.format_exc()}")

    if not raw_path or not os.path.exists(raw_path):
        return None
    try:
        if not ok:
            # Keep the partial recording for debugging, but report failure.
            failed_path = final_path[: -len(".webm")] + ".failed.webm"
            os.replace(raw_path, failed_path)
            _log(f"partial video kept at {failed_path}")
            return None
        os.replace(raw_path, final_path)  # rename/move; overwrites an older run
        return final_path
    except Exception:
        _log(f"could not move video for {company_name!r}:\n{traceback.format_exc()}")
        return None
