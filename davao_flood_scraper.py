#!/usr/bin/env python3
"""
davao_flood_scraper.py
======================
Scrapes public Facebook posts & search results related to flooding in
Davao City, Philippines.  Uses undetected-chromedriver for stealth and a
persistent Chrome profile so the operator only needs to log in once.

Requirements
------------
    pip install undetected-chromedriver selenium pandas

Usage
-----
    # First run – a visible Chrome window opens so you can log in manually.
    python davao_flood_scraper.py --login

    # Subsequent runs – headless scraping using the saved session.
    python davao_flood_scraper.py

    # Control how far to scroll (default 50 scroll cycles per query).
    python davao_flood_scraper.py --scrolls 80

    # Specify output directory.
    python davao_flood_scraper.py --output-dir ./results
"""

from __future__ import annotations

import argparse
import atexit
import csv
import hashlib
import json
import logging
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set
from urllib.parse import quote_plus, urljoin, urlparse, parse_qs, urlencode, urlunparse

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    ElementClickInterceptedException,
    WebDriverException,
)

# ──────────────────────────────────────────────
# Configuration constants
# ──────────────────────────────────────────────

FLOOD_KEYWORDS: list[str] = [
    "baha", "nagbaha", "lunod", "taas ang tubig", "suba", 
    "sapa", "overflow", "lapok", "baha sa davao",
    "flood", "flooding", "flooded", "tubig",
]

URGENCY_KEYWORDS: list[str] = [
    "tabang", "rescue", "stranded", "trap", "atop",
    "emergency", "tulong", "evacuation", "evacuate",
    "nalumos", "saklolo", "help", "relief",
]

DAVAO_AREAS: list[str] = [
    "Jade Valley", "Matina Pangi", "Matina Crossing",
    "Maa", "Ma-a", "Buhangin", "Bangkal", "Tigatto",
    "Bucana", "Talomo", "Bunawan", "Waan", "Juliville",
    "San Rafael", "El Rio", "Davao", "Toril", "Calinan",
    "Panacan", "Sasa", "Agdao", "Poblacion",
    "Catalunan", "Mintal", "Tugbok", "Bago Aplaya",
    "Lanang", "Bajada",
]

SEARCH_QUERIES: list[str] = [
    # General flood + Davao
    "baha davao",
    # "flood davao ",
    # "taas ang tubig davao",

    # Rescue & urgency          Commented out as of the momment for faster data gathering at the moment
    # "rescue flood davao",
    # "stranded flood davao",
    # "evacuation flood davao",

    # Area-specific searches     Commented out as of the momment for faster data gathering at the moment
    # "baha Matina Pangi",
    # "baha Matina Crossing",
    # "baha Buhangin davao",
    # "baha Bangkal davao",
    # "baha Bucana davao",
    # "baha Tigatto davao",
    # "baha Bunawan davao",
    # "baha Talomo davao",
    # "baha Ma-a davao",
    # "flood Jade Valley davao",

    # Situational reports       Commented out as of the momment for faster data gathering at the moment
    # "CDRRMO davao flood",
    # "davao flood update",
    # "davao flood advisory",
]

# "See more" button labels (includes Bisaya/Cebuano localizations)
SEE_MORE_LABELS: list[str] = [
    "See more", "See More",
]

# ──────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────

@dataclass
class FloodReport:
    post_id: str = ""
    post_url: str = ""
    author: str = "Unknown"
    timestamp: str = ""
    full_text: str = ""
    detected_locations: List[str] = field(default_factory=list)
    urgency_level: str = "NORMAL"          # NORMAL | HIGH | SITREP
    media_urls: List[str] = field(default_factory=list)
    scraped_at: str = ""

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

LOG_FMT = "%(asctime)s | %(levelname)-7s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FMT)
log = logging.getLogger("davao_scraper")

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _hash_text(text: str) -> str:
    """Return an MD5 fingerprint to help deduplicate posts."""
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


def _detect_locations(text: str) -> list[str]:
    """Return every monitored Davao area mentioned in *text*."""
    found: list[str] = []
    lowered = text.lower()
    for area in DAVAO_AREAS:
        # Word-boundary-aware match to avoid false positives.
        pattern = re.compile(rf"\b{re.escape(area)}\b", re.IGNORECASE)
        if pattern.search(text):
            found.append(area)
    return list(dict.fromkeys(found))  # deduplicate, preserve order


