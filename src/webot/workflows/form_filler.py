from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from webot.utils.logger import get_logger


class UserProfile(TypedDict, total=False):
    name: str
    email: str
    phone: str
    education: str
    skills: str | list[str]


class FieldInfo(TypedDict, total=False):
    selector: str
    tag: str
    input_type: str
    name: str
    id: str
    placeholder: str
    aria_label: str
    label: str
    visible: bool


class FillResult(TypedDict):
    filled: int
    skipped: int
    failed: int
    details: list[dict[str, str]]


class LlmLike(Protocol):
    def generate(self, prompt: str, *, model: str | None = None) -> str: ...


@dataclass(slots=True)
class FormFiller:
    """Detect form fields and fill them from user profile data."""

    llm_client: LlmLike | None = None

    async def fill(self, page: Page, profile: UserProfile) -> FillResult:
        logger = get_logger(__name__)
        fields = await self._detect_fields(page)

        result: FillResult = {"filled": 0, "skipped": 0, "failed": 0, "details": []}

        unmatched: list[FieldInfo] = []
        for field in fields:
            profile_key = self._deterministic_match(field)
            if profile_key is None:
                unmatched.append(field)
                continue

            ok, reason = await self._fill_field(page, field, profile_key, profile)
            self._update_result(result, field, profile_key, ok, reason)

        if unmatched and self.llm_client is not None:
            llm_map = self._llm_match(unmatched)
            for field in unmatched:
                selector = field.get("selector", "")
                profile_key = llm_map.get(selector)
                if profile_key not in {"name", "email", "phone", "education", "skills"}:
                    self._update_result(result, field, "", False, "no_match")
                    continue
                ok, reason = await self._fill_field(page, field, profile_key, profile)
                self._update_result(result, field, profile_key, ok, reason)
        else:
            for field in unmatched:
                self._update_result(result, field, "", False, "no_match")

        await self._select_default_options(page)
        logger.info("form_fill_completed", extra={"result": result})
        return result

    async def _detect_fields(self, page: Page) -> list[FieldInfo]:
        js = """
        () => {
          const nodes = Array.from(document.querySelectorAll('input, textarea, select'));
          const isVisible = (el) => {
            const s = window.getComputedStyle(el);
            if (!s) return false;
            if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };

          const esc = (value) => {
            if (typeof CSS !== 'undefined' && CSS.escape) return CSS.escape(value);
            return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
          };

          const selectorFor = (el) => {
            if (el.id) return '#' + esc(el.id);
            const n = el.getAttribute('name');
            if (n) return `${el.tagName.toLowerCase()}[name="${String(n).replace(/"/g, '\\"')}"]`;
            return el.tagName.toLowerCase();
          };

          const labelFor = (el) => {
            if (el.labels && el.labels.length > 0) {
              return Array.from(el.labels).map((x) => (x.innerText || '').trim()).join(' ').trim();
            }
            return '';
          };

          return nodes.map((el) => ({
            selector: selectorFor(el),
            tag: el.tagName.toLowerCase(),
            input_type: (el.getAttribute('type') || '').toLowerCase(),
            name: el.getAttribute('name') || '',
            id: el.id || '',
            placeholder: el.getAttribute('placeholder') || '',
            aria_label: el.getAttribute('aria-label') || '',
            label: labelFor(el),
            visible: isVisible(el),
          })).filter((x) => x.visible);
        }
        """
        raw = await page.evaluate(js)
        if not isinstance(raw, list):
            return []
        out: list[FieldInfo] = []
        for item in raw:
            if isinstance(item, dict):
                out.append(item)
        return out

    def _deterministic_match(self, field: FieldInfo) -> str | None:
        hay = " ".join(
            [
                str(field.get("name", "")),
                str(field.get("id", "")),
                str(field.get("placeholder", "")),
                str(field.get("aria_label", "")),
                str(field.get("label", "")),
            ]
        ).lower()

        rules: list[tuple[str, list[str]]] = [
            ("email", ["email", "e-mail", "mail"]),
            ("phone", ["phone", "mobile", "contact", "telephone", "tel"]),
            ("name", ["full name", "name", "first name", "last name"]),
            ("education", ["education", "degree", "university", "college", "school", "qualification"]),
            ("skills", ["skills", "skill", "technologies", "tech stack", "expertise", "competencies"]),
        ]

        for key, tokens in rules:
            if any(token in hay for token in tokens):
                return key
        return None

    async def _fill_field(
        self,
        page: Page,
        field: FieldInfo,
        profile_key: str,
        profile: UserProfile,
    ) -> tuple[bool, str]:
        selector = str(field.get("selector", "")).strip()
        if not selector:
            return False, "missing_selector"

        value = self._profile_value(profile, profile_key)
        if value is None:
            return False, "missing_profile_value"

        try:
            tag = str(field.get("tag", "")).lower()
            if tag == "select":
                await page.select_option(selector, label=value)
            else:
                await page.fill(selector, value)
            return True, "filled"
        except PlaywrightError as exc:
            return False, f"playwright_error:{exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"error:{exc}"

    def _profile_value(self, profile: UserProfile, key: str) -> str | None:
        raw = profile.get(key)
        if raw is None:
            return None
        if key == "skills" and isinstance(raw, list):
            value = ", ".join(str(item) for item in raw if str(item).strip())
            return value if value.strip() else None
        if isinstance(raw, str) and raw.strip():
            return raw
        return None

    def _llm_match(self, fields: list[FieldInfo]) -> dict[str, str]:
        if self.llm_client is None:
            return {}

        compact = [
            {
                "selector": f.get("selector", ""),
                "name": f.get("name", ""),
                "id": f.get("id", ""),
                "placeholder": f.get("placeholder", ""),
                "label": f.get("label", ""),
            }
            for f in fields[:20]
        ]
        prompt = (
            "Map form fields to one of: name,email,phone,education,skills,null. "
            "Return JSON object: {selector: key_or_null}. No markdown.\n"
            f"Fields:{json.dumps(compact, ensure_ascii=True, separators=(',', ':'))}"
        )

        try:
            raw = self.llm_client.generate(prompt)
            data = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
            if not isinstance(data, dict):
                return {}
            out: dict[str, str] = {}
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str):
                    out[k] = v
            return out
        except Exception:
            return {}

    @staticmethod
    def _update_result(
        result: FillResult,
        field: FieldInfo,
        key: str,
        ok: bool,
        reason: str,
    ) -> None:
        selector = str(field.get("selector", ""))
        if ok:
            result["filled"] += 1
            result["details"].append({"selector": selector, "field": key, "status": "filled"})
            return

        if reason == "no_match":
            result["skipped"] += 1
            result["details"].append({"selector": selector, "field": key, "status": "skipped"})
            return

        result["failed"] += 1
        result["details"].append({"selector": selector, "field": key, "status": reason})

    async def _select_default_options(self, page: Page) -> None:
        js = """
        () => {
          const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
          const groups = {};
          radios.forEach(r => {
            const name = r.getAttribute('name');
            if (name) {
              if (!groups[name]) groups[name] = [];
              groups[name].push(r);
            }
          });

          for (const name in groups) {
            const group = groups[name];
            const anyChecked = group.some(r => r.checked);
            if (!anyChecked && group.length > 0) {
              const first = group[0];
              first.checked = true;
              first.dispatchEvent(new Event('change', { bubbles: true }));
              if (first.id) {
                const lbl = document.querySelector(`label[for="${first.id}"]`);
                if (lbl) {
                  lbl.click();
                }
              }
            }
          }
        }
        """
        try:
            await page.evaluate(js)
        except Exception:
            pass
