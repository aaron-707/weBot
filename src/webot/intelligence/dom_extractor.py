from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from playwright.async_api import Page


ElementType = Literal["button", "input", "link", "form", "text"]


class DomElement(TypedDict, total=False):
    type: ElementType
    text: str
    selector: str
    visible: bool
    attributes: dict[str, str]


class DomSnapshot(TypedDict):
    buttons: list[DomElement]
    inputs: list[DomElement]
    links: list[DomElement]
    forms: list[DomElement]
    visible_text: list[DomElement]


@dataclass(slots=True)
class DomExtractor:
    """Extracts structured DOM data from a Playwright page."""

    text_min_length: int = 1
    dom_ready_timeout_ms: int = 10_000

    async def extract_buttons(self, page: Page) -> list[DomElement]:
        selectors = self.button_selectors()
        return await self._extract_with_fallback(page, selectors, "button")

    async def extract_inputs(self, page: Page) -> list[DomElement]:
        return await self._extract_with_fallback(page, ["input, textarea, select"], "input")

    async def extract_links(self, page: Page) -> list[DomElement]:
        return await self._extract_with_fallback(page, ["a[href]"], "link")

    async def extract_forms(self, page: Page) -> list[DomElement]:
        return await self._extract_with_fallback(page, ["form"], "form")

    async def extract_visible_text(self, page: Page) -> list[DomElement]:
        return await self._extract_with_fallback(
            page,
            ["h1, h2, h3, h4, h5, h6, p, span, li, label, div"],
            "text",
            text_only=True,
        )

    async def extract_all(self, page: Page) -> DomSnapshot:
        return {
            "buttons": await self.extract_buttons(page),
            "inputs": await self.extract_inputs(page),
            "links": await self.extract_links(page),
            "forms": await self.extract_forms(page),
            "visible_text": await self.extract_visible_text(page),
        }

    async def _extract_with_fallback(
        self,
        page: Page,
        selectors: list[str],
        element_type: ElementType,
        *,
        text_only: bool = False,
    ) -> list[DomElement]:
        await self._wait_for_dom_ready(page)

        merged: list[DomElement] = []
        for selector in selectors:
            raw = await self._extract_raw(page, selector, element_type, text_only=text_only)
            normalized = self._normalize_result(raw, element_type)
            merged.extend(normalized)

            # Fallback strategy: stop early once we have enough results.
            if merged:
                break

        return self._dedupe_elements(merged)

    async def _wait_for_dom_ready(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=self.dom_ready_timeout_ms)
        except Exception:
            # Safe fallback when page load state is already advanced/ambiguous.
            return

    async def _extract_raw(
        self,
        page: Page,
        selector: str,
        element_type: ElementType,
        *,
        text_only: bool = False,
    ) -> Any:
        js = """
        (config) => {
            const { selector, elementType, textOnly, textMinLength } = config;
            const nodes = Array.from(document.querySelectorAll(selector));

            const isVisible = (el) => {
                try {
                    if (!el || !el.isConnected) return false;
                    if (el.hidden || el.getAttribute('aria-hidden') === 'true') return false;

                    const style = window.getComputedStyle(el);
                    if (!style) return false;
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    if (Number(style.opacity || '1') <= 0.01) return false;
                    if (style.pointerEvents === 'none') return false;

                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) return false;

                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    const vWidth = window.innerWidth || document.documentElement.clientWidth;
                    const vHeight = window.innerHeight || document.documentElement.clientHeight;

                    if (cx >= 0 && cx <= vWidth && cy >= 0 && cy <= vHeight) {
                        const topEl = document.elementFromPoint(cx, cy);
                        if (!topEl) return false;
                        return topEl === el || el.contains(topEl) || topEl.contains(el);
                    }

                    return true;
                } catch {
                    return false;
                }
            };

            const cssEscape = (value) => {
                if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
                    return CSS.escape(value);
                }
                return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
            };

            const quoted = (value) => String(value).replace(/"/g, '\\"');

            const buildSelector = (el) => {
                try {
                    const id = el.getAttribute('id');
                    if (id) return `#${cssEscape(id)}`;

                    const testId = el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-qa');
                    if (testId) return `[data-testid=\"${quoted(testId)}\"]`;

                    const name = el.getAttribute('name');
                    if (name) {
                        return `${el.tagName.toLowerCase()}[name="${quoted(name)}"]`;
                    }

                    const val = el.getAttribute('data-value') || el.getAttribute('value');
                    if (val) {
                        const valSelector = `${el.tagName.toLowerCase()}[data-value="${quoted(val)}"]`;
                        try {
                            if (document.querySelectorAll(valSelector).length === 1) return valSelector;
                        } catch {}
                    }

                    const ariaLabel = el.getAttribute('aria-label');
                    if (ariaLabel) {
                        const ariaSelector = `${el.tagName.toLowerCase()}[aria-label="${quoted(ariaLabel)}"]`;
                        try {
                            if (document.querySelectorAll(ariaSelector).length === 1) return ariaSelector;
                        } catch {}
                    }

                    const role = el.getAttribute('role');
                    if (role) {
                        const roleSelector = `${el.tagName.toLowerCase()}[role="${quoted(role)}"]`;
                        if (document.querySelectorAll(roleSelector).length === 1) return roleSelector;

                        const text = (el.innerText || el.textContent || '').trim();
                        if (text && text.length <= 40 && !text.includes('\\n')) {
                            const sameRoleNodes = Array.from(document.querySelectorAll(roleSelector));
                            const matchingTextNodes = sameRoleNodes.filter(n => (n.innerText || n.textContent || '').trim() === text);
                            if (matchingTextNodes.length === 1) {
                                return `${roleSelector}:has-text("${quoted(text)}")`;
                            }
                        }
                    }

                    const text = (el.innerText || el.textContent || '').trim();
                    if (el.tagName.toLowerCase() === 'button' && text && text.length <= 40 && !text.includes('\\n')) {
                        const sameButtons = Array.from(document.querySelectorAll('button')).filter(n => (n.innerText || n.textContent || '').trim() === text);
                        if (sameButtons.length === 1) {
                            return `button:has-text("${quoted(text)}")`;
                        }
                    }

                    const classes = (el.className || '').toString().trim().split(/\\s+/).filter(Boolean).slice(0, 2);
                    if (classes.length > 0) {
                        const classSelector = `${el.tagName.toLowerCase()}.${classes.map(cssEscape).join('.')}`;
                        if (document.querySelectorAll(classSelector).length === 1) return classSelector;
                    }

                    const segments = [];
                    let current = el;
                    let depth = 0;
                    while (current && current.nodeType === Node.ELEMENT_NODE && depth < 6) {
                        let segment = current.tagName.toLowerCase();
                        const parent = current.parentElement;
                        if (parent) {
                            const siblings = Array.from(parent.children).filter((child) => child.tagName === current.tagName);
                            if (siblings.length > 1) {
                                const index = siblings.indexOf(current) + 1;
                                segment += `:nth-of-type(${index})`;
                            }
                        }
                        segments.unshift(segment);
                        current = current.parentElement;
                        depth += 1;
                    }
                    return segments.join(' > ');
                } catch {
                    return selector;
                }
            };

            const attributesOf = (el) => {
                const attributes = {};
                if (el && el.attributes) {
                    for (const attr of Array.from(el.attributes)) {
                        attributes[attr.name] = attr.value ?? '';
                    }
                }
                return attributes;
            };

            return nodes
                .map((el) => {
                    const text = (el.innerText || el.textContent || '').trim();
                    return {
                        type: elementType,
                        text,
                        selector: buildSelector(el),
                        visible: isVisible(el),
                        attributes: attributesOf(el),
                    };
                })
                .filter((item) => {
                    if (!item.visible) return false;
                    if (!textOnly) return true;
                    return item.text.length >= textMinLength;
                });
        }
        """

        return await page.evaluate(
            js,
            {
                "selector": selector,
                "elementType": element_type,
                "textOnly": text_only,
                "textMinLength": self.text_min_length,
            },
        )

    def _normalize_result(self, raw: Any, fallback_type: ElementType) -> list[DomElement]:
        if not isinstance(raw, list):
            return []

        normalized: list[DomElement] = []
        for item in raw:
            if not isinstance(item, dict):
                continue

            normalized_item: DomElement = {
                "type": self._normalize_type(item.get("type"), fallback_type),
                "text": self._safe_text(item.get("text")),
                "selector": self._safe_selector(item.get("selector")),
                "visible": bool(item.get("visible")),
                "attributes": item.get("attributes") if isinstance(item.get("attributes"), dict) else {},
            }
            normalized.append(normalized_item)

        return normalized

    def _dedupe_elements(self, elements: list[DomElement]) -> list[DomElement]:
        seen: set[tuple[str, str]] = set()
        deduped: list[DomElement] = []

        for item in elements:
            key = (str(item.get("selector", "")), str(item.get("text", "")))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        return deduped

    @staticmethod
    def button_selectors() -> list[str]:
        """Primary + fallback selectors for robust interactive control extraction."""
        return [
            # Primary broad selector set: buttons, combobox triggers, options, and menu items.
            (
                "button, input[type='button'], input[type='submit'], "
                "[role='button'], div[role='button'], "
                "[role='combobox'], [aria-haspopup='listbox'], [aria-haspopup='menu'], "
                "[role='option'], [role='menuitem'], [role='menuitemradio'], [role='menuitemcheckbox'], "
                "[role='tab'], li[role='option']"
            ),
            # Fallback: common clickable non-semantic controls.
            "a[role='button'], span[role='button'], [aria-pressed], [tabindex='0'][onclick]",
        ]

    @staticmethod
    def _normalize_type(value: Any, fallback_type: ElementType) -> ElementType:
        if value in {"button", "input", "link", "form", "text"}:
            return value
        return fallback_type

    @staticmethod
    def _safe_text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        return ""

    @staticmethod
    def _safe_selector(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        return ""