def _classify_urgency(text: str) -> str:
    """Classify the urgency level of a post based on keyword density."""
    lowered = text.lower()
    urgency_hits = sum(1 for kw in URGENCY_KEYWORDS if kw.lower() in lowered)
    flood_hits = sum(1 for kw in FLOOD_KEYWORDS if kw.lower() in lowered)

    if urgency_hits >= 2:
        return "SITREP"       # Multiple rescue / emergency terms → critical
    if urgency_hits >= 1 or flood_hits >= 2:
        return "HIGH"
    return "NORMAL"


def _is_flood_relevant(text: str) -> bool:
    """Quick filter: does the text mention any flood-related keyword?"""
    lowered = text.lower()
    return any(kw.lower() in lowered for kw in FLOOD_KEYWORDS)


def _human_pause(lo: float = 2.5, hi: float = 5.0) -> None:
    """Randomized sleep to emulate human browsing cadence."""
    time.sleep(random.uniform(lo, hi))


def _smooth_scroll(driver, pixels: int = 800) -> None:
    """Scroll down in small increments to mimic natural behavior."""
    step = random.randint(80, 160)
    scrolled = 0
    while scrolled < pixels:
        driver.execute_script(f"window.scrollBy(0, {step});")
        scrolled += step
        time.sleep(random.uniform(0.05, 0.15))


def _build_search_url(query: str) -> str:
    return f"https://www.facebook.com/search/posts/?q={quote_plus(query)}"

# ──────────────────────────────────────────────
# Browser setup
# ──────────────────────────────────────────────

def create_driver(profile_dir: str, *, headless: bool = False) -> uc.Chrome:
    """Instantiate an undetected-chromedriver instance with a persistent
    Chrome profile so the Facebook session survives across runs."""

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--lang=en-US")

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")

    driver = uc.Chrome(options=options, version_main=151, use_subprocess=True)
    driver.set_page_load_timeout(60)
    driver.implicitly_wait(5)
    return driver


def ensure_logged_in(driver) -> bool:
    """Navigate to Facebook and return True if already authenticated."""
    driver.get("https://www.facebook.com/")
    _human_pause(3, 5)

    # Detect the logged-in state by looking for the profile nav or the
    # composer ("What's on your mind?") — both only appear post-login.
    try:
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, '[aria-label="Your profile"], [aria-label="Create a post"], [role="navigation"]')
            )
        )
        log.info("✓ Active Facebook session detected.")
        return True
    except TimeoutException:
        log.warning("✗ No active session – manual login required.")
        return False

# ──────────────────────────────────────────────
# Post extraction logic
# ──────────────────────────────────────────────

def _expand_see_more(driver, article) -> None:
    """Click every 'See more' expander inside an article so the full
    text (including addresses and phone numbers) is captured."""
    for label in SEE_MORE_LABELS:
        try:
            # Use XPath to find spans/divs whose text matches the label
            buttons = article.find_elements(
                By.XPATH,
                f".//*[self::div or self::span or self::a]"
                f"[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                f"'abcdefghijklmnopqrstuvwxyz'), '{label.lower()}')]"
            )
            for btn in buttons:
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    time.sleep(0.3)
                    btn.click()
                    time.sleep(0.8)
                except (ElementClickInterceptedException, StaleElementReferenceException):
                    try:
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(0.8)
                    except Exception:
                        pass
        except Exception:
            continue


def _extract_text(article) -> str:
    """Pull all visible text from the post container."""
    try:
        # Facebook wraps user-generated content in dir="auto" divs
        text_divs = article.find_elements(By.CSS_SELECTOR, 'div[dir="auto"]')
        parts = []
        for div in text_divs:
            t = div.text.strip()
            if t and len(t) > 2:
                parts.append(t)
        return "\n".join(parts)
    except Exception:
        return article.text.strip() if article.text else ""


