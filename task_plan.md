# Task Plan: Douyin Comment Crawler

## Goal
Build and verify a Playwright-based program that collects only first-level Douyin comments, downloads comment images, and creates a fresh per-run output directory containing one JSON file plus image files.

## Requirements
- Use Playwright.
- Implement with Python 3.12 (`requires-python >=3.12,<3.13`).
- Collect first-level comments only.
- Preserve comment text and its relationship to downloaded images.
- Keep scrolling the comment list in the direction required by Douyin's lazy loading until all available comments are loaded.
- Create a new timestamped output directory on every run.
- Reduce account risk with conservative pacing, retries, persistent login state, and manual handling of login/captcha challenges.

## Phases

### Phase 1: Inspect repository, conventions, and runtime
**Status:** complete

### Phase 2: Design structure, selectors, schema, and risk controls
**Status:** complete

### Phase 3: Implement crawler, CLI, configuration, and documentation
**Status:** complete

### Phase 4: Test parsing, deduplication, paths, and termination
**Status:** complete

### Phase 5: Run exact-version verification and finalize usage
**Status:** complete

### Phase 6: Convert downloaded WebP images to JPEG
**Status:** complete

### Phase 7: Extend initial/manual login waiting and verify
**Status:** complete

### Phase 8: Optimize direct PyCharm Run workflow
**Status:** complete

### Phase 9: Move cursor into comment panel before every wheel action
**Status:** complete

### Phase 10: Prevent comment scrolling from switching videos
**Status:** complete

### Phase 11: Identify and isolate the real Douyin comment viewport
**Status:** complete

### Phase 12: Anchor cursor and scrolling to exact `comment-list`
**Status:** complete

### Phase 13: Eliminate wheel events and scroll the geometry-matched container
**Status:** complete

### Phase 14: Adopt coordinate-probe scrolling from the proven browser script
**Status:** complete

### Phase 15: Build a local directory-based comment viewer
**Status:** complete

## Decisions
- Do not implement captcha bypass, fingerprint spoofing, or aggressive request interception.
- Prefer a dedicated persistent system-Chrome profile so login can be reused without attaching to unrelated daily-browser tabs.
- Listen to Playwright `response` events for the top-level comment-list endpoint and use UI scrolling only to trigger normal lazy loading.
- Ignore reply-list endpoints and nested reply arrays; retain only top-level comment objects.
- Download images sequentially with the browser context's authenticated request client.
- Default scroll direction to `down` because the user-provided working reference labels that direction as loading more; retain `--scroll-direction up` for UI variants.
- Replace the interrupted Node.js implementation completely; no generated `.js` files or Node package metadata will remain.
- Use Playwright's Python async API and Python's built-in `unittest` runner.
- Default to a dedicated persistent system-Chrome profile; retain `--cdp` only as an optional mode because Playwright initializes every target in an existing Chrome and can hang on large daily sessions.
- Convert WebP comment images to JPEG for convenient local viewing.
- Give first-time profile login and detected verification prompts 30 minutes by default.
- Before each lazy-loading action, move the cursor into the visible comment panel and pause briefly.
- Resolve only an exact visible `comment-list`, move the physical cursor into it, verify the pointer hit, and directly scroll the nearest geometry-matched scroll owner without emitting a wheel event.

## Errors Encountered
| Error | Attempt | Resolution |
|---|---:|---|
| `git status` reported that the directory is not a Git repository | 1 | Treat the directory as a new empty project; avoid Git-dependent verification. |
| `tldr` is not installed | 1 | Used `rg --files` because the project contains no source code to map. |
| Image parsing test returned download URL before original URL and exposed duplicate variants | 1 | Change extraction to select one preferred URL per image object, prioritizing the original image. |
| `python3.12` is not available in PATH; `python3` is 3.13.12 | 1 | Target and declare Python 3.12 exactly, then use an available 3.12 runtime if `uv`/pyenv provides one; otherwise run compatibility checks on 3.13 and report the limitation. |
| `ModuleNotFoundError: playwright` during integration import check | 1 | Create an isolated local virtual environment and install the declared Playwright dependency for connection-level verification. |
| Playwright `connect_over_cdp("http://127.0.0.1:9222")` returned 404 for `/json/version/` | 1 | Current Chrome exposes a WebSocket path in `DevToolsActivePort`; CDP remains optional, while the reliable default is a dedicated persistent profile. |
| Direct Playwright CDP initialization still timed out after 60 seconds | 2 | Raw CDP is healthy; Playwright stalls while initializing all existing tabs/iframes. Make a dedicated persistent Chrome profile the default and keep CDP optional. |
| Connected Chrome timed out even when counting exact `[data-e2e="comment-list"]` | 3 | Stop repeating external DOM inspection; implement the exact selector inside the crawler's already-controlled Playwright page and expose concise runtime diagnostics. |

## Verification
- Python 3.12.13: compileall passed.
- Python 3.12.13: 12/12 unit tests passed.
- Python 3.12.13 + Playwright 1.61: CLI help passed.
- Python 3.12.13 + Playwright 1.61 + system Chrome: persistent-profile launch/close smoke test passed.
- Confirmed no generated JavaScript or Node package files remain.
- PyCharm interpreter `/opt/miniconda3/envs/ai_application/bin/python`: Python 3.12.13.
- Pillow 12.3.0 installed into the PyCharm interpreter.
- WebP-to-JPEG conversion test passed.
- Direct `__main__.py --help` launch passed.
- Final test suite: 16/16 passed.
- PyCharm interpreter after cursor-placement change: 18/18 tests passed.
- PyCharm interpreter after direct DOM-scroll change: compileall, CLI startup, and 18/18 tests passed.
- PyCharm interpreter after nearest-comment-scroller isolation: compileall, CLI startup, and 20/20 tests passed.
- PyCharm interpreter after exact `comment-list` anchoring: compileall, CLI startup, and 20/20 tests passed.
- PyCharm interpreter after wheel-event elimination: compileall, CLI startup, and 21/21 tests passed.
- PyCharm interpreter after coordinate-probe adoption: compileall, CLI startup, and 19/19 tests passed.
- PyCharm interpreter after local viewer addition: compileall, viewer startup/help, inline-script syntax, and 21/21 tests passed.
