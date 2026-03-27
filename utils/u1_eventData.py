"""ParentMap event scraper with robots.txt checks.

Usage:
    python -m utils.u1_eventData "https://www.parentmap.com/calendar/..."
    python -m utils.u1_eventData --calendar-url "https://www.parentmap.com/calendar" --pages 3 --delay 1.5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from http.client import IncompleteRead
from hashlib import sha1
from dataclasses import asdict, dataclass
from html import unescape
from pathlib import Path
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

USER_AGENT = "WhatsHappeninEventBot/0.1 (+https://example.local)"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EVENTS_JSONL = DATA_DIR / "events.jsonl"
PARENTMAP_ROOT = "https://www.parentmap.com"

# One robots.txt fetch per origin per process; can_fetch() is then cheap (no I/O).
_robots_parsers: dict[str, robotparser.RobotFileParser] = {}


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


def _get_html(url: str, timeout: int = 20, max_attempts: int = 3) -> str:
    """GET HTML with retries for truncated responses and transient network errors."""
    for attempt in range(max_attempts):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < max_attempts - 1:
                time.sleep(min(2**attempt, 8.0))
                continue
            raise
        except (IncompleteRead, URLError, TimeoutError, ConnectionError) as e:
            if attempt < max_attempts - 1:
                time.sleep(min(2**attempt, 8.0))
                continue
            raise


def _robots_cache_key(parsed) -> str:
    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()
    return f"{scheme}://{netloc}"


def _get_robots_parser(parsed) -> robotparser.RobotFileParser:
    key = _robots_cache_key(parsed)
    if key not in _robots_parsers:
        rp = robotparser.RobotFileParser()
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc
        rp.set_url(f"{scheme}://{netloc}/robots.txt")
        rp.read()
        _robots_parsers[key] = rp
    return _robots_parsers[key]


def is_allowed_by_robots(url: str, user_agent: str = USER_AGENT) -> bool:
    parsed = urlparse(url)
    rp = _get_robots_parser(parsed)
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


def extract_event_urls_from_calendar_html(html: str, base_url: str = PARENTMAP_ROOT) -> list[str]:
    raw_paths = re.findall(r'href=["\'](/calendar/[^"\'?#]+)["\']', html, flags=re.IGNORECASE)
    seen: set[str] = set()
    event_urls: list[str] = []
    for path in raw_paths:
        if path.lower() == "/calendar":
            continue
        full_url = urljoin(base_url, path)
        if full_url in seen:
            continue
        seen.add(full_url)
        event_urls.append(full_url)
    return event_urls


def discover_calendar_event_urls(
    calendar_url: str, pages: int = 1, delay_seconds: float = 1.0
) -> tuple[list[str], list[str]]:
    """Return (deduped event detail URLs, listing page URLs fetched in order)."""
    seen: set[str] = set()
    all_event_urls: list[str] = []
    listing_pages_pulled: list[str] = []
    for page_idx in range(max(1, pages)):
        if page_idx == 0:
            page_url = calendar_url
        else:
            separator = "&" if "?" in calendar_url else "?"
            page_url = f"{calendar_url}{separator}page={page_idx}"
        listing_pages_pulled.append(page_url)
        html = _get_html(page_url)
        event_urls = extract_event_urls_from_calendar_html(html, base_url=calendar_url)
        for event_url in event_urls:
            if event_url in seen:
                continue
            seen.add(event_url)
            all_event_urls.append(event_url)
        if delay_seconds > 0 and page_idx < pages - 1:
            time.sleep(delay_seconds)
    return all_event_urls, listing_pages_pulled


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
    event_payload = result["event"]
    event_payload["event_id"] = build_event_id(url)
    event_payload["occurrence_ids"] = build_occurrence_ids(
        event_payload["event_id"],
        event.schedule,
        event.date_range,
    )
    return result


def _canonical_event_key(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().strip()
    path = parsed.path.strip().lower().rstrip("/")
    if not path:
        path = "/"
    return f"{host}{path}"


def build_event_id(url: str, source_prefix: str = "parentmap") -> str:
    canonical_key = _canonical_event_key(url)
    digest = sha1(canonical_key.encode("utf-8")).hexdigest()
    return f"{source_prefix}_{digest}"


def _normalize_occurrence_value(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value


def build_occurrence_ids(event_id: str, schedule: list[str], date_range: str | None = None) -> list[str]:
    seeds = schedule if schedule else ([date_range] if date_range else [])
    occurrence_ids: list[str] = []
    for seed in seeds:
        if not seed:
            continue
        normalized = _normalize_occurrence_value(seed)
        digest = sha1(f"{event_id}|{normalized}".encode("utf-8")).hexdigest()
        occurrence_ids.append(f"{event_id}_occ_{digest[:16]}")
    return occurrence_ids


def write_payload_to_jsonl(payload: dict, output_path: Path = EVENTS_JSONL) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return output_path


def _load_existing_urls(output_path: Path) -> set[str]:
    if not output_path.exists():
        return set()
    seen_urls: set[str] = set()
    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = record.get("url")
            if isinstance(url, str) and url:
                seen_urls.add(url)
    return seen_urls


def scrape_calendar_to_jsonl(
    calendar_url: str,
    pages: int = 1,
    output_path: Path = EVENTS_JSONL,
    delay_seconds: float = 1.0,
) -> dict:
    event_urls, listing_pages_pulled = discover_calendar_event_urls(
        calendar_url, pages=pages, delay_seconds=delay_seconds
    )
    existing_urls = _load_existing_urls(output_path)

    scraped = 0
    skipped_existing = 0
    failed = 0

    for idx, event_url in enumerate(event_urls, start=1):
        if event_url in existing_urls:
            skipped_existing += 1
            continue
        try:
            payload = scrape_event_to_json(event_url)
            write_payload_to_jsonl(payload, output_path=output_path)
            existing_urls.add(event_url)
            scraped += 1
            print(f"[{idx}/{len(event_urls)}] Scraped {event_url}")
        except (HTTPError, URLError, ValueError) as exc:
            failed += 1
            print(f"[{idx}/{len(event_urls)}] Failed {event_url}: {exc}")
        if delay_seconds > 0 and idx < len(event_urls):
            time.sleep(delay_seconds)

    return {
        "calendar_url": calendar_url,
        "pages": pages,
        "listing_pages_pulled": listing_pages_pulled,
        "discovered_event_urls": len(event_urls),
        "scraped": scraped,
        "skipped_existing": skipped_existing,
        "failed": failed,
        "output_path": str(output_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape ParentMap event pages into JSONL.")
    parser.add_argument("event_url", nargs="?", help="Single ParentMap event URL to scrape.")
    parser.add_argument(
        "--calendar-url",
        help="ParentMap calendar listing URL (for example: https://www.parentmap.com/calendar).",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="Number of calendar pages to crawl when --calendar-url is provided.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between requests to avoid overwhelming the site.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=EVENTS_JSONL,
        help="JSONL output path.",
    )
    args = parser.parse_args()

    if not args.event_url and not args.calendar_url:
        parser.print_help()
        return 1

    try:
        if args.calendar_url:
            summary = scrape_calendar_to_jsonl(
                args.calendar_url.strip(),
                pages=max(1, args.pages),
                output_path=args.output,
                delay_seconds=max(0.0, args.delay),
            )
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0

        payload = scrape_event_to_json(args.event_url.strip())
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        output_path = write_payload_to_jsonl(payload, output_path=args.output)
        print(f"\nAppended JSONL record to: {output_path}")
        return 0
    except (HTTPError, URLError, ValueError) as exc:
        error_url = args.calendar_url or args.event_url
        print(json.dumps({"url": error_url, "error": str(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