def _extract_media_urls(article) -> list[str]:
    """Return direct `src` URLs for images inside the post."""
    urls: list[str] = []
    try:
        imgs = article.find_elements(By.CSS_SELECTOR, "img")
        for img in imgs:
            src = img.get_attribute("src") or ""
            # Filter out tiny icons, emoji, profile pics (usually <100px wide)
            if src and ("scontent" in src or "fbcdn" in src):
                width = img.get_attribute("width")
                natural = img.get_attribute("naturalWidth")
                # Only keep images likely to be content photos
                if width and int(width) < 50:
                    continue
                if natural and int(natural) < 80:
                    continue
                urls.append(src)
    except Exception:
        pass
    return list(dict.fromkeys(urls))  # deduplicate


def _clean_facebook_url(url: str) -> str:
    """Clean tracking params from Facebook URL while preserving essential post identifiers."""
    if not url:
        return ""
    if url.startswith("/"):
        url = "https://www.facebook.com" + url

    try:
        parsed = urlparse(url)
        # If it's a php script with query params, preserve essential identifiers
        if any(p in parsed.path for p in ["permalink.php", "story.php", "photo.php", "video.php", "watch"]):
            query_params = parse_qs(parsed.query)
            essential_keys = ["story_fbid", "id", "fbid", "v", "comment_id", "theater", "set"]
            kept = {k: v[0] for k, v in query_params.items() if k in essential_keys and v}
            clean_query = urlencode(kept)
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", clean_query, ""))
        else:
            # Clean path URL: strip ?__cft__... and tracking parameters
            clean_path = parsed.path.rstrip("/")
            return urlunparse((parsed.scheme, parsed.netloc, clean_path, "", "", ""))
    except Exception:
        return url.split("?")[0]


def _is_valid_author(name: str) -> bool:
    """Validate that the string is a real poster/page name, not a hashtag or UI label."""
    if not name or len(name) < 2 or len(name) > 90:
        return False
    t = name.strip()
    if t.startswith("#") or t.startswith("http") or t.startswith("@"):
        return False

    lowered = t.lower()
    invalid_keywords = [
        "see more", "see less", "like", "comment", "share", "follow", "facebook",
        "public", "joined", "replies", "reaction", "suggested", "sponsored",
        "tan-awa", "tingnan", "photos", "videos", "groups", "reels",
        "top fans", "following", "admin", "moderator", "unknown", "shared with",
        "news feed", "write a comment", "log in", "sign up", "notifications"
    ]
    if any(bad == lowered or lowered.startswith(bad) for bad in invalid_keywords):
        return False

    return True


def _is_valid_timestamp(text: str) -> bool:
    """Validate whether text is a plausible date/time string and NOT a hashtag or username."""
    if not text or len(text) > 75:
        return False
    t = text.strip()
    if t.startswith("#") or t.startswith("http") or t.startswith("@"):
        return False

    # Reject common UI labels
    lowered = t.lower()
    if any(bad in lowered for bad in [
        "see more", "see less", "like", "comment", "share", "follow", "facebook",
        "public", "joined", "replies", "reaction", "author", "suggested",
        "sponsored", "view more", "hide", "tan-awa", "tingnan", "unknown"
    ]):
        return False

    # Check for date / time patterns
    date_patterns = [
        r'\b(?:just now|yesterday|today|kahapon)\b',
        r'\b\d+\s*(?:m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks|y|yr|yrs|year|years)(?:\s+ago)?\b',
        r'\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?\b',
        r'\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:\s+\d{4})?\b',
        r'\b\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)?\b',
        r'\b\d{4}-\d{2}-\d{2}\b',
        r'\b(?:hulyo|agosto|setyembre|oktubre|nobiyembre|disyembre|enero|pebrero|marso|abril|mayo|hunyo)\s+\d{1,2}\b',
    ]
    return any(re.search(pat, t, re.IGNORECASE) for pat in date_patterns)


