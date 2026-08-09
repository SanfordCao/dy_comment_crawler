# Findings

## Repository
- The project directory contains no application code or package metadata.
- It is not currently a Git repository.
- `tldr` is unavailable, but `rg --files` confirmed that only the three planning files exist.
- The user requires Python 3.12; the Node.js implementation has been removed.
- The current shell exposes Python 3.13.12 as `python3`, but no `python3.12` executable.

## Planned Architecture
- `src/douyin_comment_crawler/cli.py`: argument parsing and process lifecycle.
- `src/douyin_comment_crawler/crawler.py`: Playwright connection, page preparation, lazy loading, extraction, download, and result writing.
- `src/douyin_comment_crawler/comments.py`: pure first-level comment parsing and deduplication helpers.
- `src/douyin_comment_crawler/output.py`: unique run directory and JSON/image path handling.
- `tests/`: Python built-in `unittest` tests.
- `output/<timestamp>-<aweme-id>/comments.json` plus downloaded image files.

## Python Runtime
- Project metadata will enforce `requires-python = ">=3.12,<3.13"`.
- The implementation uses `playwright.async_api`, `asyncio`, `dataclasses`, `pathlib`, and built-in `unittest`.
- The local shell currently has Python 3.13.12 only; no `uv`, `pyenv`, `mise`, or `python3.12` command is present.
- Python 3.13 can still provide a useful syntax/unit-test compatibility check, but final reporting must distinguish that from an exact 3.12 run.
- A temporary Python 3.12.13 environment was created under `/private/tmp`; all tests and CLI startup passed there.

## Chrome CDP Compatibility
- Chrome's current `chrome://inspect/#remote-debugging` UI reports port 9222, but `GET /json/version/` returns 404.
- Therefore Playwright 1.61 cannot resolve a WebSocket URL from the plain HTTP endpoint and needs an explicitly resolved `ws://` endpoint.
- Raw CDP requests succeed. Playwright's timeout occurs because it initializes all existing targets in the user's large daily Chrome session.
- A dedicated persistent system-Chrome profile launched successfully under Python 3.12.13 and Playwright 1.61, so it is the verified default.

## Data Collection Design
- Attach the response listener before navigation so the first comment page is captured.
- Accept only Douyin response URLs that contain a comment-list path and reject reply-list paths.
- Track `has_more` and `cursor` from captured pages; use idle rounds as a fallback termination signal.
- Parse only the response's top-level `comments` array. Never traverse `reply_comment` or related nested reply fields.
- Locate the visible scrollable element containing the comment region using semantic `data-e2e` hints plus scroll metrics rather than generated class names.
- Move the mouse over that element, then change that verified element's scroll position directly. Page-level wheel events must not be used because Douyin can interpret them as previous/next-video navigation.
- JSON comment records will include ID, author, text, timestamps/counts, source image URLs, and downloaded relative image filenames.

## Douyin UI / Data
- The supplied page uses a `/user/self?...&modal_id=<aweme_id>` URL and renders the target post in a modal.
- Comments are lazy-loaded in a scrollable panel; the implementation must detect the actual scroll container and progress without relying on fixed CSS class names alone.

## Security and Risk
- Use conservative randomized delays and bounded retries.
- Reuse a logged-in Chrome session through Playwright CDP.
- Stop and ask for manual intervention when login/captcha/risk verification is shown.
- Do not bypass platform protections.
- Default to a single video per run, human-like randomized pauses, no concurrent comment/image scraping, and a hard maximum duration/comments limit.
- A dedicated persistent Chrome profile is safer and more reliable than attaching Playwright to every tab in the user's daily Chrome. It retains Douyin login between runs without exposing unrelated tabs.

## Follow-up Improvements
- Existing behavior saved WebP bytes unchanged and only allowed 5 minutes for a detected login challenge.
- WebP downloads will be decoded with Pillow and saved as quality-92 JPEG, compositing transparent pixels onto white.
- A new persistent profile will always pause for initial login, even if Douyin's current login UI text is not matched.
- When PyCharm runs `__main__.py` without Script parameters, the program now prompts for the URL in the Run console.
- Package-relative imports and direct-script fallbacks allow both PyCharm script execution and installed/module execution.
- The earlier scroll path still used `page.mouse.wheel()` after cursor placement, so the event could reach Douyin's global video navigation layer.
- The safe path requires a genuinely scrollable comment element and calls its DOM `scrollBy()` directly. If no such element is found, no scroll event is sent.
- The first direct-DOM implementation could still select the video feed itself because it scored every scrollable ancestor containing comment descendants.
- A safe comment scroller must be the nearest scrollable ancestor of a concrete comment item/list anchor, occupy only the right-side narrow panel, use scrollable overflow, and contain no `<video>`. The same invariants must be checked again immediately before mutation.
- The connected daily-Chrome page timed out even for an exact `[data-e2e="comment-list"]` locator count, so external DOM inspection is not a reliable verification path for this unusually large tab.
- The next implementation must resolve an exact visible `comment-list` in the crawler-owned page, move the physical cursor into its bounding box, and print a small locator diagnostic in the PyCharm console.
- The exact-list strategy uses only equality selectors, verifies the pointer hit with `elementFromPoint`, then sends a native wheel while stopping propagation at `comment-list`. It has no approximate right-panel or ancestor fallback.
- Exact cursor placement was insufficient because Douyin can observe native wheel events before target-level propagation is stopped.
- The revised strategy emits no wheel event. It anchors on exact `comment-list`, requires a nearest scrollable ancestor with closely matching horizontal geometry, and changes only that element's scroll position.
- The user supplied a browser-console implementation that works from a fixed right-side probe point rather than semantic selectors. The crawler should mirror its `elementFromPoint` → overflow-ancestor → `scrollTop` plus synthetic `WheelEvent` sequence.
- The supplied reference explicitly labels positive/downward scrolling as “加载更多”, so PyCharm's no-argument default should be `down`; `up` remains configurable.

## Local Comment Viewer
- A completed run contains top-level metadata plus `comments`; each comment has `text`, `author`, timestamps, IP label, counts, and `images`.
- Each successful image record has a local filename in `images[].file`; failed records may have `file: null` and an error.
- A browser directory input exposes every selected file through `webkitRelativePath`, allowing `comments.json` and referenced JPEG files to be matched entirely client-side without uploads.
- The viewer should remain local-only, use text nodes rather than HTML injection for comment content, and revoke generated object URLs when another directory is selected.
