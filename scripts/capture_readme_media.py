"""Capture the README media from demo mode: the hero GIF and light/dark screenshots.

Manual script. It costs nothing and makes no network call beyond loopback: it starts
``llm-crossjury ui --demo`` on a free port, drives the UI in headless Edge or Chrome
over the DevTools protocol, and writes to ``docs/assets/``:

* ``hero.gif``: the full loop in the conversation UI: ask the jury, the answers side by
  side, AI synthesize, the final answer and the additional list, then "Continue with"
  one model and a chat reply (about 30 s, looping);
* ``<name>-light.png`` and ``<name>-dark.png`` for every name in :data:`STILLS`;
* ``hero.mp4`` when ``ffmpeg`` is on PATH (skipped otherwise).

Usage::

    uv run python scripts/capture_readme_media.py              # everything
    uv run python scripts/capture_readme_media.py --no-gif     # screenshots only
    uv run python scripts/capture_readme_media.py --no-stills  # hero.gif only
    uv run python scripts/capture_readme_media.py --only wizard --only models
    uv run python scripts/capture_readme_media.py --browser "C:/path/to/chrome.exe"

Dependencies: only what the project already installs (``websockets`` for the DevTools
connection, Pillow for the GIF, ``python-docx`` for the sample Word file). The sample
attachments are generated at run time.

**After a UI change**, run the script and look at the images. If it stops with
``timed out waiting for ...`` or ``missing element``, the message names the key in
:data:`SEL`; update that selector (and, for buttons, :data:`BUTTON_TEXT`, the fallback
used when the selector finds nothing). What the script relies on:

* home page (``/``): the scroll container ``#main`` (the page itself does not scroll),
  the composer ``#task``, ``#estimate-btn``, ``#run-btn`` ("Ask models"), ``#send-btn``,
  the jury panel ``#jury-panel`` with one checkbox ``#use-<provider>`` per provider and a
  radio ``input[name="backend-<provider>"][value="cli"]`` for subscriptions, the history
  list ``#history`` and the file input ``#attach-input`` / list ``#attach-list``;
* a jury turn ``.turn-jury`` with ``.jury-card``, one ``article.answer`` per member (an
  ``.answer-body`` once answered, ``.pending`` or ``aria-busy`` while waiting) and a
  ``.btn-continue`` button per answer; its synthesis panel ``.panel.synthesis`` with
  ``.syn-btn``, an optional ``.decision`` ("Continue with review"), and the result
  ``.synth-result`` holding ``.final`` and ``.additional``; the subscription consent
  checkbox ``#consent``;
* a chat turn ``.turn-chat`` whose reply is ``.bubble-model``;
* the setup wizard on ``/settings?onboarding=1``: ``#wizard``, ``#wizard-next`` and the
  subscription rows ``#cli-rows``;
* dark mode via ``prefers-color-scheme`` (the script emulates it, and also emulates
  ``prefers-reduced-motion: reduce`` so the page scrolls instantly);
* the demo labels ``#demo-badge`` (home) and ``#demo-banner`` (settings), which the
  script hides (``--show-banner`` keeps them).

The jury cards may render answers one by one and offer a retry per member: the script
only waits until every selected member shows an answer body and nothing is pending.

Determinism: the demo server starts with an empty temporary data dir, the providers
are deterministic, the label shuffle seed is pinned (the script injects ``seed`` into
synthesis requests), and frame timings are scripted. Only the measured latency shown
per answer can differ by a tenth of a second between runs. The history sidebar is
seeded with a few example conversation titles (:data:`HISTORY_TITLES`) through the
demo server's own API.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from websockets.sync.client import ClientConnection, connect

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "assets"
KEY_ENVS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY")
PROVIDERS = ("anthropic", "openai", "gemini", "xai")
CONTINUE_INDEX = 1
"""Which answer's "Continue with" button the flow clicks (0-based; 1 = OpenAI)."""
MIN_SCROLL_PX = 2
SEED = 20261108
"""Label shuffle seed injected into synthesis requests, so labels are stable."""
HISTORY_TITLES = (
    "Mixed model or repeated-measures ANOVA for my pilot data?",
    "Check the induction step in Lemma 2 of my draft",
    "Summarize the methods section of the attached paper",
)
"""Older conversations created before the capture, oldest first (demo data only)."""
STILL_HEIGHT = 1500
"""Viewport height for element stills, so a tall element fits without inner scrolling."""

BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
BROWSER_NAMES = (
    "msedge",
    "microsoft-edge",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "chrome",
)

SEL: dict[str, str] = {
    # home page: composer and jury panel
    "task": "#task",
    "use": "#use-{provider}",
    "backend": 'input[name="backend-{provider}"][value="{backend}"]',
    "estimate_btn": "#estimate-btn",
    "run_btn": "#run-btn",
    "send_btn": "#send-btn",
    "jury_panel": "#jury-panel",
    "estimate": "#estimate",
    "consent": "#consent",
    "dock": ".dock",
    "attach_input": "#attach-input",
    "attach_list": "#attach-list",
    "history": "#history",
    # jury turn
    "jury_turn": ".turn-jury",
    "jury_card": ".turn-jury .jury-card",
    "answer_body": ".turn-jury article.answer .answer-body",
    "pending": ".turn-jury .answers .pending, .turn-jury .answers [aria-busy='true']",
    "continue_btn": ".turn-jury article.answer .btn-continue",
    "synthesis": ".turn-jury .panel.synthesis",
    "syn_btn": ".turn-jury .syn-btn",
    "decision": ".turn-jury .decision:not([hidden])",
    "review_btn": ".turn-jury .decision:not([hidden]) .btn-review",
    "syn_result": ".turn-jury .synth-result:not([hidden])",
    "final": ".turn-jury .synth-result .final",
    "additional": ".turn-jury .synth-result .additional",
    # chat turn
    "chat_turn": ".turn-chat",
    "chat_reply": ".turn-chat .bubble-model",
    # settings page: setup wizard
    "wizard": "#wizard",
    "wizard_next": "#wizard-next",
    "cli_rows": "#cli-rows",
    # demo labels and overlays (hidden unless --show-banner)
    "banner": "#demo-banner, #demo-badge",
    "tour": ".tour, .tour-ring",
}
"""CSS selectors the script relies on (see the module docstring)."""

BUTTON_TEXT = {
    "estimate_btn": "Estimate",
    "run_btn": "Ask models",
    "send_btn": "Send",
    "syn_btn": "AI synthesize",
    "review_btn": "Continue with review",
}
"""Fallback: a visible button whose text starts with this, if the selector finds nothing."""

STILLS = (
    "answers",
    "synthesis",
    "additional-list",
    "conversation",
    "models",
    "attachments",
    "wizard",
)
"""Screenshot names, written as ``<name>-light.png`` and ``<name>-dark.png``."""

PAGE_HELPERS = """
window.__cap = {
  q(sel, text) {
    let el = document.querySelector(sel);
    if (!el && text) {
      const want = text.toLowerCase();
      el = [...document.querySelectorAll('button, [role="button"], a')]
        .find((b) => b.offsetParent !== null
          && b.textContent.trim().toLowerCase().startsWith(want)) || null;
    }
    return el;
  },
  visible(el) {
    if (!el) return false;
    if (el.closest('[hidden]')) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  },
  scroller() {
    return document.querySelector('#main.thread-scroll') || document.scrollingElement;
  },
  top(el) {
    const s = this.scroller();
    const base = s === document.scrollingElement ? 0 : s.getBoundingClientRect().top;
    return el.getBoundingClientRect().top - base + s.scrollTop;
  },
  maxScroll() { const s = this.scroller(); return s.scrollHeight - s.clientHeight; },
  viewBottom() {
    const s = this.scroller();
    return s === document.scrollingElement ? window.innerHeight : s.getBoundingClientRect().bottom;
  },
};
"""

PAGE_SETUP = """
window.confirm = () => true;
(() => {
  const original = window.fetch.bind(window);
  window.fetch = (input, init) => {
    try {
      const url = typeof input === 'string' ? input : input.url;
      if (url.includes('/api/synthesis/') && init && typeof init.body === 'string') {
        const body = JSON.parse(init.body);
        if (body.seed === null || body.seed === undefined) {
          body.seed = __SEED__;
          init = Object.assign({}, init, { body: JSON.stringify(body) });
        }
      }
    } catch (_) { /* leave the request unchanged */ }
    return original(input, init);
  };
})();
""".replace("__SEED__", str(SEED))


# --------------------------------------------------------------------------- processes


