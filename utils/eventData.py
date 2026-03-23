"""ParentMap event scraper with robots.txt checks.

Usage:
    python -m utils.eventData "https://www.parentmap.com/calendar/..."
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from html import unescape
from pathlib import Path
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

USER_AGENT = "WhatsHappeninEventBot/0.1 (+https://example.local)"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass
class EventData:
    url: str
    title: str | None
    date_range: str | None
    schedule: list[str]
    description: str | None
    price: str | None
    recommended_ages: str | None
    venue: str | None
    address: str | None
    event_types: list[str]
    source_url: str | None


def _get_html(url: str, timeout: int = 20) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def is_allowed_by_robots(url: str, user_agent: str = USER_AGENT) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = robotparser.RobotFileParser()
    rp.set_url(robots_url)
    rp.read()
    return rp.can_fetch(user_agent, url)


def _first_match(pattern: str, text: str, flags: int = 0) -> str | None:
    match = re.search(pattern, text, flags)
    return unescape(match.group(1).strip()) if match else None


def _clean_text(html_snippet: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_snippet)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_tags(value: str | None) -> str | None:
    if value is None:
        return None
    return _clean_text(value)


def _all_matches(pattern: str, text: str, flags: int = 0) -> list[str]:
    return [unescape(item.strip()) for item in re.findall(pattern, text, flags)]


def _get_section(pattern: str, text: str, flags: int = 0) -> str | None:
    match = re.search(pattern, text, flags)
    return match.group(1) if match else None


def parse_parentmap_event(url: str) -> EventData:
    html = _get_html(url)

    title = _strip_tags(_first_match(r"<h1[^>]*>(.*?)</h1>", html, flags=re.IGNORECASE | re.DOTALL))

    # ParentMap typically renders event dates in one or more `field-content` spans.
    date_values = _all_matches(
        r'views-field-field-event-date-recur-value"><span class="field-content">(.*?)</span>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    date_values = [_strip_tags(value) for value in date_values if _strip_tags(value)]
    # Keep ordering and remove duplicates.
    seen: set[str] = set()
    deduped_dates: list[str] = []
    for value in date_values:
        if value not in seen:
            seen.add(value)
            deduped_dates.append(value)

    short_dates: list[str] = [
        value
        for value in deduped_dates
        if not re.search(r"\b@\b|\ba\.m\.|\bp\.m\.", value, flags=re.IGNORECASE)
    ]
    if len(short_dates) >= 2:
        date_range = f"{short_dates[0]} — {short_dates[1]}"
    elif short_dates:
        date_range = short_dates[0]
    elif deduped_dates:
        date_range = deduped_dates[0]
    else:
        date_range = _first_match(
            r"<meta[^>]+property=[\"']article:published_time[\"'][^>]+content=[\"'](.*?)[\"']",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )

    body_html = _get_section(
        r'<div class="event-content">[\s\S]*?<div class="body">(.*?)</div>\s*</div>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    description = _clean_text(body_html) if body_html else None

    price = _strip_tags(
        _first_match(
            r'<div class="field_event_cost">(.*?)</div>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    recommended_ages = _strip_tags(
        _first_match(
            r'<div class="field_age_recommendation">(.*?)</div>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )

    venue = _strip_tags(
        _first_match(
            r'<div class="venue-name[^"]*">(.*?)</div>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    address_line1 = _strip_tags(
        _first_match(
            r'<span class="address-line1">(.*?)</span>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    locality = _strip_tags(
        _first_match(
            r'<span class="locality">(.*?)</span>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    admin_area = _strip_tags(
        _first_match(
            r'<span class="administrative-area">(.*?)</span>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    postal_code = _strip_tags(
        _first_match(
            r'<span class="postal-code">(.*?)</span>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    country = _strip_tags(
        _first_match(
            r'<span class="country">(.*?)</span>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    city_region = " ".join(part for part in [locality, admin_area, postal_code] if part)
    address_parts = [address_line1, city_region if city_region else None, country]
    address = ", ".join(part for part in address_parts if part)

    event_types = [
        _clean_text(part)
        for part in _all_matches(r'<ul class="field_event_type">([\s\S]*?)</ul>', html, re.IGNORECASE | re.DOTALL)
    ]
    if event_types:
        # `event_types` currently has the entire UL rendered to plain text in one string.
        # Split into list items for a clean JSON shape.
        event_types = [
            _clean_text(item).rstrip(",").strip()
            for item in re.findall(r"<li>([\s\S]*?)</li>", _get_section(r'(<ul class="field_event_type">[\s\S]*?</ul>)', html, re.IGNORECASE | re.DOTALL) or "", re.IGNORECASE | re.DOTALL)
            if _clean_text(item)
        ]

    schedule_block = _get_section(
        r'<li class="info-when info-icon">[\s\S]*?<h4 class="label">When</h4>([\s\S]*?)</li>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    schedule: list[str] = []
    if schedule_block:
        starts = _all_matches(
            r'views-field-field-event-date-recur-value"><span class="field-content">(.*?)</span>',
            schedule_block,
            re.IGNORECASE | re.DOTALL,
        )
        ends = _all_matches(
            r'views-field-field-event-date-recur-end-value"><span class="field-content">(.*?)</span>',
            schedule_block,
            re.IGNORECASE | re.DOTALL,
        )
        for idx, start in enumerate(starts):
            start_text = _strip_tags(start)
            end_text = _strip_tags(ends[idx]) if idx < len(ends) else None
            if start_text and end_text:
                schedule.append(f"{start_text}–{end_text}")
            elif start_text:
                schedule.append(start_text)

    source_url = _first_match(
        r'<div class="field_event_url[^"]*">\s*<a href="([^"]+)"',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if source_url and not source_url.startswith("http"):
        source_url = urljoin(url, source_url)

    return EventData(
        url=url,
        title=title,
        date_range=date_range,
        schedule=schedule,
        description=description,
        price=price,
        recommended_ages=recommended_ages,
        venue=venue,
        address=address,
        event_types=event_types,
        source_url=source_url,
    )


def scrape_event_to_json(url: str) -> dict:
    allowed = is_allowed_by_robots(url)
    result = {"url": url, "robots_allowed": allowed}
    if not allowed:
        result["error"] = "Blocked by robots.txt for configured user-agent."
        return result

    event = parse_parentmap_event(url)
    result["event"] = asdict(event)
    return result


def _safe_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    slug = parsed.path.strip("/").replace("/", "_")
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug).strip("-")
    if not slug:
        slug = "event"
    return f"{slug}.json"


def write_payload_to_data(payload: dict, filename: str | None = None) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_name = filename or _safe_filename_from_url(payload.get("url", "event"))
    if not out_name.endswith(".json"):
        out_name = f"{out_name}.json"
    output_path = DATA_DIR / out_name
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def main() -> int:
    if len(sys.argv) < 2:
        print('Usage: python -m utils.eventData "<event-url>" [output_filename.json]')
        return 1

    url = sys.argv[1].strip()
    output_filename = sys.argv[2].strip() if len(sys.argv) > 2 else None
    try:
        payload = scrape_event_to_json(url)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        output_path = write_payload_to_data(payload, output_filename)
        print(f"\nSaved JSON to: {output_path}")
        return 0
    except (HTTPError, URLError, ValueError) as exc:
        print(json.dumps({"url": url, "error": str(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
