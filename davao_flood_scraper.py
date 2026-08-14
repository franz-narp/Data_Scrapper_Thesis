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
import csv
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set
from urllib.parse import quote_plus, urljoin

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
    "baha", "nagbaha", "lunop", "taas ang tubig", "suba",
    "overflow", "inundated", "lapok", "baha sa davao",
    "flood", "flooding", "flooded", "tubig",
]

URGENCY_KEYWORDS: list[str] = [
    "tabang", "rescue", "stranded", "trap", "atop",
    "emergency", "tulong", "evacuation", "evacuate",
    "nalimod", "saklolo", "help", "relief",
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
    "baha",
    "baha davao",
    "baha davao city",
    "lunop davao",
    "nagbaha davao",
    "flood davao city",
    "flooding davao",
    "taas ang tubig davao",
    "lapok davao",
    # Rescue & urgency
    "rescue baha davao",
    "rescue flood davao",
    "stranded baha davao",
    "evacuation davao flood",
    "tabang baha davao",
    "tulong baha davao",
    # Area-specific searches
    "baha Matina Pangi",
    "baha Matina Crossing",
    "baha Buhangin davao",
    "baha Bangkal davao",
    "baha Bucana davao",
    "baha Tigatto davao",
    "baha Bunawan davao",
    "baha Talomo davao",
    "baha Ma-a davao",
    "flood Jade Valley davao",
    # Situational reports
    "CDRRMO davao flood",
    "davao flood update",
    "davao flood advisory",
]

# "See more" button labels (includes Bisaya/Cebuano localizations)
SEE_MORE_LABELS: list[str] = [
    "See more", "See More",
    "Tan-awa ang dugang",                                     # Cebuano
    "Tingnan ang Iba Pa",                                     # Filipino
    "Ver más",                                                # Spanish (rare)
]

# ──────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────

@dataclass
class FloodReport:
    post_id: str = ""
    timestamp: str = ""
    author: str = ""
    full_text: str = ""
    media_urls: List[str] = field(default_factory=list)
    detected_locations: List[str] = field(default_factory=list)
    urgency_level: str = "NORMAL"          # NORMAL | HIGH | SITREP
    post_url: str = ""
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

    driver = uc.Chrome(options=options, use_subprocess=True)
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


def _extract_permalink(driver, article) -> str:
    """Attempt to extract a permalink / post URL from timestamp links."""
    try:
        # Post timestamps are usually <a> links with href containing /posts/ or /permalink/
        links = article.find_elements(By.CSS_SELECTOR, "a[href]")
        for link in links:
            href = link.get_attribute("href") or ""
            if any(seg in href for seg in ["/posts/", "/permalink/", "/photo/", "/videos/", "story_fbid"]):
                # Clean tracking parameters
                clean = href.split("?")[0]
                return clean
    except Exception:
        pass
    return ""


def _extract_author(article) -> str:
    """Best-effort extraction of the posting author's name."""
    try:
        # The author name is typically the first strong/heading link inside the article
        heading = article.find_element(By.CSS_SELECTOR, "strong, h2, h3, h4")
        return heading.text.strip()
    except NoSuchElementException:
        pass
    try:
        # Fallback: first link with a user/page profile href
        links = article.find_elements(By.CSS_SELECTOR, "a[href]")
        for link in links:
            href = link.get_attribute("href") or ""
            text = link.text.strip()
            if text and len(text) > 1 and ("facebook.com/" in href) and "/posts/" not in href:
                return text
    except Exception:
        pass
    return "Unknown"


def _extract_timestamp(article) -> str:
    """Extract the human-readable timestamp string from the post."""
    try:
        # Facebook often stores timestamps in <abbr> or aria-label on timestamp links
        abbr_tags = article.find_elements(By.TAG_NAME, "abbr")
        for abbr in abbr_tags:
            ts = abbr.get_attribute("data-utime") or abbr.get_attribute("title") or abbr.text
            if ts:
                return ts.strip()
    except Exception:
        pass

    try:
        # Modern FB uses aria-label on the timestamp <a> element
        time_links = article.find_elements(
            By.CSS_SELECTOR, 'a[href*="/posts/"], a[href*="permalink"], a[role="link"]'
        )
        for tl in time_links:
            label = tl.get_attribute("aria-label") or ""
            if label and any(c.isdigit() for c in label):
                return label.strip()
            # Sometimes the visible text is the relative timestamp
            text = tl.text.strip()
            if text and any(unit in text.lower() for unit in ["ago", "hr", "min", "yesterday", "just now", "h", "m", "d"]):
                return text
    except Exception:
        pass

    return ""


