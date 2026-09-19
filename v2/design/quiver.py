"""QuiverAI — the vector graphics the console is drawn with.

Quiver generates SVG, not data, so nothing here runs during a call. The
generator script calls it once, the markup lands in ``v2/web/assets``
and gets committed; from then on the dashboard reads the icons off disk. A jury
demo on a dead venue wifi still has its icons, and a page reload never spends a
credit.

Synchronous on purpose: this is a build tool, and the call path must not be able
to reach it by accident.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from v2.config import Config

settings = Config()

GENERATIONS = "/v1/svgs/generations"
ANIMATIONS = "/v1/svgs/animations"
MODELS = "/v1/models"

# A drawing can take a while, and a retried generation is a second charge.
TIMEOUT_S = 240.0
ATTEMPTS = 3
BACKOFF_S = 2.0

# Model-authored markup is inlined into the page so `currentColor` follows the
# theme, which means it must not be able to carry anything executable. These are
# our own committed build artefacts rather than user input, so this is a second
# line rather than the only one.
_SCRIPTISH = re.compile(r"<\s*(script|foreignObject)\b.*?<\s*/\s*\1\s*>", re.I | re.S)
_BARE_SCRIPTISH = re.compile(r"<\s*(script|foreignObject)\b[^>]*/?>", re.I)
_HANDLER = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_BAD_URL = re.compile(r"(href|xlink:href)\s*=\s*(\"|')?\s*javascript:[^\"'>]*(\"|')?", re.I)


class QuiverError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, request_id: str = "") -> None:
        super().__init__(f"{status} {code}: {message}" if status else f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id

    @property
    def out_of_credit(self) -> bool:
        return self.code in {"insufficient_credits", "weekly_limit_exceeded"}

    @property
    def unsupported(self) -> bool:
        """The account cannot do this at all, so no amount of retrying helps.

        Animation is gated per organization — Quiver's own list of core
        endpoints leaves it out — and a hackathon key most likely lacks it.
        """
        return self.code in {"unsupported_animation_request", "model_not_found"}


@dataclass
class Drawing:
    """One SVG that came back, and what it cost."""

    svg: str
    model: str
    credits: int = 0
    total_tokens: int = 0
    request_id: str = ""


class QuiverClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
        key = api_key or settings.quiver_api_key
        if not key:
            raise QuiverError(
                0, "invalid_api_key",
                "QUIVER_API_KEY is not set. Put the key from quiver.ai in .env",
            )
        self._client = httpx.Client(
            base_url=base_url or settings.quiver_base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(TIMEOUT_S, connect=10.0),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "QuiverClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ---- models -------------------------------------------------------

    def models(self) -> list[str]:
        """Every model id this key may use."""
        body = self._request("GET", MODELS)
        rows = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(rows, list):
            return []
        ids: list[str] = []
        for row in rows:
            if isinstance(row, str):
                ids.append(row)
            elif isinstance(row, dict):
                found = row.get("id") or row.get("model") or row.get("name")
                if found:
                    ids.append(str(found))
        return ids

    def pick_model(self, preferred: Optional[str] = None) -> str:
        """Settle on a model without pinning an id the account may not have.

        Newest Arrow first, and the standard size over Max: an icon is a dozen
        paths, so Max buys nothing an icon can show and costs more. The list
        arrives in no promised order, hence sorting on the version rather than
        trusting the first match.
        """
        preferred = (preferred or settings.quiver_model or "").strip()
        available = self.models()
        if preferred:
            if available and preferred not in available:
                raise QuiverError(
                    404, "model_not_found",
                    f"{preferred!r} is not on this key. Available: {', '.join(available) or 'none'}",
                )
            return preferred
        if not available:
            raise QuiverError(404, "model_not_found", "this key lists no models")
        plain = [m for m in available if "arrow" in m.lower() and "max" not in m.lower()]
        arrow = [m for m in available if "arrow" in m.lower()]
        return sorted(plain or arrow or available, key=_version, reverse=True)[0]

    # ---- drawing ------------------------------------------------------

    def draw(
        self,
        prompt: str,
        model: str,
        instructions: Optional[str] = None,
        width: int = 24,
        height: int = 24,
        temperature: float = 0.4,
        reasoning_effort: str = "medium",
    ) -> Drawing:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "stream": False,
            "temperature": temperature,
            "reasoning_effort": reasoning_effort,
            "attributes": {"viewBox": {"minX": 0, "minY": 0, "width": width, "height": height}},
        }
        if instructions:
            payload["instructions"] = instructions

        return _drawing_from(self._request("POST", GENERATIONS, json=payload), model)

    def animate(
        self,
        svg: str,
        model: str,
        prompt: Optional[str] = None,
        temperature: float = 0.4,
        reasoning_effort: str = "medium",
    ) -> Drawing:
        """Put motion inside an SVG we already have.

        Sent as base64 rather than a URL so nothing has to be publicly hosted
        first. Raises with ``unsupported`` set when the account lacks animation,
        which is the common case and the caller's cue to fall back to CSS.
        """
        payload: dict[str, Any] = {
            "model": model,
            "svg_source": {"base64": base64.b64encode(svg.encode("utf-8")).decode("ascii")},
            "stream": False,
            "temperature": temperature,
            "reasoning_effort": reasoning_effort,
        }
        if prompt:
            payload["prompt"] = prompt

        return _drawing_from(self._request("POST", ANIMATIONS, json=payload), model)

    # ---- transport ----------------------------------------------------

    def _request(self, method: str, path: str, json: Optional[dict[str, Any]] = None) -> Any:
        last: Optional[QuiverError] = None

        for attempt in range(ATTEMPTS):
            try:
                response = self._client.request(method, path, json=json)
            except httpx.HTTPError as exc:
                last = QuiverError(0, "server_error", str(exc))
            else:
                if response.status_code < 300:
                    return response.json()
                last = _error_from(response)
                # A refusal, a bad key or an empty balance will say the same
                # thing three times, and two of those three would still be
                # charged if they were generations.
                if not _worth_retrying(last):
                    raise last
                wait = last.retry_after if hasattr(last, "retry_after") else None
                time.sleep(wait or BACKOFF_S * (attempt + 1))
                continue
            if attempt < ATTEMPTS - 1:
                time.sleep(BACKOFF_S * (attempt + 1))

        assert last is not None
        raise last


def _drawing_from(body: dict[str, Any], model: str) -> Drawing:
    documents = body.get("data") or []
    if not documents or not documents[0].get("svg"):
        raise QuiverError(502, "model_error", "the response carried no SVG")
    usage = body.get("usage") or {}
    return Drawing(
        svg=sanitize(documents[0]["svg"]),
        model=model,
        credits=int(body.get("credits") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        request_id=str(body.get("id") or ""),
    )


def _version(name: str) -> tuple[float, ...]:
    """Sort key from whatever numbers a model id carries, so arrow-2 outranks
    arrow-1.1 without hard-coding either of them."""
    found = re.findall(r"\d+(?:\.\d+)?", name)
    return tuple(float(part) for part in found) if found else (0.0,)


def _worth_retrying(error: QuiverError) -> bool:
    if error.status in {408, 429}:
        return True
    return error.status == 0 or 500 <= error.status < 600


def _error_from(response: httpx.Response) -> QuiverError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = QuiverError(
        response.status_code,
        str(body.get("code") or "server_error"),
        str(body.get("message") or response.text[:300] or response.reason_phrase),
        str(body.get("request_id") or ""),
    )
    retry_after = body.get("retry_after") or response.headers.get("Retry-After")
    try:
        error.retry_after = min(30, int(retry_after)) if retry_after else None  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        error.retry_after = None  # type: ignore[attr-defined]
    return error


def sanitize(svg: str) -> str:
    """Strip anything that should never survive into an inlined icon."""
    cleaned = _SCRIPTISH.sub("", svg)
    cleaned = _BARE_SCRIPTISH.sub("", cleaned)
    cleaned = _HANDLER.sub("", cleaned)
    cleaned = _BAD_URL.sub("", cleaned)
    # A model occasionally wraps the markup in prose or a code fence.
    found = re.search(r"<svg\b.*</svg>", cleaned, re.I | re.S)
    return (found.group(0) if found else cleaned).strip()


_VIEWBOX = re.compile(r'viewBox\s*=\s*"([^"]+)"', re.I)
_RECT = re.compile(r"<rect\b[^>]*?/?>", re.I)
_ATTR = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
_PAINT_ATTR = re.compile(r'\b(fill|stroke)\s*=\s*"([^"]*)"', re.I)
_PAINT_STYLE = re.compile(r"\b(fill|stroke)\s*:\s*([^;\"']+)", re.I)

_NAMED_LUMA = {
    "white": 1.0, "black": 0.0, "silver": 0.75, "gray": 0.5, "grey": 0.5,
    "whitesmoke": 0.96, "ivory": 0.99, "snow": 0.99,
}
_KEEP = {"none", "currentcolor", "inherit", "transparent", ""}


def _luma(value: str) -> Optional[float]:
    """Perceived lightness of a colour, or None when it is not one."""
    text = value.strip().lower()
    if text in _KEEP:
        return None
    if text in _NAMED_LUMA:
        return _NAMED_LUMA[text]
    hexed = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", text)
    if hexed:
        digits = hexed.group(1)
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        r, g, b = (int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    rgb = re.fullmatch(r"rgba?\(([^)]*)\)", text)
    if rgb:
        parts = re.findall(r"[\d.]+", rgb.group(1))[:3]
        if len(parts) == 3:
            r, g, b = (min(255.0, float(p)) / 255 for p in parts)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b
    return None


def _length(value: str, extent: float) -> float:
    text = (value or "").strip()
    if text.endswith("%"):
        try:
            return float(text[:-1]) / 100 * extent
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def themeable(svg: str) -> tuple[str, dict[str, int]]:
    """Turn Arrow's output into an icon that follows the theme.

    Arrow draws white line work on a solid dark plate rather than the flat
    ``currentColor`` line icon the instructions ask for, every time. Arguing
    with it costs a generation per attempt and may not land, so the plate is
    dropped and whatever is left is repainted as ``currentColor`` — which is
    deterministic, free, and leaves the theme in charge of the colour.
    """
    found = _VIEWBOX.search(svg)
    box_w, box_h = 24.0, 24.0
    if found:
        numbers = re.findall(r"-?[\d.]+", found.group(1))
        if len(numbers) == 4:
            box_w, box_h = float(numbers[2]) or 24.0, float(numbers[3]) or 24.0

    plates = 0

    def maybe_plate(match: re.Match) -> str:
        nonlocal plates
        attrs = {k.lower(): v for k, v in _ATTR.findall(match.group(0))}
        luma = _luma(attrs.get("fill", ""))
        stroke = attrs.get("stroke", "none").strip().lower()
        wide = _length(attrs.get("width", "0"), box_w) >= box_w * 0.55
        tall = _length(attrs.get("height", "0"), box_h) >= box_h * 0.55
        # A backdrop is big, solid, dark and never outlined. Real icon geometry
        # in this style is drawn as strokes, so it cannot match all four.
        if luma is not None and luma < 0.5 and stroke in {"none", ""} and wide and tall:
            plates += 1
            return ""
        return match.group(0)

    out = _RECT.sub(maybe_plate, svg)

    painted = 0

    def repaint(match: re.Match) -> str:
        nonlocal painted
        value = match.group(2).strip()
        # A gradient reference is every bit as fixed as a hex literal — the
        # gradient it points at holds its own stops — so it goes the same way.
        gradient = value.lower().startswith("url(")
        if not gradient and _luma(value) is None:
            return match.group(0)
        painted += 1
        joiner = "=" if "=" in match.group(0) else ":"
        quote = '"' if joiner == "=" else ""
        return f"{match.group(1)}{joiner}{quote}currentColor{quote}"

    out = _PAINT_ATTR.sub(repaint, out)
    out = _PAINT_STYLE.sub(repaint, out)
    # CSS owns the rendered size; leaving Quiver's width/height on the root
    # fights the icon slot and is what made some marks sit off-centre.
    out = re.sub(r'\s(width|height)="[^"]*"', "", out, count=2)
    if "preserveAspectRatio=" not in out:
        out = re.sub(r"<svg\b", '<svg preserveAspectRatio="xMidYMid meet"', out, count=1, flags=re.I)
    return out.strip(), {"plates": plates, "paints": painted}


def frame_viewbox(svg: str, bbox: tuple[float, float, float, float], *, square: bool = True, fill: float = 0.72) -> str:
    """Rewrites the root viewBox so ``bbox`` sits centred at ``fill`` of the frame.

    ``bbox`` is (x, y, w, h) in the SVG's current user space — typically from a
    browser ``getBBox()``. Icons get a square frame; the empty-state scene keeps
    its aspect ratio.
    """
    x, y, w, h = bbox
    if w <= 0 or h <= 0:
        return svg
    if square:
        side = max(w, h) / fill
        vx, vy, vw, vh = x + w / 2 - side / 2, y + h / 2 - side / 2, side, side
    else:
        pad_x, pad_y = w * 0.08, h * 0.1
        vx, vy, vw, vh = x - pad_x, y - pad_y, w + 2 * pad_x, h + 2 * pad_y
    framed = f'{vx:.3f} {vy:.3f} {vw:.3f} {vh:.3f}'
    if _VIEWBOX.search(svg):
        return _VIEWBOX.sub(f'viewBox="{framed}"', svg, count=1)
    return re.sub(r"<svg\b", f'<svg viewBox="{framed}"', svg, count=1, flags=re.I)


def hardcoded_colours(svg: str) -> list[str]:
    """Colours that would stop an icon following the theme.

    Not an error — the icon still draws — but the set is themeable only as long
    as everything paints with currentColor, so a run says which ones drifted.
    """
    found = re.findall(r"(?:fill|stroke)\s*=\s*[\"']([^\"']+)[\"']", svg, re.I)
    return sorted({
        value for value in found
        if value.lower() not in {"currentcolor", "none", "inherit", "transparent"}
    })