def free_port() -> int:
    """Return a free TCP port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, timeout_s: float = 30.0) -> bytes:
    """Poll ``url`` until it answers 200; return the body."""
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - loopback
                return bytes(response.read())
        except OSError as exc:
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not answer within {timeout_s} s ({last})")


def find_browser(explicit: str | None) -> str:
    """Locate Edge or Chrome (``--browser`` wins)."""
    if explicit:
        return explicit
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("No Edge or Chrome found; pass --browser PATH.")


@contextmanager
def demo_server() -> Iterator[str]:
    """Run ``llm-crossjury ui --demo`` on a free port; yield its base URL."""
    port = free_port()
    env = {k: v for k, v in os.environ.items() if k not in KEY_ENVS}
    cmd = [sys.executable, "-m", "llm_crossjury", "ui", "--demo", "--no-browser"]
    proc = subprocess.Popen(  # noqa: S603 - fixed argument list
        [*cmd, "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        wait_http(base + "/api/providers")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@contextmanager
def headless_browser(executable: str, width: int, height: int) -> Iterator[str]:
    """Start a headless browser with remote debugging; yield the page's WebSocket URL."""
    port = free_port()
    profile = tempfile.mkdtemp(prefix="crossjury-capture-")
    args = [
        executable,
        "--headless=new",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        f"--window-size={width},{height}",
        "--force-device-scale-factor=1",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
        "--lang=en-US",
        "about:blank",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
    try:
        wait_http(f"http://127.0.0.1:{port}/json/version")
        targets = json.loads(wait_http(f"http://127.0.0.1:{port}/json/list"))
        page = next(t for t in targets if t.get("type") == "page")
        yield str(page["webSocketDebuggerUrl"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


# --------------------------------------------------------------------------- devtools


class Page:
    """A minimal DevTools protocol client for one page."""

    def __init__(self, ws: ClientConnection, width: int, height: int) -> None:
        self._ws = ws
        self._next = 0
        self.width = width
        self.height = height

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one command and return its result (events are skipped)."""
        self._next += 1
        msg_id = self._next
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            reply = json.loads(self._ws.recv(timeout=60))
            if reply.get("id") == msg_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return dict(reply.get("result", {}))

    def setup(self) -> None:
        """Enable domains, set the viewport and install the page helpers."""
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": self.width, "height": self.height, "deviceScaleFactor": 1, "mobile": False},
        )
        self.send("Page.addScriptToEvaluateOnNewDocument", {"source": PAGE_HELPERS + PAGE_SETUP})

    def js(self, expression: str, *, await_promise: bool = False) -> Any:
        """Evaluate ``expression`` in the page and return its value."""
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"JS error in {expression[:80]!r}: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def wait(self, expression: str, timeout_s: float = 20.0, interval_s: float = 0.1) -> None:
        """Wait until ``expression`` is truthy."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.js(expression):
                return
            time.sleep(interval_s)
        raise TimeoutError(f"timed out waiting for {expression}")

    def resize(self, width: int, height: int) -> None:
        """Change the emulated viewport."""
        self.width, self.height = width, height
        self.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )

    def goto(self, url: str, ready: str, *, show_banner: bool) -> None:
        """Navigate and wait until ``ready`` is truthy; hide the demo labels and the tour."""
        self.send("Page.navigate", {"url": url})
        self.wait("document.readyState === 'complete' && !!window.__cap")
        self.wait(ready)
        time.sleep(0.3)  # the tour, if any, opens on the next animation frame
        self.js("window.CJ && window.CJ.tour && window.CJ.tour.close && window.CJ.tour.close()")
        hide = ["tour"] if show_banner else ["tour", "banner"]
        for key in hide:
            self.js(f"document.querySelectorAll({json.dumps(SEL[key])}).forEach((e) => e.remove())")
        self.js("document.fonts ? document.fonts.ready.then(() => true) : true", await_promise=True)

    def theme(self, dark: bool) -> None:
        """Emulate ``prefers-color-scheme`` (and reduced motion, for instant scrolling)."""
        value = "dark" if dark else "light"
        self.send(
            "Emulation.setEmulatedMedia",
            {
                "features": [
                    {"name": "prefers-color-scheme", "value": value},
                    {"name": "prefers-reduced-motion", "value": "reduce"},
                ]
            },
        )

    def screenshot(self, clip: dict[str, float] | None = None) -> bytes:
        """PNG of the viewport, or of ``clip`` (page coordinates)."""
        params: dict[str, Any] = {"format": "png", "fromSurface": True}
        if clip is not None:
            params["clip"] = {**clip, "scale": 1}
            # Never beyond the viewport: that re-lays out the page at the clip's size.
            params["captureBeyondViewport"] = False
        return base64.b64decode(self.send("Page.captureScreenshot", params)["data"])

    # -- element helpers

    def el(self, key: str, **fmt: str) -> str:
        """JS expression for the element ``SEL[key]`` (with the text fallback)."""
        selector = SEL[key].format(**fmt)
        text = BUTTON_TEXT.get(key)
        return f"window.__cap.q({json.dumps(selector)}, {json.dumps(text)})"

    def exists(self, key: str, **fmt: str) -> bool:
        return bool(self.js(f"!!{self.el(key, **fmt)}"))

    def count(self, key: str) -> str:
        """JS expression: how many elements match ``SEL[key]``."""
        return f"document.querySelectorAll({json.dumps(SEL[key])}).length"

    def visible(self, key: str, **fmt: str) -> str:
        return f"window.__cap.visible({self.el(key, **fmt)})"

    def click(self, key: str, index: int | None = None, **fmt: str) -> None:
        """Click ``SEL[key]`` (or its ``index``-th match); fail loudly when it is missing."""
        if index is None:
            target = self.el(key, **fmt)
        else:
            target = f"document.querySelectorAll({json.dumps(SEL[key].format(**fmt))})[{index}]"
        if not self.js(
            f"(() => {{ const e = {target}; if (!e) return false; e.click(); return true; }})()"
        ):
            raise RuntimeError(f"missing element SEL[{key!r}] = {SEL[key]!r}")

    def enabled(self, key: str) -> str:
        return f"(() => {{ const e = {self.el(key)}; return !!e && !e.disabled; }})()"

    def set_text(self, key: str, value: str) -> None:
        self.js(
            f"(() => {{ const e = {self.el(key)}; e.value = {json.dumps(value)};"
            " e.dispatchEvent(new Event('input', { bubbles: true })); return true; })()"
        )

    def scroll_y(self) -> float:
        """Scroll position of the scroll container (``#main`` on the home page)."""
        return float(self.js("window.__cap.scroller().scrollTop"))

    def scroll_set(self, y: float) -> None:
        self.js(f"window.__cap.scroller().scrollTo(0, {y:.1f})")

    def bottom_y(self) -> float:
        return float(self.js("window.__cap.maxScroll()"))

    def target_y(self, key: str, offset: float = 16) -> float:
        """Scroll position that puts the element's top ``offset`` px below the top."""
        top = self.js(f"window.__cap.top({self.el(key)})")
        return max(0.0, min(float(top) - offset, self.bottom_y()))

    def clip(
        self, key: str, *, pad: float = 12, max_height: float | None = None
    ) -> dict[str, float]:
        """Page-coordinate clip around an element (optionally capped in height)."""
        rect = self.js(
            f"(() => {{ const r = {self.el(key)}.getBoundingClientRect();"
            " return [r.left + window.scrollX, r.top + window.scrollY, r.width, r.height,"
            " window.__cap.viewBottom() + window.scrollY]; })()"
        )
        x, y, w, h, bottom = (float(v) for v in rect)
        # Inside a scroll container only the visible part is painted: stop at its edge.
        h = min(h, bottom - y - pad)
        height = h + 2 * pad if max_height is None else min(h + 2 * pad, max_height)
        return {
            "x": max(0.0, x - pad),
            "y": max(0.0, y - pad),
            "width": w + 2 * pad,
            "height": height,
        }

    def set_files(self, key: str, files: list[Path]) -> None:
        """Set the files of a file input (fires its change event)."""
        root = self.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        node = self.send("DOM.querySelector", {"nodeId": root, "selector": SEL[key]})["nodeId"]
        self.send("DOM.setFileInputFiles", {"nodeId": node, "files": [str(f) for f in files]})


# --------------------------------------------------------------------------- recording


@dataclass
class Recorder:
    """Collects GIF frames: a PNG plus how long it stays on screen."""

    page: Page
    frames: list[tuple[bytes, int]] = field(default_factory=list)

    def shot(self, duration_ms: int) -> None:
        self.frames.append((self.page.screenshot(), duration_ms))

    def hold(self, ms: int) -> None:
        if self.frames:
            data, duration = self.frames[-1]
            self.frames[-1] = (data, duration + ms)
        else:
            self.shot(ms)

    def scroll_to(self, y: float, *, steps: int = 8, step_ms: int = 60) -> None:
        start = self.page.scroll_y()
        if abs(y - start) < MIN_SCROLL_PX:
            return
        for i in range(1, steps + 1):
            t = i / steps
            eased = t * t * (3 - 2 * t)
            self.page.scroll_set(start + (y - start) * eased)
            self.shot(step_ms)

    def type_text(
        self, key: str, text: str, *, chars_per_frame: int = 5, frame_ms: int = 55
    ) -> None:
        for end in range(chars_per_frame, len(text) + chars_per_frame, chars_per_frame):
            self.page.set_text(key, text[:end])
            self.shot(frame_ms)

    def wait_recording(
        self, expression: str, *, frame_ms: int = 150, timeout_s: float = 30
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.shot(frame_ms)
            if self.page.js(expression):
                return
            time.sleep(max(0.0, frame_ms / 1000 - 0.05))
        raise TimeoutError(f"timed out waiting for {expression}")


def save_gif(frames: list[tuple[bytes, int]], path: Path, *, width: int, colors: int) -> int:
    """Write an optimized looping GIF with one shared palette; return its size."""
    images = [Image.open(io.BytesIO(data)).convert("RGB") for data, _ in frames]
    if images[0].width != width:
        ratio = width / images[0].width
        size = (width, round(images[0].height * ratio))
        images = [im.resize(size, Image.Resampling.LANCZOS) for im in images]
    # One palette from a sample of frames keeps colors stable (no flicker between frames).
    step = max(1, len(images) // 12)
    sample = images[::step]
    sheet = Image.new("RGB", (images[0].width, images[0].height * len(sample)))
    for i, im in enumerate(sample):
        sheet.paste(im, (0, i * im.height))
    palette = sheet.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
    quantized = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
    durations = [max(20, d) for _, d in frames]
    quantized[0].save(
        path,
        save_all=True,
        append_images=quantized[1:],
        duration=durations,
        loop=0,
        optimize=False,
        disposal=1,
    )
    return path.stat().st_size


def save_mp4(frames: list[tuple[bytes, int]], path: Path) -> bool:
    """Write an MP4 with ffmpeg if it is installed; return whether one was written."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        lines = []
        for i, (data, duration) in enumerate(frames):
            frame = Path(tmp) / f"f{i:05d}.png"
            frame.write_bytes(data)
            lines += [f"file '{frame.as_posix()}'", f"duration {duration / 1000:.3f}"]
        lines.append(f"file '{(Path(tmp) / f'f{len(frames) - 1:05d}.png').as_posix()}'")
        listing = Path(tmp) / "frames.txt"
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        subprocess.run(  # noqa: S603 - fixed argument list
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-vf",
                "fps=30,format=yuv420p",
                "-movflags",
                "+faststart",
                str(path),
            ],
            check=True,
        )
    return True


# --------------------------------------------------------------------------- samples


def _pdf_bytes(lines: list[str]) -> bytes:
    """A tiny one-page PDF with the given text lines (Helvetica, no dependencies)."""
    text = ["BT", "/F1 12 Tf", "72 720 Td", "16 TL"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        text.append(f"({escaped}) Tj T*")
    text.append("ET")
    stream = "\n".join(text).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


RT_A = [412, 438, 455, 471, 486, 502, 519, 533, 561, 590, 644, 731]
RT_B = [451, 476, 489, 507, 522, 538, 556, 571, 603, 629, 698, 812]


def make_samples(directory: Path) -> list[Path]:
    """Write a PDF, a Word file and a PNG chart to attach in the screenshots."""
    pdf = directory / "pilot-study.pdf"
    pdf.write_bytes(
        _pdf_bytes(
            [
                "Pilot study: simple reaction times (ms), one mean per participant",
                "Group A (n = 12): " + ", ".join(map(str, RT_A)),
                "Group B (n = 12): " + ", ".join(map(str, RT_B)),
            ]
        )
    )
    from docx import Document  # noqa: PLC0415 - project dependency, only needed here

    doc = Document()
    doc.core_properties.author = "llm-crossjury demo"
    doc.add_heading("Methods (draft)", level=1)
    doc.add_paragraph(
        "Participants pressed a key as soon as a target appeared. We compare mean "
        "reaction times between the two groups."
    )
    docx = directory / "methods-draft.docx"
    doc.save(str(docx))

    png = directory / "rt-histogram.png"
    image = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(image)
    bins = [400 + 50 * i for i in range(10)]
    for series, color, shift in ((RT_A, (70, 110, 200), 0), (RT_B, (220, 120, 60), 14)):
        for i, low in enumerate(bins):
            count = sum(1 for v in series if low <= v < low + 50)
            x = 40 + i * 58 + shift
            draw.rectangle((x, 320 - count * 50, x + 12, 320), fill=color)
    draw.line((30, 320, 620, 320), fill="black", width=2)
    draw.text((40, 330), "Reaction time (ms), 400-900", fill="black")
    image.save(png)
    return [pdf, docx, png]


# --------------------------------------------------------------------------- flows


@dataclass
class Shots:
    """Writes light and dark stills of the current state."""

    page: Page
    out: Path
    only: frozenset[str] | None = None
    written: list[Path] = field(default_factory=list)

    def wanted(self, name: str) -> bool:
        return self.only is None or name in self.only

    def capture(
        self, name: str, key: str, *, max_height: float | None = None, scroll: bool = True
    ) -> None:
        """Element still: scroll it to the top of its container, then clip around it."""
        if not self.wanted(name):
            return
        if scroll:
            self.page.scroll_set(self.page.target_y(key, offset=12))
            time.sleep(0.2)
        for dark in (False, True):
            self.page.theme(dark)
            time.sleep(0.2)
            data = self.page.screenshot(self.page.clip(key, max_height=max_height))
            self._write(name, dark, data)
        self.page.theme(False)

    def capture_viewport(self, name: str) -> None:
        """The whole viewport (sidebar, thread and composer)."""
        if not self.wanted(name):
            return
        for dark in (False, True):
            self.page.theme(dark)
            time.sleep(0.2)
            self._write(name, dark, self.page.screenshot())
        self.page.theme(False)

    def _write(self, name: str, dark: bool, data: bytes) -> None:
        path = self.out / f"{name}-{'dark' if dark else 'light'}.png"
        path.write_bytes(data)
        self.written.append(path)


def seed_history(base: str) -> None:
    """Create the example conversations listed in :data:`HISTORY_TITLES` (demo only)."""
    for title in HISTORY_TITLES:
        request = urllib.request.Request(  # noqa: S310 - loopback demo server
            base + "/api/sessions",
            data=json.dumps({"title": title}).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            response.read()
        time.sleep(0.05)  # distinct updated_at, so the order is stable


def open_home(page: Page, base: str, banner: bool) -> None:
    """A fresh conversation on the home page, with the jury panel loaded."""
    page.theme(False)
    page.goto(base + "/", f"!!{page.el('use', provider='xai')}", show_banner=banner)
    page.js("document.querySelector('#new-chat') && document.querySelector('#new-chat').click()")
    page.wait(f"!!{page.el('use', provider='xai')}")
    page.scroll_set(0)


def select_models(page: Page, rec: Recorder | None, providers: tuple[str, ...] = PROVIDERS) -> None:
    for provider in providers:
        if not page.js(f"{page.el('use', provider=provider)}.checked"):
            page.click("use", provider=provider)
        if rec is not None:
            rec.shot(260)


def estimate(page: Page) -> None:
    page.click("estimate_btn")
    page.wait(page.visible("estimate"))
    page.wait(page.enabled("run_btn"))


def answers_done(page: Page, n: int) -> str:
    """JS: the jury turn shows ``n`` answer bodies and nothing is pending."""
    return f"{page.count('answer_body')} >= {n} && {page.count('pending')} === 0"


def synthesis_ready(page: Page) -> None:
    """Bring the synthesis panel into view (it estimates lazily) and wait for its button."""
    page.wait(f"!!{page.el('synthesis')}", timeout_s=30)
    page.scroll_set(page.target_y("synthesis", offset=8))
    page.wait(page.enabled("syn_btn"), timeout_s=30)


def run_synthesis(page: Page, rec: Recorder | None) -> None:
    """Click AI synthesize; continue past the optional skip-review decision; wait."""
    page.click("syn_btn")
    done = f"window.__cap.visible({page.el('syn_result')})"
    decision = f"window.__cap.visible({page.el('decision')})"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if rec is not None:
            rec.shot(160)
        else:
            time.sleep(0.15)
        if page.js(done):
            return
        if page.js(decision):
            if rec is not None:
                rec.hold(900)
            page.click("review_btn")
    raise TimeoutError(f"timed out waiting for {done}")


def ask_the_jury(page: Page, rec: Recorder | None) -> None:
    """Type the demo question, pick four models, estimate, ask, and wait for the answers."""
    from llm_crossjury.demo import DEMO_QUESTION  # noqa: PLC0415

    if rec is None:
        page.set_text("task", DEMO_QUESTION)
    else:
        rec.type_text("task", DEMO_QUESTION, chars_per_frame=7)
        rec.hold(400)
    select_models(page, rec)
    estimate(page)
    if rec is not None:
        page.scroll_set(page.bottom_y())
        rec.shot(1600)
    page.click("run_btn")
    if rec is None:
        page.wait(answers_done(page, len(PROVIDERS)), timeout_s=60)
    else:
        rec.wait_recording(answers_done(page, len(PROVIDERS)), frame_ms=180, timeout_s=60)


def continue_chat(page: Page, rec: Recorder | None) -> None:
    """Click "Continue with" on one answer, send the demo follow-up, wait for the reply."""
    from llm_crossjury.demo.script import CHAT_QUESTION  # noqa: PLC0415

    page.click("continue_btn", index=CONTINUE_INDEX)
    page.wait(f"window.__cap.visible({page.el('send_btn')})")
    if rec is None:
        page.set_text("task", CHAT_QUESTION)
    else:
        rec.shot(700)
        rec.type_text("task", CHAT_QUESTION, chars_per_frame=6)
        rec.hold(500)
    page.wait(page.enabled("send_btn"))
    page.click("send_btn")
    busy = "document.querySelector('.turn-chat [aria-busy=\"true\"]')"
    reply = f"{page.count('chat_reply')} > 0 && !{busy}"
    if rec is None:
        page.wait(reply, timeout_s=30)
    else:
        rec.wait_recording(reply, frame_ms=180, timeout_s=30)
    time.sleep(0.3)
    page.scroll_set(page.bottom_y())


def record_flow(page: Page, base: str, rec: Recorder, banner: bool) -> None:
    """The hero: one full loop, recorded frame by frame."""
    open_home(page, base, banner)
    rec.shot(900)
    ask_the_jury(page, rec)
    rec.scroll_to(page.target_y("jury_card", offset=8), steps=6)
    rec.hold(2600)
    synthesis_ready(page)
    rec.shot(900)
    run_synthesis(page, rec)
    rec.scroll_to(page.target_y("final", offset=8), steps=8)
    rec.hold(2800)
    start, end = page.scroll_y(), page.target_y("additional", offset=8)
    rec.scroll_to(end, steps=max(6, int((end - start) / 80)), step_ms=70)
    rec.hold(3400)
    rec.scroll_to(page.target_y("jury_card", offset=8), steps=8)
    rec.hold(600)
    continue_chat(page, rec)
    rec.scroll_to(page.bottom_y(), steps=6)
    rec.hold(3600)


def conversation_stills(page: Page, base: str, shots: Shots, banner: bool) -> None:
    """Answers, synthesis, additional list and the chat view, from one conversation."""
    open_home(page, base, banner)
    ask_the_jury(page, None)
    shots.capture("answers", "jury_turn", max_height=820)
    synthesis_ready(page)
    run_synthesis(page, None)
    shots.capture("synthesis", "final", max_height=900)
    shots.capture("additional-list", "additional")
    continue_chat(page, None)
    page.resize(page.width, 860)
    time.sleep(0.3)
    page.scroll_set(page.bottom_y())
    shots.capture_viewport("conversation")
    page.resize(page.width, STILL_HEIGHT)


def models_still(page: Page, base: str, shots: Shots, banner: bool) -> None:
    """The jury panel: two models on a subscription, the cost estimate and the consent."""
    if not shots.wanted("models"):
        return
    from llm_crossjury.demo import DEMO_QUESTION  # noqa: PLC0415

    open_home(page, base, banner)
    page.set_text("task", DEMO_QUESTION)
    for provider in ("anthropic", "openai"):
        page.click("backend", provider=provider, backend="cli")
    page.wait(
        f"!!{page.el('use', provider='openai')} && !{page.el('use', provider='openai')}.disabled"
    )
    select_models(page, None, ("anthropic", "openai", "gemini"))
    page.js("document.querySelectorAll('#providers details[open]').forEach((d) => d.open = false)")
    page.wait(page.visible("consent"))
    if not page.js(f"{page.el('consent')}.checked"):
        page.click("consent")  # subscription members need the unknown-cost consent
    estimate(page)
    shots.capture("models", "jury_panel", scroll=False)


def attachments_still(
    page: Page, base: str, shots: Shots, banner: bool, samples: list[Path]
) -> None:
    """The composer with a PDF, a Word file and a PNG attached."""
    if not shots.wanted("attachments"):
        return
    from llm_crossjury.demo import DEMO_QUESTION  # noqa: PLC0415

    page.resize(page.width, 860)
    open_home(page, base, banner)
    page.set_text("task", DEMO_QUESTION)
    select_models(page, None)
    page.set_files("attach_input", samples)
    page.wait(
        f"document.querySelectorAll({json.dumps(SEL['attach_list'] + ' li')}).length >= "
        f"{len(samples)} && !document.querySelector('#attach-list .spinner')"
    )
    time.sleep(0.3)
    shots.capture("attachments", "dock", scroll=False)
    page.resize(page.width, STILL_HEIGHT)


def wizard_still(page: Page, base: str, shots: Shots, banner: bool) -> None:
    """The setup wizard's Subscriptions step, with one row's commands open."""
    if not shots.wanted("wizard"):
        return
    page.theme(False)
    page.goto(
        base + "/settings?onboarding=1",
        f"window.__cap.visible({page.el('wizard')})",
        show_banner=banner,
    )
    page.click("wizard_next")
    page.wait(f"document.querySelectorAll({json.dumps(SEL['cli_rows'] + ' > *')}).length > 0")
    time.sleep(1.0)  # the CLI check settles
    first_row = json.dumps(SEL["cli_rows"] + " details")
    page.js(f"document.querySelector({first_row})?.setAttribute('open', '')")
    page.scroll_set(0)
    shots.capture("wizard", "wizard", scroll=False)


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    """Entry point; see the module docstring."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--browser", help="Edge or Chrome executable (default: auto-detect).")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory.")
    parser.add_argument("--width", type=int, default=1440, help="Viewport width (px).")
    parser.add_argument("--height", type=int, default=860, help="Viewport height (px).")
    parser.add_argument(
        "--gif-width", type=int, default=1080, help="Width of hero.gif (default: 1080)."
    )
    parser.add_argument("--colors", type=int, default=192, help="GIF palette size.")
    parser.add_argument("--no-gif", action="store_true", help="Skip hero.gif and hero.mp4.")
    parser.add_argument("--no-stills", action="store_true", help="Skip the screenshots.")
    parser.add_argument(
        "--only", action="append", choices=STILLS, help="Only these stills (repeatable)."
    )
    parser.add_argument("--show-banner", action="store_true", help="Keep the demo labels.")
    args = parser.parse_args(argv)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    browser = find_browser(args.browser)
    banner: bool = args.show_banner
    with tempfile.TemporaryDirectory(prefix="crossjury-samples-") as tmp, demo_server() as base:
        samples = make_samples(Path(tmp))
        seed_history(base)
        with (
            headless_browser(browser, args.width, args.height) as ws_url,
            connect(ws_url, max_size=None, open_timeout=20) as ws,
        ):
            page = Page(ws, args.width, args.height)
            page.setup()
            page.theme(False)
            if not args.no_gif:
                rec = Recorder(page)
                # Stills are taken in a second pass: a beyond-viewport capture during the
                # recording can leave one glitched frame.
                record_flow(page, base, rec, banner)
                size = save_gif(
                    rec.frames, out / "hero.gif", width=args.gif_width, colors=args.colors
                )
                seconds = sum(d for _, d in rec.frames) / 1000
                print(f"hero.gif: {len(rec.frames)} frames, {seconds:.1f} s, {size / 1e6:.2f} MB")
                if save_mp4(rec.frames, out / "hero.mp4"):
                    print("hero.mp4 written")
                else:
                    print("ffmpeg not found: hero.mp4 skipped")
            if not args.no_stills:
                shots = Shots(page, out, frozenset(args.only) if args.only else None)
                page.resize(args.width, STILL_HEIGHT)
                if any(
                    shots.wanted(n)
                    for n in ("answers", "synthesis", "additional-list", "conversation")
                ):
                    conversation_stills(page, base, shots, banner)
                models_still(page, base, shots, banner)
                attachments_still(page, base, shots, banner, samples)
                wizard_still(page, base, shots, banner)
                for path in shots.written:
                    print(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
