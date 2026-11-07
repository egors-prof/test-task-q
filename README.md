# CSV-driven Browser Automation Video Recorder

Records a video of an automated browser session for every row of `input.csv`
(Python + Playwright, sync API, Chromium).

**Demo video:** [`demo/Acme Minsk.webm`](demo/Acme%20Minsk.webm)

## Run

```bash
pip install -r requirements.txt
python -m playwright install chromium
python capture_video.py                 # reads input.csv, writes ./video/<Company Name>.webm
```

Options: `python capture_video.py other.csv --video-folder out --headless`

- **stdout:** one absolute video path per processed row
- **stderr:** progress and errors
- **Exit code:** `0` if all rows succeeded, `1` if some row failed, `2` if the input is invalid

A Chromium window opens during recording. That is intentional: DuckDuckGo answers searches from
headless Chromium with "Unexpected error" (see below). On a Linux server without a display, use `xvfb-run python capture_video.py`.

## Files

| File | Purpose |
|---|---|
| `capture_video.py` | Reads the CSV with `csv.DictReader(f, delimiter=";", quotechar='"')` and calls `qscript.CaptureVideo_Magento(...)` for each row |
| `qscript.py` | `PATH_TO_VIDEO_FOLDER`, `CaptureVideo_Magento(...)`, `ShowCallout(...)`, the injected overlay (cursor, halo, ripple, callout) and the sample scenario |
| `input.csv` | Sample input: 3 rows. It includes a quoted field containing `;` and non-ASCII values (`München`, `Marszałkowska`) |
| `requirements.txt` | `playwright>=1.49` |

## What the scenario does

1. Opens `url` (DuckDuckGo) with the browser locale set from the `Language` column.
2. Shows the SVG cursor and a yellow callout under the search box (2 s fade-in, 1 s hold, 2 s fade-out).
3. Glides the cursor to the search box, clicks, and types `Magento developer in <City>` character by character.
4. Glides to the search button and clicks it. DuckDuckGo then performs a full page navigation.
5. Waits for the results (up to 45 s), glides to the first result and clicks it.
6. Stays on the opened page for a moment, closes the context and moves the video to
   `<PATH_TO_VIDEO_FOLDER>/<Company Name>.webm`.

## Technical decisions

### Cursor and effects follow real mouse events
Playwright's video does not include the OS cursor, so an overlay is injected. The overlay never animates
on its own. It listens to the real `mousemove` and `mousedown` events produced by `page.mouse` and draws
the arrow, halo and ripple at `event.clientX/Y`. As a result:
- There is a single source of truth: the drawn cursor is exactly where the browser's mouse is.
  Hover states, the drawn cursor and the click target always agree.
- Every click gets a ripple automatically. No step can forget to call it.

### The overlay is injected with `context.add_init_script`
- It runs in every new document, so the cursor still works after DuckDuckGo's full page reload and on
  the clicked result's site.
- The script is an IIFE, because `add_init_script` evaluates the string as-is.
- It runs in the top frame only, so iframes don't get a second cursor.

### The overlay is isolated in a closed Shadow DOM
- It is attached to `<html>`, not `<body>`, so frameworks re-rendering the body do not delete it.
- It uses `pointer-events: none` and `z-index: 2147483647`.
- The closed shadow root means the page's CSS can't restyle it and the page's scripts can't reach it.
- If the page removes it anyway, the next event re-creates it.

### Real (trusted) input instead of JavaScript-dispatched events
Clicks use `page.mouse.down()/up()` and typing uses `page.keyboard.type(char)`, which go through the Chrome DevTools Protocol.
- Synthetic `dispatchEvent` events don't trigger default actions (form submit, link navigation).
- React-controlled inputs would need the native-setter hack.
- Real key presses avoid both problems, open DuckDuckGo's suggestion dropdown as a real user would, and
  type non-ASCII characters like `ü` and `ł` correctly.

### Human-like motion
- **Path:** a cubic Bézier curve with a random sideways bend. Hands move in arcs, not ruler-straight lines.
- **Speed profile:** minimum-jerk / smootherstep (`6t⁵−15t⁴+10t³`). This is the classic model of human reaching
  movements: slow start, fast middle, slow stop.