def extract_posts_from_page(
    driver,
    seen_hashes: set[str],
    seen_urls: set[str],
    max_scrolls: int = 50,
) -> list[FloodReport]:
    """Scroll the current page and extract flood-relevant posts."""

    reports: list[FloodReport] = []
    stale_rounds = 0
    prev_count = 0

    for scroll_i in range(max_scrolls):
        _smooth_scroll(driver, pixels=random.randint(700, 1200))
        _human_pause(2.5, 5.0)

        articles = driver.find_elements(By.CSS_SELECTOR, 'div[role="article"]')
        if not articles:
            # Fallback: try broader selectors used in some FB layouts
            articles = driver.find_elements(By.CSS_SELECTOR, '[data-ad-preview], .userContentWrapper')

        log.info(f"  scroll {scroll_i + 1}/{max_scrolls} — {len(articles)} article(s) visible")

        for article in articles:
            try:
                # Quick identity check via permalink
                permalink = _extract_permalink(driver, article)
                if permalink and permalink in seen_urls:
                    continue

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

                # ── Build report ──
                seen_hashes.add(text_hash)
                if permalink:
                    seen_urls.add(permalink)

                report = FloodReport(
                    post_id=text_hash[:12],
                    timestamp=_extract_timestamp(article),
                    author=_extract_author(article),
                    full_text=full_text,
                    media_urls=_extract_media_urls(article),
                    detected_locations=_detect_locations(full_text),
                    urgency_level=_classify_urgency(full_text),
                    post_url=permalink,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                )
                reports.append(report)
                log.info(
                    f"    ★ [{report.urgency_level}] {report.author[:30]} — "
                    f"locations: {report.detected_locations or '—'}"
                )

            except StaleElementReferenceException:
                continue
            except Exception as exc:
                log.debug(f"    ⚠ Article parse error: {exc}")
                continue

        # Staleness detection: stop scrolling if no new posts appear
        if len(reports) == prev_count:
            stale_rounds += 1
            if stale_rounds >= 5:
                log.info("  No new posts in 5 consecutive scrolls — stopping early.")
                break
        else:
            stale_rounds = 0
            prev_count = len(reports)

    return reports

# ──────────────────────────────────────────────
# Output writers
# ──────────────────────────────────────────────

def save_json(reports: list[FloodReport], path: Path) -> None:
    data = [asdict(r) for r in reports]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(f"Saved {len(reports)} reports → {path}")


def save_csv(reports: list[FloodReport], path: Path) -> None:
    if not reports:
        log.warning("No reports to write to CSV.")
        return

    fieldnames = list(asdict(reports[0]).keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in reports:
            row = asdict(r)
            # Flatten lists to semicolon-separated strings for CSV
            row["media_urls"] = "; ".join(row["media_urls"])
            row["detected_locations"] = "; ".join(row["detected_locations"])
            writer.writerow(row)
    log.info(f"Saved {len(reports)} reports → {path}")

# ──────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────

def scrape_search_queries(
    driver,
    queries: list[str],
    seen_hashes: set[str],
    seen_urls: set[str],
    max_scrolls: int,
) -> list[FloodReport]:
    """Iterate through Facebook search-post URLs for each query."""
    all_reports: list[FloodReport] = []

    for query in queries:
        url = _build_search_url(query)
        log.info(f"▶ Searching: \"{query}\"  →  {url}")
        try:
            driver.get(url)
            _human_pause(4, 7)

            # Sometimes Facebook shows a "Log In" overlay on search pages;
            # dismiss it if present.
            _dismiss_login_overlay(driver)

            reports = extract_posts_from_page(driver, seen_hashes, seen_urls, max_scrolls)
            all_reports.extend(reports)
            log.info(f"  ✓ Collected {len(reports)} new posts for query \"{query}\"")
        except WebDriverException as exc:
            log.error(f"  ✗ Failed on query \"{query}\": {exc}")
        _human_pause(3, 6)

    return all_reports





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

    # ── Launch browser ──
    headless = args.headless and not args.login
    driver = create_driver(profile_dir, headless=headless)

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

        # ── Deduplication state ──
        seen_hashes: set[str] = set()
        seen_urls: set[str] = set()
        all_reports: list[FloodReport] = []

        # ── Search-based discovery ──
        log.info("\n━━━ Searching for Davao flood posts ━━━")
        search_reports = scrape_search_queries(
            driver, SEARCH_QUERIES, seen_hashes, seen_urls, args.scrolls
        )
        all_reports.extend(search_reports)

        # ── Summary & save ──
        log.info("\n" + "=" * 60)
        log.info(f"  Total flood reports collected: {len(all_reports)}")
        sitrep = sum(1 for r in all_reports if r.urgency_level == "SITREP")
        high = sum(1 for r in all_reports if r.urgency_level == "HIGH")
        log.info(f"  SITREP: {sitrep}  |  HIGH: {high}  |  NORMAL: {len(all_reports) - sitrep - high}")
        log.info("=" * 60)

        if all_reports:
            save_json(all_reports, json_path)
            save_csv(all_reports, csv_path)
        else:
            log.warning("No flood-related posts were found during this run.")

    except KeyboardInterrupt:
        log.info("\nInterrupted by user. Saving partial results…")
        if "all_reports" in dir() and all_reports:
            save_json(all_reports, json_path)
            save_csv(all_reports, csv_path)
    except Exception as exc:
        log.exception(f"Unexpected error: {exc}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        log.info("Browser closed. Done.")


if __name__ == "__main__":
    main()