def _extract_author(driver, article) -> str:
    """Best-effort extraction of the posting author's name, avoiding hashtags."""
    # 1. Look for headings (h2, h3, h4, h5, [role="heading"])
    try:
        headings = article.find_elements(By.CSS_SELECTOR, 'h2, h3, h4, h5, [role="heading"]')
        for heading in headings:
            links = heading.find_elements(By.CSS_SELECTOR, "a")
            for link in links:
                txt = link.text.strip() or (link.get_attribute("innerText") or "").strip()
                if _is_valid_author(txt):
                    return txt
            txt = heading.text.strip() or (heading.get_attribute("innerText") or "").strip()
            first_line = txt.split("\n")[0].strip() if txt else ""
            if _is_valid_author(first_line):
                return first_line
    except Exception:
        pass

    # 2. Look for profile link in the header area (excluding hashtags/posts/photos)
    try:
        links = article.find_elements(By.CSS_SELECTOR, "a[href]")
        for link in links:
            href = link.get_attribute("href") or ""
            if any(seg in href for seg in [
                "/hashtag/", "/posts/", "/permalink", "/photo", "/video", "/watch",
                "/reel", "/events/", "/login", "/recover", "/help", "/sharer", "/friends"
            ]):
                continue

            txt = link.text.strip() or (link.get_attribute("innerText") or "").strip()
            if _is_valid_author(txt):
                return txt
            aria = (link.get_attribute("aria-label") or "").strip()
            if _is_valid_author(aria):
                return aria
    except Exception:
        pass

    # 3. Look for Profile picture alt text / aria-label
    try:
        imgs = article.find_elements(By.CSS_SELECTOR, 'img[alt*="profile" i], img[alt*="profile picture" i]')
        for img in imgs:
            alt = (img.get_attribute("alt") or "").strip()
            cleaned = re.sub(r"(?:'s\s+profile\s+picture|profile\s+picture\s+of\s+|profile\s+of\s+)", "", alt, flags=re.I).strip()
            if _is_valid_author(cleaned):
                return cleaned
    except Exception:
        pass

    return "Unknown"


