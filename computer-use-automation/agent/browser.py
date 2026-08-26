"""
Thin wrapper around Playwright that gives both the agent loop and the
replay engine the same two primitives:

  - `observe()`  -> a compact, JSON-serializable description of the current
                    page: interactive elements (role, accessible name, a
                    short id we invent for referencing them this turn) and
                    any alert/status banners. This is deliberately NOT a raw
                    screenshot or raw DOM dump -- it's the "accessibility
                    tree" framing that still works when there's no clean DOM.

  - `resolve(locator)` -> a Playwright Locator object, found by trying the
                    primary strategy then each fallback in order. Every
                    action goes through this, so replay and discovery share
                    one robustness story.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

from .schema import Locator


ROLE_TAGS = {
    "button": "button, input[type=submit], input[type=button]",
    "textbox": "input[type=text], input[type=password], input[type=number], textarea",
    "link": "a",
    "combobox": "select",
}


@dataclass
class ElementRef:
    ref_id: str
    role: str
    name: str
    locator: "Locator"


class BrowserSession:
    def __init__(self, headless: bool = True, slow_mo: int = 0):
        self._pw = sync_playwright().start()
        chromium_path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
        launch_kwargs = dict(headless=headless, slow_mo=slow_mo, args=["--no-sandbox"])
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path
        self.browser = self._pw.chromium.launch(**launch_kwargs)
        self.context = self.browser.new_context()
        self.page: Page = self.context.new_page()

    def close(self):
        try:
            self.context.close()
            self.browser.close()
        finally:
            self._pw.stop()

    def cdp_endpoint(self) -> Optional[str]:
        """Documents the seam for a real deployment where an operator
        process would attach to this same browser over CDP instead of the
        in-process handoff this demo uses (see escalation.py)."""
        return None

    # -- observation ---------------------------------------------------------
    def observe(self, max_elements: int = 40) -> dict:
        page = self.page
        elements = []
        idx = 0
        for role, css in ROLE_TAGS.items():
            for el in page.query_selector_all(css):
                try:
                    if not el.is_visible():
                        continue
                    name = _accessible_name(el, role)
                    ref_id = f"e{idx}"
                    idx += 1
                    entry = {"ref_id": ref_id, "role": role, "name": name}
                    if role in ("textbox", "combobox"):
                        try:
                            entry["value"] = el.input_value()
                        except Exception:
                            entry["value"] = ""
                    elements.append(entry)
                    if idx >= max_elements:
                        break
                except Exception:
                    continue

        banners = []
        for el in page.query_selector_all('[role=alert], [role=status], .error, .notice, .success'):
            try:
                if el.is_visible():
                    txt = el.inner_text().strip()
                    if txt:
                        banners.append(txt)
            except Exception:
                continue

        return {
            "url": page.url,
            "title": page.title(),
            "elements": elements,
            "banners": banners,
        }

    # -- locator resolution ---------------------------------------------------
    def resolve(self, locator: Locator, timeout_ms: int = 5000):
        """Try primary strategy, then fallbacks in order. Returns a Playwright
        Locator (already asserted to have >=1 match) or raises TimeoutError."""
        candidates = [locator] + list(locator.fallbacks)
        last_err = None
        for cand in candidates:
            try:
                pw_locator = self._to_playwright_locator(cand)
                pw_locator.first.wait_for(state="visible", timeout=timeout_ms)
                return pw_locator.first
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
        raise PWTimeout(f"No locator strategy resolved (tried {len(candidates)}): {last_err}")

    def _to_playwright_locator(self, loc: Locator):
        page = self.page
        if loc.strategy == "role":
            role, name = loc.value.split("::", 1)
            return page.get_by_role(role, name=name, exact=False)
        if loc.strategy == "text":
            return page.get_by_text(loc.value, exact=False)
        if loc.strategy == "css":
            return page.locator(loc.value)
        if loc.strategy == "xpath":
            return page.locator(f"xpath={loc.value}")
        raise ValueError(f"Unknown locator strategy: {loc.strategy}")

    # -- actions ---------------------------------------------------------------
    def goto(self, url: str, timeout_ms: int = 8000):
        self.page.goto(url, timeout=timeout_ms)

    def click(self, locator: Locator, timeout_ms: int = 5000):
        el = self.resolve(locator, timeout_ms)
        el.click()

    def fill(self, locator: Locator, text: str, timeout_ms: int = 5000):
        el = self.resolve(locator, timeout_ms)
        el.fill(text)

    def select(self, locator: Locator, value: str, timeout_ms: int = 5000):
        el = self.resolve(locator, timeout_ms)
        el.select_option(label=value)

    def extract_text(self, locator: Locator, timeout_ms: int = 5000) -> str:
        el = self.resolve(locator, timeout_ms)
        return el.inner_text().strip()

    def wait_for(self, locator: Locator, timeout_ms: int = 5000) -> bool:
        try:
            self.resolve(locator, timeout_ms)
            return True
        except Exception:
            return False

    def screenshot(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.page.screenshot(path=path)


def _accessible_name(el, role: str) -> str:
    try:
        aria = el.get_attribute("aria-label")
        if aria:
            return aria.strip()
        if role in ("textbox", "combobox"):
            # legacy forms rarely have <label for=...>; fall back to nearest
            # preceding table cell text, which is how this app lays out labels.
            val = el.evaluate(
                "el => { const row = el.closest('tr'); "
                "if (row) { const c = row.querySelector('td'); if (c) return c.innerText; } "
                "return ''; }"
            )
            return (val or "").strip()
        text = el.inner_text() if role != "textbox" else ""
        if not text:
            text = el.get_attribute("value") or ""
        return text.strip()
    except Exception:
        return ""


def build_locator_for_ref(role: str, name: str) -> Locator:
    """Given the (role, name) chosen from an `observe()` snapshot, build a
    Locator with sensible fallbacks: role+name first (survives markup/layout
    changes), then plain text match as a fallback."""
    primary = Locator(strategy="role", value=f"{role}::{name}")
    fallbacks = []
    if name:
        fallbacks.append(Locator(strategy="text", value=name))
    return Locator(strategy=primary.strategy, value=primary.value, fallbacks=fallbacks)