- **Duration** grows with distance (roughly Fitts' law), with ±10 % jitter.
- **Updates:** 60 mouse updates per second, scheduled against the clock, so IPC latency does not stretch the motion.
- **Aim point:** a random spot near the target's center. If the layout shifted during the move (results stream in),
  a short correction move follows.
- **Typing:** Gaussian delays of about 110 ms per character, longer pauses after spaces and occasional hesitations.

### Navigation handling
- DuckDuckGo's search reloads the whole page, which destroys the JavaScript context.
- Because clicks are done with `page.mouse` instead of `page.evaluate`, no in-flight JS call can be killed by that navigation.
- After the click, `page.wait_for_selector(FIRST_RESULT)` polls across the navigation, with a generous 45 s timeout.
- The remaining `evaluate` calls (cursor re-sync, callout) go through `_safe_evaluate`. On a "context was destroyed" or
  navigation error, it waits for the new document and retries.
- After each page load, the cursor is re-placed at the remembered position, because the new document hasn't
  seen a mousemove event yet.

### Search button
- On the current DuckDuckGo page, `form button[type="submit"]` matches two buttons.
- The header button is collapsed to width 0. After typing, the visible one is the "Search" button in the
  suggestions dropdown (the same button used in the reference video).
- The code uses the first *visible* match (`>> visible=true`). If none is visible, it falls back to pressing Enter.

### Headed browser by default
- DuckDuckGo returns "Unexpected error" for searches from headless Chromium (user agent `HeadlessChrome`).
- Instead of faking a user agent, the tool runs a real browser window (`HEADLESS = False`). Playwright records
  video in headed mode as well.
- `--headless` and `qscript.HEADLESS` remain available for sites that allow headless browsers.

### Video
- `record_video_dir=PATH_TO_VIDEO_FOLDER`, `record_video_size=1280×720` and a viewport of the same size,
  so there is no scaling and no grey bars.
- `context.close()` finalizes the file. `os.replace` then renames it to `<Company Name>.webm`: an atomic move, no copy,
  and it overwrites an older run.
- Company names are sanitized for file names (`<>:"/\|?*`, reserved Windows names). Unicode is kept.
- `PATH_TO_VIDEO_FOLDER` is read when the function is called, not when the module is imported, so
  `capture_video.py --video-folder` can override it at startup.
- On failure the function returns `None` (per the spec). The partial recording is kept as `<name>.failed.webm`
  for debugging.

### CSV
- The file is opened with `encoding="utf-8-sig"`, which also accepts the byte-order mark Excel adds when saving "CSV UTF-8".
  Without it, the first column would be named `﻿URL`.
- It uses `newline=""`, as the `csv` module requires, so quoted fields can contain line breaks.
- The header is validated, and all rows are read before any browser starts, so a malformed file fails immediately.
- Values are stripped of surrounding whitespace.
- `capture_video.py` forces UTF-8 output. Otherwise the Windows console or a pipe (cp1252) crashes when
  printing `München.webm`.

### Parameters
- `language` is used as the browser locale (`en-US`, `de-DE`, …). This makes the UI language deterministic instead of
  depending on the machine's settings.
- `country`, `postcode`, `street` and `house_no` are accepted with the exact signature for the real Magento scenario.
  The DuckDuckGo sample doesn't need them.

### Compatibility
- The code uses Python 3.9 syntax (no `match` statements, no `X | Y` type hints).
- `requirements.txt` sets only a lower bound (`playwright>=1.49`). Playwright 1.61+ requires Python 3.10, so an exact
  pin to the latest version would break the "Python 3.9+" constraint. With the lower bound, pip installs 1.60 on 3.9
  and the newest release on 3.10+.
- Tested with Python 3.14 and Playwright 1.64.1 on Windows 11.

## Known limitations
- The selectors are DuckDuckGo's markup as of November 2026. If DuckDuckGo changes it, update the constants at the top of `qscript.py`.
- If the first result's site is slow, the video shows the page as far as it got within 8 s. This is logged, not treated as a failure,
  because the required action (the click) already happened.