def _extract_timestamp_and_url(driver, article, full_text: str = "") -> tuple[str, str]:
    """Extract publish timestamp and direct post permalink."""
    timestamp = ""
    post_url = ""

    # Strategy 1: Find post permalink and timestamp from post links
    try:
        links = article.find_elements(By.CSS_SELECTOR, "a[href]")
        for link in links:
            href = link.get_attribute("href") or ""
            if not href or "/hashtag/" in href:
                continue

            is_post_link = any(seg in href for seg in [
                "/posts/", "/permalink", "story_fbid=", "/videos/", "/watch",
                "/reel/", "photo.php", "story.php", "fbid="
            ])

            aria = (link.get_attribute("aria-label") or "").strip()
            title = (link.get_attribute("title") or "").strip()
            inner = (link.get_attribute("innerText") or link.text or "").strip()
            data_utime = link.get_attribute("data-utime")

            found_ts = ""
            if data_utime and data_utime.isdigit():
                try:
                    found_ts = datetime.fromtimestamp(int(data_utime), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
            elif _is_valid_timestamp(aria):
                found_ts = aria
            elif _is_valid_timestamp(title):
                found_ts = title
            elif _is_valid_timestamp(inner):
                found_ts = inner

            if found_ts and not timestamp:
                timestamp = found_ts

            if is_post_link and not post_url:
                post_url = _clean_facebook_url(href)

            if timestamp and post_url:
                break
    except Exception:
        pass

    # Strategy 2: Check <abbr> tags for timestamp
    if not timestamp:
        try:
            abbrs = article.find_elements(By.TAG_NAME, "abbr")
            for abbr in abbrs:
                ts = abbr.get_attribute("data-utime") or abbr.get_attribute("title") or abbr.text
                if ts:
                    if ts.isdigit():
                        try:
                            timestamp = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                            break
                        except Exception:
                            pass
                    elif _is_valid_timestamp(ts):
                        timestamp = ts.strip()
                        break
        except Exception:
            pass

    # Strategy 3: Hover over header time link to reveal Facebook tooltip
    if not timestamp or any(rel in timestamp.lower() for rel in ["hr", "min", "ago", "yesterday", "d", "h", "m", "just now"]):
        try:
            header_links = article.find_elements(
                By.CSS_SELECTOR, 'a[role="link"][href*="/posts/"], a[role="link"][href*="permalink"], span > a[role="link"]'
            )
            for hl in header_links[:2]:
                try:
                    ActionChains(driver).move_to_element(hl).perform()
                    time.sleep(0.25)
                    tooltips = driver.find_elements(By.CSS_SELECTOR, 'div[role="tooltip"], div[data-visualcompletion="tooltip-target"]')
                    for tt in tooltips:
                        t = tt.text.strip()
                        if _is_valid_timestamp(t) and any(c.isdigit() for c in t):
                            timestamp = t
                            break
                    if timestamp and not any(rel in timestamp.lower() for rel in ["ago", "hr", "min", "just now"]):
                        break
                except Exception:
                    pass
        except Exception:
            pass

    # Strategy 4: Fallback date extraction from post body text (e.g. "July 24, 2026", "Hulyo 24, 2026")
    if not timestamp and full_text:
        match = re.search(
            r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December|'
            r'Enero|Pebrero|Marso|Abril|Mayo|Hunyo|Hulyo|Agosto|Setyembre|Oktubre|Nobiyembre|Disyembre)\s+\d{1,2}(?:,?\s+\d{4})?\b',
            full_text,
            re.IGNORECASE
        )
        if match:
            timestamp = match.group(0).strip()

    return timestamp, post_url


def extract_posts_from_page(
    driver,
    seen_hashes: set[str],
    seen_urls: set[str],
    all_reports: list[FloodReport],
    json_path: Path,
    csv_path: Path,
    max_scrolls: int = 50,
) -> int:
    """Scroll the current page, extract flood-relevant posts, and save
    each one to disk the moment it is found.

    Returns the number of *new* posts found on this page.
    """

    new_count = 0
    stale_rounds = 0
    prev_total = len(all_reports)

    for scroll_i in range(max_scrolls):
        _smooth_scroll(driver, pixels=random.randint(700, 1200))
        _human_pause(2.5, 5.0)

        articles = driver.find_elements(By.CSS_SELECTOR, 'div[role="article"]')
        if not articles:
            # Fallback: try broader selectors used in some FB layouts
            articles = driver.find_elements(By.CSS_SELECTOR, '[data-ad-preview], .userContentWrapper')

        log.info(f"  scroll {scroll_i + 1}/{max_scrolls} — {len(articles)} article(s) visible  |  total saved: {len(all_reports)}")

        for article in articles:
            try:
                # Expand truncated text
                _expand_see_more(driver, article)

                full_text = _extract_text(article)
                if not full_text:
                    continue

                # Deduplicate by content hash
                text_hash = _hash_text(full_text[:500])
                if text_hash in seen_hashes:
                    continue

                # Relevance filter
                if not _is_flood_relevant(full_text):
                    continue

                # Extract metadata: Author, Timestamp, Post URL
                timestamp, post_url = _extract_timestamp_and_url(driver, article, full_text)
                author = _extract_author(driver, article)

                if post_url and post_url in seen_urls:
                    continue

                # ── Build report ──
                seen_hashes.add(text_hash)
                if post_url:
                    seen_urls.add(post_url)

                report = FloodReport(
                    post_id=text_hash[:12],
                    post_url=post_url,
                    author=author,
                    timestamp=timestamp,
                    full_text=full_text,
                    detected_locations=_detect_locations(full_text),
                    urgency_level=_classify_urgency(full_text),
                    media_urls=_extract_media_urls(article),
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                )
                all_reports.append(report)
                new_count += 1
                log.info(
                    f"    ★ [{report.urgency_level}] {report.author[:25]} | "
                    f"Date: {report.timestamp or 'N/A'} | "
                    f"Link: {report.post_url or 'N/A'}  "
                    f"(#{len(all_reports)} total)"
                )

                # ── INCREMENTAL SAVE: write to disk immediately ──
                save_incremental(all_reports, json_path, csv_path)

            except StaleElementReferenceException:
                continue
            except Exception as exc:
                log.debug(f"    ⚠ Article parse error: {exc}")
                continue

        # Staleness detection: stop scrolling if no new posts appear
        if len(all_reports) == prev_total:
            stale_rounds += 1
            if stale_rounds >= 5:
                log.info("  No new posts in 5 consecutive scrolls — stopping early.")
                break
        else:
            stale_rounds = 0
            prev_total = len(all_reports)

    return new_count

# ──────────────────────────────────────────────
# Output writers
# ──────────────────────────────────────────────

def save_json(reports: list[FloodReport], path: Path) -> None:
    """Write the full reports list to JSON (overwrites)."""
    data = [asdict(r) for r in reports]
    # Write to a temp file first, then rename — protects against
    # half-written files if power dies mid-write.
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)
    log.info(f"💾 Saved {len(reports)} reports → {path}")


