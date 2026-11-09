"""Capture the README media from demo mode: the hero GIF and light/dark screenshots.

Manual script. It costs nothing and makes no network call beyond loopback: it starts
``llm-crossjury ui --demo`` on a free port, drives the UI in headless Edge or Chrome
over the DevTools protocol, and writes:

* ``docs/assets/hero.gif``: the full flow (ask, pick models, estimate, run, answers,
  AI synthesize, final answer and additional list), about 25 s, looping;
* ``docs/assets/<name>-light.png`` and ``<name>-dark.png`` for ``compare``,
  ``synthesis``, ``additional-list``, ``estimate``, ``attachments``, ``subscription``
  and ``settings``;
* ``docs/assets/hero.mp4`` when ``ffmpeg`` is on PATH (skipped otherwise).

Usage::

    uv run python scripts/capture_readme_media.py            # everything
    uv run python scripts/capture_readme_media.py --no-gif   # screenshots only
    uv run python scripts/capture_readme_media.py --browser "C:/path/to/chrome.exe"

Dependencies: only what the project already installs (``websockets`` for the DevTools
connection, Pillow for the GIF, ``python-docx`` for the sample Word file). The sample
attachments are generated at run time.

What the script expects from the UI (update :data:`SEL` after a redesign):

* the task textarea ``#task``;
* one checkbox per provider ``#use-<provider>`` and, for subscription-capable
  providers, a radio ``input[name="backend-<provider>"][value="cli"]``;
* the buttons ``#estimate-btn``, ``#run-btn`` and ``#syn-btn`` (falls back to a button
  whose text starts with "Estimate", "Run" or "AI synthesize");
* the regions ``#estimate``, ``#results-section``, ``#synthesis-section``,
  ``#syn-result`` (with an ``.additional`` block), ``#providers`` and ``#task-panel``;
* the file input ``#attach-input`` and the list ``#attach-list``;
* dark mode via ``prefers-color-scheme`` (the script emulates it);
* the demo banner ``#demo-banner``, which the script hides (``--show-banner`` keeps it).

Determinism: the demo server starts with an empty temporary data dir, the providers
are deterministic, the label shuffle seed is pinned (the script injects ``seed`` into
synthesis requests), and frame timings are scripted. Only the measured latency shown
per answer can differ by a tenth of a second between runs.
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
MIN_SCROLL_PX = 2
SEED = 20261108
"""Label shuffle seed injected into synthesis requests, so labels are stable."""

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
    "task": "#task",
    "use": "#use-{provider}",
    "backend": 'input[name="backend-{provider}"][value="{backend}"]',
    "estimate_btn": "#estimate-btn",
    "run_btn": "#run-btn",
    "syn_btn": "#syn-btn",
    "estimate": "#estimate",
    "actions": ".actions-panel",
    "results": "#results-section",
    "synthesis": "#synthesis-section",
    "syn_result": "#syn-result",
    "additional": "#syn-result .additional",
    "providers": "#providers",
    "task_panel": "#task-panel",
    "attach_input": "#attach-input",
    "attach_list": "#attach-list",
    "banner": "#demo-banner",
}
"""CSS selectors the script relies on (see the module docstring)."""

BUTTON_TEXT = {"estimate_btn": "Estimate", "run_btn": "Run", "syn_btn": "AI synthesize"}
"""Fallback: a button whose text starts with this, if the selector finds nothing."""

PAGE_HELPERS = """
window.__cap = {
  q(sel, text) {
    let el = document.querySelector(sel);
    if (!el && text) {
      const want = text.toLowerCase();
      el = [...document.querySelectorAll('button, [role="button"], a')]
        .find((b) => b.textContent.trim().toLowerCase().startsWith(want)) || null;
    }
    return el;
  },
  visible(el) {
    if (!el) return false;
    if (el.closest('[hidden]')) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
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


MASK_PATHS = r"""
(() => {
  // The demo key store lives in a temp dir whose path names the local user: mask it.
  const re = /(?:[A-Za-z]:\\|\/)\S*?keys\.env/g;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (re.test(n.nodeValue)) {
      n.nodeValue = n.nodeValue.replace(re, "<user config dir>/llm-crossjury/keys.env");
    }
    re.lastIndex = 0;
  }
  return true;
})()
"""


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

    def goto(self, url: str, ready: str, *, show_banner: bool) -> None:
        """Navigate and wait until ``ready`` is truthy; hide the demo banner."""
        self.send("Page.navigate", {"url": url})
        self.wait("document.readyState === 'complete' && !!window.__cap")
        self.wait(ready)
        if not show_banner:
            self.js(f"document.querySelector({json.dumps(SEL['banner'])})?.remove()")
        self.js("document.fonts ? document.fonts.ready.then(() => true) : true", await_promise=True)

    def theme(self, dark: bool) -> None:
        """Emulate ``prefers-color-scheme``."""
        value = "dark" if dark else "light"
        self.send(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-color-scheme", "value": value}]},
        )

    def screenshot(self, clip: dict[str, float] | None = None) -> bytes:
        """PNG of the viewport, or of ``clip`` (page coordinates)."""
        params: dict[str, Any] = {"format": "png", "fromSurface": True}
        if clip is not None:
            params["clip"] = {**clip, "scale": 1}
            params["captureBeyondViewport"] = True
        return base64.b64decode(self.send("Page.captureScreenshot", params)["data"])

    # -- element helpers

    def el(self, key: str, **fmt: str) -> str:
        """JS expression for the element ``SEL[key]`` (with the text fallback)."""
        selector = SEL[key].format(**fmt)
        text = BUTTON_TEXT.get(key)
        return f"window.__cap.q({json.dumps(selector)}, {json.dumps(text)})"

    def exists(self, key: str, **fmt: str) -> bool:
        return bool(self.js(f"!!{self.el(key, **fmt)}"))

    def visible(self, key: str, **fmt: str) -> str:
        return f"window.__cap.visible({self.el(key, **fmt)})"

    def click(self, key: str, **fmt: str) -> None:
        self.js(f"(() => {{ const e = {self.el(key, **fmt)}; e.click(); return true; }})()")

    def enabled(self, key: str) -> str:
        return f"(() => {{ const e = {self.el(key)}; return !!e && !e.disabled; }})()"

    def set_text(self, key: str, value: str) -> None:
        self.js(
            f"(() => {{ const e = {self.el(key)}; e.value = {json.dumps(value)};"
            " e.dispatchEvent(new Event('input', { bubbles: true })); return true; })()"
        )

    def scroll_y(self) -> float:
        return float(self.js("window.scrollY"))

    def target_y(self, key: str, offset: float = 16) -> float:
        """Scroll position that puts the element's top ``offset`` px below the top."""
        top = self.js(f"{self.el(key)}.getBoundingClientRect().top + window.scrollY")
        limit = self.js("document.documentElement.scrollHeight - window.innerHeight")
        return max(0.0, min(float(top) - offset, float(limit)))

    def clip(
        self, key: str, *, pad: float = 12, max_height: float | None = None
    ) -> dict[str, float]:
        """Page-coordinate clip around an element (optionally capped in height)."""
        rect = self.js(
            f"(() => {{ const r = {self.el(key)}.getBoundingClientRect();"
            " return [r.left + window.scrollX, r.top + window.scrollY, r.width, r.height]; })()"
        )
        x, y, w, h = (float(v) for v in rect)
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
            self.page.js(f"window.scrollTo(0, {start + (y - start) * eased:.1f})")
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


_FIXED_CHROME_JS = (
    "document.querySelectorAll('footer.strip').forEach("
    "(e) => {{ e.style.visibility = '{value}'; }})"
)
"""Hide (``hidden``) or restore (``""``) the fixed bottom strip of the compare page."""


@dataclass
class Shots:
    """Writes light and dark stills of the current state."""

    page: Page
    out: Path
    written: list[Path] = field(default_factory=list)

    def capture(self, name: str, key: str, *, max_height: float | None = None) -> None:
        # Fixed-position chrome (the bottom cost strip) would be painted over the middle
        # of an element clip; hide it for element stills (visibility keeps the layout).
        self.page.js(_FIXED_CHROME_JS.format(value="hidden"))
        try:
            for dark in (False, True):
                self.page.theme(dark)
                time.sleep(0.15)
                data = self.page.screenshot(self.page.clip(key, max_height=max_height))
                self.page.screenshot()  # settle the viewport after a beyond-viewport capture
                path = self.out / f"{name}-{'dark' if dark else 'light'}.png"
                path.write_bytes(data)
                self.written.append(path)
        finally:
            self.page.js(_FIXED_CHROME_JS.format(value=""))
            self.page.theme(False)

    def capture_viewport(self, name: str) -> None:
        for dark in (False, True):
            self.page.theme(dark)
            time.sleep(0.15)
            height = self.page.js("document.documentElement.scrollHeight")
            clip = {"x": 0, "y": 0, "width": self.page.width, "height": float(height)}
            path = self.out / f"{name}-{'dark' if dark else 'light'}.png"
            path.write_bytes(self.page.screenshot(clip))
            self.written.append(path)
        self.page.theme(False)


def select_models(page: Page, rec: Recorder | None, providers: tuple[str, ...]) -> None:
    for provider in providers:
        if not page.js(f"{page.el('use', provider=provider)}.checked"):
            page.click("use", provider=provider)
        if rec is not None:
            rec.shot(320)


RESULTS_DONE = "!document.querySelector('#results .pending, #results .spinner')"


def _open_compare(page: Page, base: str, banner: bool) -> None:
    page.theme(False)
    page.goto(base + "/", f"!!{page.el('use', provider='xai')}", show_banner=banner)
    page.js("window.scrollTo(0, 0)")


def _estimate(page: Page) -> None:
    page.click("estimate_btn")
    page.wait(page.visible("estimate"))
    page.wait(page.enabled("run_btn"))


def record_flow(page: Page, base: str, rec: Recorder, banner: bool) -> None:
    """The hero flow, recorded frame by frame."""
    from llm_crossjury.demo import DEMO_QUESTION  # noqa: PLC0415

    _open_compare(page, base, banner)
    rec.shot(900)
    rec.type_text("task", DEMO_QUESTION)
    rec.hold(500)
    select_models(page, rec, PROVIDERS)
    rec.hold(300)
    _estimate(page)
    rec.shot(300)
    rec.scroll_to(page.target_y("actions", offset=300))
    rec.hold(1500)
    page.click("run_btn")
    rec.scroll_to(page.target_y("results", offset=8), steps=6)
    rec.wait_recording(f"window.__cap.visible({page.el('results')}) && {RESULTS_DONE}")
    rec.hold(1800)
    page.wait(page.visible("synthesis"))
    rec.scroll_to(page.target_y("results", offset=-260), steps=8)
    rec.hold(900)
    rec.scroll_to(page.target_y("synthesis", offset=8))
    page.wait(page.enabled("syn_btn"))
    rec.shot(1000)
    page.click("syn_btn")
    rec.wait_recording(f"window.__cap.visible({page.el('syn_result')})", frame_ms=160)
    rec.hold(400)
    rec.scroll_to(page.target_y("syn_result", offset=8))
    rec.hold(2600)
    start, end = page.scroll_y(), page.target_y("additional", offset=8)
    rec.scroll_to(end, steps=max(6, int((end - start) / 70)), step_ms=70)
    rec.hold(3200)
    bottom = float(page.js("document.documentElement.scrollHeight - window.innerHeight"))
    rec.scroll_to(bottom, steps=10, step_ms=70)
    rec.hold(2600)


def stills_flow(page: Page, base: str, shots: Shots, banner: bool) -> None:
    """The same flow without recording: estimate, compare and synthesis stills."""
    from llm_crossjury.demo import DEMO_QUESTION  # noqa: PLC0415

    _open_compare(page, base, banner)
    page.set_text("task", DEMO_QUESTION)
    select_models(page, None, PROVIDERS)
    _estimate(page)
    shots.capture("estimate", "actions")
    page.click("run_btn")
    page.wait(RESULTS_DONE)
    page.wait(page.visible("synthesis"))
    shots.capture("compare", "results", max_height=760)
    page.wait(page.enabled("syn_btn"))
    page.click("syn_btn")
    page.wait(f"window.__cap.visible({page.el('syn_result')})", timeout_s=40)
    shots.capture("synthesis", "syn_result", max_height=900)
    shots.capture("additional-list", "additional")


def subscription_flow(page: Page, base: str, shots: Shots, banner: bool) -> None:
    page.theme(False)
    page.goto(base + "/", f"!!{page.el('use', provider='xai')}", show_banner=banner)
    for provider in ("anthropic", "openai"):
        page.click("backend", provider=provider, backend="cli")
    page.wait(
        f"!!{page.el('use', provider='openai')} && !{page.el('use', provider='openai')}.disabled"
    )
    select_models(page, None, ("anthropic", "openai", "gemini"))
    page.js("document.querySelectorAll('#providers details[open]').forEach((d) => d.open = false)")
    shots.capture("subscription", "providers")


def attachments_flow(
    page: Page, base: str, shots: Shots, banner: bool, samples: list[Path]
) -> None:
    from llm_crossjury.demo import DEMO_QUESTION  # noqa: PLC0415

    page.theme(False)
    page.goto(base + "/", f"!!{page.el('use', provider='xai')}", show_banner=banner)
    page.set_text("task", DEMO_QUESTION)
    select_models(page, None, PROVIDERS)
    page.set_files("attach_input", samples)
    page.wait(
        f"document.querySelectorAll({json.dumps(SEL['attach_list'] + ' li')}).length >= "
        f"{len(samples)} && !document.querySelector('#attach-list .spinner')"
    )
    shots.capture("attachments", "task_panel")


def settings_flow(page: Page, base: str, shots: Shots, banner: bool) -> None:
    page.theme(False)
    page.goto(
        base + "/settings",
        "document.querySelectorAll('input, button').length > 2",
        show_banner=banner,
    )
    time.sleep(0.4)
    page.js(MASK_PATHS)
    shots.capture_viewport("settings")


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    """Entry point; see the module docstring."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--browser", help="Edge or Chrome executable (default: auto-detect).")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory.")
    parser.add_argument("--width", type=int, default=1200, help="Viewport width (px).")
    parser.add_argument("--height", type=int, default=760, help="Viewport height (px).")
    parser.add_argument(
        "--gif-width", type=int, default=None, help="Width of hero.gif (default: viewport)."
    )
    parser.add_argument("--colors", type=int, default=256, help="GIF palette size.")
    parser.add_argument("--no-gif", action="store_true", help="Skip hero.gif and hero.mp4.")
    parser.add_argument("--no-stills", action="store_true", help="Skip the screenshots.")
    parser.add_argument("--show-banner", action="store_true", help="Keep the demo banner.")
    args = parser.parse_args(argv)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    browser = find_browser(args.browser)
    with tempfile.TemporaryDirectory(prefix="crossjury-samples-") as tmp, demo_server() as base:
        samples = make_samples(Path(tmp))
        with (
            headless_browser(browser, args.width, args.height) as ws_url,
            connect(ws_url, max_size=None, open_timeout=20) as ws,
        ):
            page = Page(ws, args.width, args.height)
            page.setup()
            shots = Shots(page, out)
            if not args.no_gif:
                rec = Recorder(page)
                # Stills are taken in a second pass: a beyond-viewport capture during the
                # recording can leave one glitched frame.
                record_flow(page, base, rec, args.show_banner)
                size = save_gif(
                    rec.frames,
                    out / "hero.gif",
                    width=args.gif_width or args.width,
                    colors=args.colors,
                )
                seconds = sum(d for _, d in rec.frames) / 1000
                print(f"hero.gif: {len(rec.frames)} frames, {seconds:.1f} s, {size / 1e6:.2f} MB")
                if save_mp4(rec.frames, out / "hero.mp4"):
                    print("hero.mp4 written")
                else:
                    print("ffmpeg not found: hero.mp4 skipped")
            if not args.no_stills:
                stills_flow(page, base, shots, args.show_banner)
                subscription_flow(page, base, shots, args.show_banner)
                attachments_flow(page, base, shots, args.show_banner, samples)
                settings_flow(page, base, shots, args.show_banner)
                for path in shots.written:
                    print(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