def save_csv(reports: list[FloodReport], path: Path) -> None:
    """Write the full reports list to CSV (overwrites)."""
    if not reports:
        log.warning("No reports to write to CSV.")
        return

    fieldnames = [
        "post_id",
        "post_url",
        "author",
        "timestamp",
        "full_text",
        "detected_locations",
        "urgency_level",
        "media_urls",
        "scraped_at",
    ]
    tmp_path = path.with_suffix(".csv.tmp")
    with open(tmp_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in reports:
            row = asdict(r)
            # Flatten lists to semicolon-separated strings for CSV
            row["media_urls"] = "; ".join(row["media_urls"])
            row["detected_locations"] = "; ".join(row["detected_locations"])
            writer.writerow(row)
    tmp_path.replace(path)
    log.info(f"💾 Saved {len(reports)} reports → {path}")


def save_incremental(all_reports: list[FloodReport], json_path: Path, csv_path: Path) -> None:
    """Save all accumulated reports to both JSON and CSV right now."""
    if all_reports:
        save_json(all_reports, json_path)
        save_csv(all_reports, csv_path)


def load_existing_reports(json_path: Path) -> tuple[list[FloodReport], set[str], set[str]]:
    """Load previously saved reports so the scraper can resume after a
    crash, power loss, or manual stop without losing data."""
    reports: list[FloodReport] = []
    seen_hashes: set[str] = set()
    seen_urls: set[str] = set()

    if not json_path.exists():
        return reports, seen_hashes, seen_urls

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data:
            # Clean up corrupted entries from previous buggy runs
            if item.get("author", "").startswith("#"):
                item["author"] = "Unknown"
            if item.get("timestamp", "").startswith("#"):
                item["timestamp"] = ""

            report = FloodReport(**item)
            reports.append(report)
            seen_hashes.add(report.post_id)  # post_id is the hash[:12]
            # Also add the full hash of the text
            seen_hashes.add(_hash_text(report.full_text[:500]))
            if report.post_url:
                seen_urls.add(report.post_url)
        log.info(f"📂 Loaded {len(reports)} existing reports from {json_path} — will resume from where we left off.")
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        log.warning(f"⚠ Could not load existing reports ({exc}). Starting fresh.")
        reports, seen_hashes, seen_urls = [], set(), set()

    return reports, seen_hashes, seen_urls

# ──────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────

def scrape_search_queries(
    driver,
    queries: list[str],
    seen_hashes: set[str],
    seen_urls: set[str],
    max_scrolls: int,
    all_reports: list[FloodReport],
    json_path: Path,
    csv_path: Path,
) -> None:
    """Iterate through Facebook search-post URLs for each query.
    Posts are saved incrementally inside extract_posts_from_page."""

    for i, query in enumerate(queries, 1):
        url = _build_search_url(query)
        log.info(f"▶ [{i}/{len(queries)}] Searching: \"{query}\"  →  {url}")
        try:
            driver.get(url)
            _human_pause(4, 7)

            # Dismiss login overlay if present
            _dismiss_login_overlay(driver)

            new_count = extract_posts_from_page(
                driver, seen_hashes, seen_urls,
                all_reports, json_path, csv_path,
                max_scrolls,
            )
            log.info(f"  ✓ +{new_count} new posts for \"{query}\"  |  Grand total: {len(all_reports)}")

        except KeyboardInterrupt:
            log.warning("\n[!] Ctrl+C detected. Data already saved incrementally.")
            raise
        except WebDriverException as exc:
            log.error(f"  ✗ Failed on query \"{query}\": {exc}")
            # Even on failure, data so far is already on disk
        _human_pause(3, 6)





def _dismiss_login_overlay(driver) -> None:
    """Try to close modal login prompts or cookie banners."""
    selectors = [
        '[aria-label="Close"]',
        '[data-testid="cookie-policy-manage-dialog-accept-button"]',
        'div[role="dialog"] button',
    ]
    for sel in selectors:
        try:
            btns = driver.find_elements(By.CSS_SELECTOR, sel)
            for btn in btns:
                if btn.is_displayed():
                    btn.click()
                    time.sleep(0.5)
        except Exception:
            pass

# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Davao City Flood Report Scraper — Facebook",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open a visible browser window so you can log in manually. "
             "After logging in, close this script (Ctrl+C) and re-run without --login.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Run Chrome in headless mode (requires prior session via --login).",
    )
    parser.add_argument(
        "--scrolls",
        type=int,
        default=50,
        help="Number of scroll iterations per query / page (default: 50).",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=".",
        help="Directory to write output files (default: current directory).",
    )
    parser.add_argument(
        "--profile-dir",
        type=str,
        default=None,
        help="Path to the Chrome user-data-dir for session persistence. "
             "Defaults to ./fb_chrome_profile in the script's directory.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # ── Resolve paths ──
    script_dir = Path(__file__).resolve().parent
    profile_dir = args.profile_dir or str(script_dir / "fb_chrome_profile")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "davao_fb_flood_reports.json"
    csv_path = output_dir / "davao_fb_flood_reports.csv"

    log.info("=" * 60)
    log.info("  Davao City Flood Report Scraper — Facebook")
    log.info(f"  Profile dir : {profile_dir}")
    log.info(f"  Output dir  : {output_dir.resolve()}")
    log.info(f"  Scrolls/page: {args.scrolls}")
    log.info("=" * 60)

    # ── Load any previously saved reports (resume support) ──
    all_reports, seen_hashes, seen_urls = load_existing_reports(json_path)
    if all_reports:
        log.info(f"  Resuming with {len(all_reports)} previously saved reports.")

    # ── Launch browser ──
    headless = args.headless and not args.login
    driver = create_driver(profile_dir, headless=headless)

    # Register an emergency save handler so that even if the process is
    # killed (e.g. terminal closed, SIGTERM), we attempt a final save.
    def _emergency_save(*_args):
        log.warning("\n🚨 Emergency save triggered (signal/atexit)…")
        save_incremental(all_reports, json_path, csv_path)

    atexit.register(_emergency_save)
    # SIGTERM (sent when terminal is closed on some systems)
    try:
        signal.signal(signal.SIGTERM, lambda *_: (_emergency_save(), sys.exit(0)))
    except (OSError, ValueError):
        pass  # Not available on all platforms / threads

    try:
        # ── Login flow ──
        if args.login:
            log.info("Opening Facebook for manual login…")
            log.info("Log in to your account, then press Ctrl+C to close this session.")
            log.info("After that, re-run the script WITHOUT --login to scrape.")
            driver.get("https://www.facebook.com/login")
            try:
                while True:
                    time.sleep(5)
            except KeyboardInterrupt:
                log.info("Login session saved. You can now run the scraper.")
                return
        else:
            if not ensure_logged_in(driver):
                log.error(
                    "No active Facebook session found.\n"
                    "Run with --login first to authenticate:\n"
                    "    python davao_flood_scraper.py --login"
                )
                return

        # ── Search-based discovery ──
        log.info("\n━━━ Searching for Davao flood posts ━━━")
        scrape_search_queries(
            driver, SEARCH_QUERIES, seen_hashes, seen_urls,
            args.scrolls, all_reports, json_path, csv_path,
        )

        # ── Final summary ──
        log.info("\n" + "=" * 60)
        log.info(f"  Total flood reports collected: {len(all_reports)}")
        sitrep = sum(1 for r in all_reports if r.urgency_level == "SITREP")
        high = sum(1 for r in all_reports if r.urgency_level == "HIGH")
        log.info(f"  SITREP: {sitrep}  |  HIGH: {high}  |  NORMAL: {len(all_reports) - sitrep - high}")
        log.info("=" * 60)

        # One last save to be sure
        save_incremental(all_reports, json_path, csv_path)

        if not all_reports:
            log.warning("No flood-related posts were found during this run.")

    except KeyboardInterrupt:
        log.info("\n⏹ Interrupted by user. Data already saved incrementally — nothing lost.")
        save_incremental(all_reports, json_path, csv_path)
    except Exception as exc:
        log.exception(f"Unexpected error: {exc}")
        # Save whatever we have before crashing
        save_incremental(all_reports, json_path, csv_path)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        log.info(f"Browser closed. Done. Total reports on disk: {len(all_reports)}")


if __name__ == "__main__":
    main()
