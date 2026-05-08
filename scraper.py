"""
scraper.py — Multi-platform job scraper
========================================
Scrapes LinkedIn, Bayt.com, and Jobstreet for matching job listings,
applies exclusion filters, deduplicates, and inserts results into the
resume tailor DB as status='queued' for batch processing.

Usage:
    source venv/bin/activate
    python3 scraper.py                          # all platforms
    python3 scraper.py --platforms bayt         # MENA only (no auth needed)
    python3 scraper.py --platforms linkedin     # opens visible browser; logs in if needed
    python3 scraper.py --dry-run                # preview without inserting
"""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import json
import logging
import random
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from urllib.parse import urlparse, urlunparse, unquote

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class ScraperConfig:
    platforms: list[str]
    locations: dict[str, list[str]]
    categories: dict[str, list[str]]
    max_per_platform: "int | dict[str, int]"   # int (global) or {platform: n}
    exclude_companies: list[str]
    exclude_title_keywords: list[str]
    linkedin_user_data_dir: str
    db_path: str
    delay_min: float
    delay_max: float
    max_per_query: int = 5          # max JD fetches per query-location combination
    platform_timeout_secs: int = 900  # per-platform hard timeout

    @classmethod
    def from_file(cls, path: str) -> "ScraperConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)

    def get_max_per_platform(self, platform: str) -> int:
        """Return the platform-specific cap, falling back to global value."""
        if isinstance(self.max_per_platform, dict):
            return self.max_per_platform.get(platform, max(self.max_per_platform.values()))
        return self.max_per_platform

    def delay(self) -> None:
        time.sleep(random.uniform(self.delay_min, self.delay_max))

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class JobListing:
    job_url: str
    title: str
    company: str
    location: str
    jd_text: str
    source: str      # "bayt" | "jobstreet" | "linkedin"
    category: str
    relevance_score: float = 0.0   # set by orchestrator before insertion; used for ranking

# ---------------------------------------------------------------------------
# Filter engine
# ---------------------------------------------------------------------------

class FilterEngine:
    """Implements all exclusion rules from skills/references/filtering.md."""

    def __init__(self, cfg: ScraperConfig) -> None:
        self._exc_companies = [c.lower() for c in cfg.exclude_companies]
        self._exc_titles    = [t.lower() for t in cfg.exclude_title_keywords]

    def _company_excluded(self, company: str) -> bool:
        cl = company.lower()
        return any(ec in cl for ec in self._exc_companies)

    def _title_excluded(self, title: str) -> bool:
        tl = title.lower()
        for kw in self._exc_titles:
            kw = kw.strip()
            if not kw:
                continue
            # "contract" only excluded when in *title* (not company name)
            # already handled since we only call this on titles
            if kw == "entry level":
                if "entry level" in tl and "senior" not in tl:
                    return True
                continue
            if kw in tl:
                return True
        return False

    _WORKTYPE_SIGNALS = ["part-time", "part time", "freelance basis", "volunteer role"]

    def _jd_worktype_excluded(self, jd_text: str) -> bool:
        """Catch part-time/freelance signals in JD text that don't appear in the title."""
        window = jd_text[:2000].lower()
        return any(sig in window for sig in self._WORKTYPE_SIGNALS)

    def is_filtered(self, listing: JobListing) -> tuple[bool, str]:
        """Returns (filtered_out, reason)."""
        if self._company_excluded(listing.company):
            return True, f"excluded company: {listing.company}"
        if self._title_excluded(listing.title):
            return True, f"excluded title keyword in: {listing.title}"
        if not listing.jd_text or len(listing.jd_text.strip()) < 100:
            return True, "JD too short or empty"
        if self._jd_worktype_excluded(listing.jd_text):
            return True, "JD indicates part-time/freelance work type"
        return False, ""

# ---------------------------------------------------------------------------
# Deduplicator
# ---------------------------------------------------------------------------

class DeduplicatorDB:
    """Loads existing job_urls from DB on init; tracks seen URLs in-memory."""

    def __init__(self, db_path: str) -> None:
        self._seen: set[str] = set()
        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT job_url FROM applications WHERE job_url IS NOT NULL"
            ).fetchall()
            conn.close()
            self._seen = {self._norm(r[0]) for r in rows if r[0]}
            log.info("Deduplicator loaded %d existing job URLs from DB", len(self._seen))
        except sqlite3.OperationalError:
            pass  # DB or column doesn't exist yet — first run

    def _norm(self, url: str) -> str:
        """Normalise URL: unquote percent-encoding, strip query/fragment, lowercase, strip www., trailing slash."""
        try:
            url = unquote(url.strip())
            p = urlparse(url.lower())
            host = p.netloc.removeprefix("www.")
            path = p.path.rstrip("/")
            return urlunparse((p.scheme, host, path, "", "", ""))
        except Exception:
            return url.lower().strip()

    def is_duplicate(self, url: str) -> bool:
        return self._norm(url) in self._seen

    def mark_seen(self, url: str) -> None:
        self._seen.add(self._norm(url))

# ---------------------------------------------------------------------------
# Relevance pre-filter
# ---------------------------------------------------------------------------

_STOPWORDS = {
    # 4+ letter noise
    "this", "that", "with", "from", "will", "have", "your", "their",
    "they", "been", "able", "also", "such", "more", "than", "into",
    "other", "some", "when", "what", "work", "team", "role", "must",
    "skills", "experience", "required", "including", "within",
    # 2-3 letter noise (added to support [a-z]{2,} regex)
    "to", "in", "of", "or", "an", "is", "it", "be", "as", "at",
    "by", "we", "do", "if", "no", "on", "up", "so", "go", "me",
    "my", "he", "we", "us", "am", "are", "was", "the", "and",
    "but", "for", "not", "you", "all", "any", "can", "has", "had",
    "him", "his", "her", "its", "our", "out", "own", "per",
    "etc", "via", "non", "new", "one", "two", "use", "get",
    "may", "key", "own", "set", "run", "day", "way", "big",
}

class RelevanceFilter:
    """
    Lightweight pre-queue filter: checks keyword overlap between a job's JD
    and the vocabulary extracted from the master resume. Jobs with < MIN_OVERLAP_RATIO
    of the vocabulary appearing in the JD are rejected before entering the pipeline.

    Conservative threshold (20%) — the LLM pipeline handles precise matching.
    This is a coarse gate for clearly irrelevant listings (e.g. maritime logistics
    scraped under a 'data analyst' search).

    Fails open: if master_resume.md is unreadable, all jobs pass.
    """
    MIN_OVERLAP_RATIO = 0.06  # 6% of the JD's unique words must appear in resume vocab

    def __init__(self, vocab_path: str = "master_resume.md") -> None:
        self._vocab: set[str] = set()
        try:
            text = Path(vocab_path).read_text(encoding="utf-8").lower()
            self._vocab = {
                w for w in re.findall(r"[a-z]{2,}", text)
                if w not in _STOPWORDS
            }
            log.info("RelevanceFilter: loaded %d vocab words from %s", len(self._vocab), vocab_path)
        except Exception as e:
            log.warning("RelevanceFilter: could not load vocab from %s: %s — all jobs pass", vocab_path, e)

    def is_relevant(self, jd_text: str) -> tuple[bool, float]:
        """Returns (relevant, overlap_ratio). Always returns True if vocab unavailable.

        Ratio = (resume vocab ∩ JD words) / JD unique words.
        Asks: what fraction of the JD's content words appear in the resume?
        This is stable regardless of resume length, and works with short LinkedIn JDs.
        """
        if not self._vocab:
            return True, 1.0
        jd_words = {w for w in re.findall(r"[a-z]{2,}", jd_text.lower()) if w not in _STOPWORDS}
        if not jd_words:
            return True, 1.0
        ratio = len(self._vocab & jd_words) / len(jd_words)
        return ratio >= self.MIN_OVERLAP_RATIO, ratio


# ---------------------------------------------------------------------------
# Navigation helper
# ---------------------------------------------------------------------------

def _goto_with_retry(page, url: str, retries: int = 3, base_wait: float = 10.0, **kwargs) -> None:
    """Navigate with exponential backoff retry on failure (10s → 20s → 40s).
    Also waits for Cloudflare JS challenges to resolve (title "Just a moment...")."""
    cf_phrases = ("just a moment", "checking your browser", "enable javascript")
    for attempt in range(retries):
        try:
            page.goto(url, **kwargs)
            # If Cloudflare challenge detected, wait for the JS redirect to complete
            if any(p in (page.title() or "").lower() for p in cf_phrases):
                log.debug("Cloudflare challenge detected — waiting for redirect...")
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    page.wait_for_function(
                        "() => !document.title.toLowerCase().includes('just a moment') "
                        "&& !document.title.toLowerCase().includes('checking your browser')",
                        timeout=15000,
                    )
                except Exception:
                    pass  # best-effort; extraction will return empty and loop will break
            return
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = base_wait * (2 ** attempt)
            log.warning("Navigation failed (attempt %d/%d): %s — retrying in %.0fs",
                        attempt + 1, retries, e, wait)
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Bayt.com scraper (requests + BeautifulSoup, no browser)
# ---------------------------------------------------------------------------

class BaytScraper:
    """
    Scrapes Bayt.com using Playwright headless Chromium.
    Plain HTTP requests get 403 from Bayt's bot protection; a real browser context
    passes cleanly. JSON-LD structured data is still used for extraction.
    """

    COUNTRY_SLUGS = {
        "uae": "uae", "dubai": "uae",
        "saudi-arabia": "saudi-arabia", "ksa": "saudi-arabia",
        "qatar": "qatar",
        "bahrain": "bahrain",
        "kuwait": "kuwait",
        "oman": "oman",
        "egypt": "egypt",
        "jordan": "jordan",
    }

    CATEGORY_SLUGS = {
        # data_analyst
        "data analyst":                  "data-analyst",
        "business intelligence analyst": "business-intelligence",
        "power bi analyst":              "business-intelligence",
        "analytics engineer":            "data-analyst",
        "bi developer":                  "business-intelligence",
        "reporting analyst":             "data-analyst",
        # cybersecurity
        "cybersecurity analyst":         "information-security",
        "soc analyst":                   "information-security",
        "grc analyst":                   "information-security",
        "information security analyst":  "information-security",
        "threat intelligence analyst":   "information-security",
        "cloud security analyst":        "information-security",
        "iam analyst":                   "information-security",
        "siem analyst":                  "information-security",
        # dlp_governance
        "dlp analyst":                   "information-security",
        "data loss prevention analyst":  "information-security",
        "data security analyst":         "information-security",
        "data governance analyst":       "data-analyst",
        "microsoft purview analyst":     "information-security",
        "information governance analyst":"information-security",
        # risk_analyst
        "risk analyst":                  "risk-management",
        "compliance analyst":            "risk-management",
        "privacy analyst":               "risk-management",
        "data protection analyst":       "risk-management",
        "pdpa analyst":                  "risk-management",
        # business_analyst
        "business analyst":              "business-analyst",
        "strategy analyst":              "business-analyst",
        "process analyst":               "business-analyst",
        "product analyst":               "business-analyst",
    }

    def __init__(self, cfg: ScraperConfig) -> None:
        self.cfg = cfg

    def scrape(self) -> list[JobListing]:
        try:
            from playwright.sync_api import sync_playwright
            from bs4 import BeautifulSoup
            self._BS = BeautifulSoup
        except ImportError:
            log.error("playwright / beautifulsoup4 not installed")
            raise

        results: list[JobListing] = []
        countries = self.cfg.locations.get("bayt", [])

        # Use a persistent profile so Cloudflare cookies accumulate across runs.
        # headless=False is required — Cloudflare fingerprints headless Chromium.
        profile_dir = Path(self.cfg.db_path).parent / "bayt_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        log.info("[Bayt] using persistent profile at %s (non-headless, Cloudflare bypass)", profile_dir)

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36",
            )
            page = ctx.new_page()

            for country in countries:
                slug = self.COUNTRY_SLUGS.get(country.lower(), country.lower())
                # Dedup key is (cat_name, cat_slug) — different categories may share
                # the same Bayt URL slug but each category deserves its own scrape run.
                seen_slugs: set[tuple[str, str]] = set()

                for cat_name, queries in self.cfg.categories.items():
                    for query in queries:
                        cat_slug = self.CATEGORY_SLUGS.get(query.lower())
                        if not cat_slug or (cat_name, cat_slug) in seen_slugs:
                            continue
                        seen_slugs.add((cat_name, cat_slug))

                        log.info("[Bayt] %s / %s — fetching...", country, query)
                        try:
                            listings = self._scrape_category(page, slug, cat_slug, cat_name)
                            results.extend(listings)
                            log.info("[Bayt] %s / %s — found %d jobs", country, query, len(listings))
                        except Exception as e:
                            log.warning("[Bayt] %s / %s — failed: %s", country, query, e)

            ctx.close()

        return results

    def _scrape_category(self, page, country: str, cat_slug: str, category: str) -> list[JobListing]:
        listings: list[JobListing] = []
        pg = 1

        while len(listings) < self.cfg.max_per_query:
            url = f"https://www.bayt.com/en/{country}/jobs/{cat_slug}-jobs/?page={pg}"
            try:
                _goto_with_retry(page, url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                log.warning("[Bayt] page %d navigation failed after retries: %s", pg, e)
                break

            html = page.content()
            soup = self._BS(html, "html.parser")
            job_urls = self._extract_job_urls(soup)

            if not job_urls:
                break

            for job_url in job_urls:
                if len(listings) >= self.cfg.max_per_query:
                    break
                self.cfg.delay()
                try:
                    listing = self._scrape_job(page, job_url, category)
                    if listing:
                        listings.append(listing)
                except Exception as e:
                    log.debug("[Bayt] job detail failed %s: %s", job_url, e)

            pg += 1
            self.cfg.delay()

        return listings

    def _extract_job_urls(self, soup) -> list[str]:
        urls: list[str] = []

        # JSON-LD ItemList (most reliable)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, list):
                    data = data[0]
                if data.get("@type") == "ItemList":
                    for item in data.get("itemListElement", []):
                        u = item.get("url") or (item.get("item") or {}).get("url")
                        if u:
                            urls.append(u)
                    if urls:
                        return urls
            except Exception:
                pass

        # Fallback: heading anchor tags
        for a in soup.select("h2 a[href*='/jobs/']"):
            href = a.get("href", "")
            if href.startswith("/"):
                href = "https://www.bayt.com" + href
            if href and href not in urls:
                urls.append(href)

        return urls

    def _scrape_job(self, page, url: str, category: str) -> "JobListing | None":
        _goto_with_retry(page, url, timeout=30000, wait_until="domcontentloaded")
        soup = self._BS(page.content(), "html.parser")

        title, company, location, jd_text = "", "", "", ""

        # JSON-LD JobPosting
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, list):
                    data = data[0]
                if data.get("@type") == "JobPosting":
                    title   = data.get("title", "")
                    company = (data.get("hiringOrganization") or {}).get("name", "")
                    loc     = (data.get("jobLocation") or {}).get("address", {})
                    if isinstance(loc, dict):
                        location = loc.get("addressLocality", "") or loc.get("addressCountry", "")
                    jd_html = data.get("description", "")
                    if jd_html:
                        jd_text = self._BS(jd_html, "html.parser").get_text(separator="\n").strip()
                    break
            except Exception:
                pass

        # DOM fallbacks
        if not title:
            t = soup.select_one("h1[class*='title'], h1[class*='job']")
            title = t.get_text(strip=True) if t else ""
        if not company:
            for sel in [
                "[class*='company-name']",
                "[itemprop='name']",
                "a[href*='/companies/']",
                "[class*='company'] a",
                "[class*='employer'] a",
                "[class*='company']",
                "[class*='employer']",
            ]:
                c = soup.select_one(sel)
                if c:
                    text = c.get_text(strip=True)
                    if text:
                        company = text
                        break
        if not jd_text:
            d = soup.select_one("[class*='description'], [class*='job-content']")
            jd_text = d.get_text(separator="\n", strip=True) if d else ""

        if not title or not jd_text:
            return None
        if not company:
            log.warning("[Bayt] could not extract company for '%s' — %s", title, url)

        return JobListing(
            job_url=url, title=title, company=company,
            location=str(location), jd_text=jd_text,
            source="bayt", category=category,
        )


# ---------------------------------------------------------------------------
# Jobstreet scraper (Playwright headless)
# ---------------------------------------------------------------------------

class JobstreetScraper:
    """Scrapes Jobstreet Malaysia using Playwright."""

    def __init__(self, cfg: ScraperConfig) -> None:
        self.cfg = cfg

    def scrape(self) -> list[JobListing]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error("playwright not installed")
            raise

        results: list[JobListing] = []
        locations = self.cfg.locations.get("jobstreet", ["Malaysia"])

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36"
            })

            for cat_name, queries in self.cfg.categories.items():
                for query in queries:
                    for loc in locations:
                        log.info("[Jobstreet] %s in %s — fetching...", query, loc)
                        try:
                            listings = self._scrape_query(page, query, loc, cat_name)
                            results.extend(listings)
                            log.info("[Jobstreet] %s in %s — found %d jobs", query, loc, len(listings))
                        except Exception as e:
                            log.warning("[Jobstreet] %s in %s — failed: %s", query, loc, e)

            browser.close()

        return results

    def _slug(self, text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

    def _scrape_query(self, page, query: str, location: str, category: str) -> list[JobListing]:
        listings: list[JobListing] = []
        pg = 1

        while len(listings) < self.cfg.max_per_query:
            q_slug  = self._slug(query)
            loc_slug = self._slug(location)
            url = f"https://my.jobstreet.com/{q_slug}-jobs/in-{loc_slug}?pg={pg}"

            try:
                _goto_with_retry(page, url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                log.warning("[Jobstreet] listing page failed after retries: %s", e)
                break
            try:
                page.wait_for_selector("article", timeout=15000)
            except Exception:
                break

            cards = page.query_selector_all("article")
            if not cards:
                break

            job_links: list[dict] = []
            for card in cards:
                try:
                    a = card.query_selector("a[data-automation='jobTitle']")
                    if not a:
                        continue
                    title   = a.inner_text().strip()
                    href    = a.get_attribute("href") or ""
                    if not href.startswith("http"):
                        href = "https://my.jobstreet.com" + href
                    href = href.split("?")[0]  # strip tracking params

                    co_el  = card.query_selector("[data-automation='jobCompany']")
                    loc_el = card.query_selector("[data-automation='jobLocation']")
                    company  = co_el.inner_text().strip()  if co_el  else ""
                    location_text = loc_el.inner_text().strip() if loc_el else location

                    job_links.append({"title": title, "company": company,
                                      "location": location_text, "url": href})
                except Exception:
                    continue

            for job in job_links:
                if len(listings) >= self.cfg.max_per_query:
                    break
                self.cfg.delay()
                try:
                    jd_text = self._fetch_jd(page, job["url"])
                    company = job["company"] or self._extract_company_from_page(page)
                    if not company:
                        log.warning("[Jobstreet] could not extract company for '%s' — %s", job["title"], job["url"])
                    if jd_text:
                        listings.append(JobListing(
                            job_url=job["url"], title=job["title"],
                            company=company, location=job["location"],
                            jd_text=jd_text, source="jobstreet", category=category,
                        ))
                except Exception as e:
                    log.debug("[Jobstreet] JD fetch failed %s: %s", job["url"], e)

            pg += 1
            self.cfg.delay()

        return listings

    def _fetch_jd(self, page, url: str) -> str:
        _goto_with_retry(page, url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("[data-automation='jobAdDetails']", timeout=10000)
            el = page.query_selector("[data-automation='jobAdDetails']")
            return el.inner_text().strip() if el else ""
        except Exception:
            # Fallback: full page text
            return page.evaluate("document.body.innerText")[:8000]

    def _extract_company_from_page(self, page) -> str:
        """Extract company name from current detail page when card-level extraction failed."""
        # JSON-LD JobPosting is most reliable when available
        try:
            for script in page.query_selector_all("script[type='application/ld+json']"):
                try:
                    data = json.loads(script.inner_text() or "")
                    if data.get("@type") == "JobPosting":
                        name = (data.get("hiringOrganization") or {}).get("name", "")
                        if name:
                            return name
                except Exception:
                    pass
        except Exception:
            pass

        # DOM selector fallbacks
        for selector in [
            "[data-automation='jobCompany']",
            "[data-automation='advertiser']",
            "[data-automation='job-detail-company-name']",
            "[class*='companyName']",
            "[class*='company-name']",
        ]:
            try:
                el = page.query_selector(selector)
                if el:
                    text = el.inner_text().strip()
                    if text:
                        return text
            except Exception:
                pass
        return ""


# ---------------------------------------------------------------------------
# LinkedIn scraper (Playwright with persistent user session)
# ---------------------------------------------------------------------------

class LinkedInScraper:
    """
    Scrapes LinkedIn Jobs using a persistent Playwright profile stored in
    .linkedin_profile/ (inside the project directory).  This avoids conflicts
    with a running Chrome instance that would lock the real Chrome profile.

    Flow on every run:
      1. Open the persistent context (headless=False so the window is visible
         during the login check — it closes automatically once scraping starts).
      2. Navigate to /feed/. If already logged in, proceed headlessly.
      3. If redirected to login/authwall, keep the window open and wait up to
         LOGIN_TIMEOUT_SECS for the user to log in manually, then continue.
    """

    LOGIN_TIMEOUT_SECS = 120  # how long to wait for manual login

    def __init__(self, cfg: ScraperConfig) -> None:
        self.cfg      = cfg
        # Dedicated profile dir inside the project — never conflicts with Chrome
        self.profile_dir = Path(__file__).parent / ".linkedin_profile"
        self.profile_dir.mkdir(exist_ok=True)

    def scrape(self) -> list[JobListing]:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            log.error("playwright not installed")
            raise

        results: list[JobListing] = []
        locations = self.cfg.locations.get("linkedin", ["Malaysia"])

        with sync_playwright() as p:
            # Always launch visible so the user can see/interact if login is needed.
            # The window stays open throughout scraping — that's fine.
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.new_page()

            # ── Login check ──────────────────────────────────────────────────
            log.info("[LinkedIn] Checking login state...")
            try:
                page.goto("https://www.linkedin.com/feed/",
                          timeout=30000, wait_until="domcontentloaded")
            except Exception:
                pass

            if "linkedin.com/feed" not in page.url:
                log.info("[LinkedIn] Not logged in — navigating to login page.")
                log.info("[LinkedIn] Please log in within %d seconds.", self.LOGIN_TIMEOUT_SECS)
                try:
                    page.goto("https://www.linkedin.com/login",
                              timeout=15000, wait_until="domcontentloaded")
                    # Wait until the browser lands on the feed (login completed)
                    page.wait_for_url(
                        "**/feed/**",
                        timeout=self.LOGIN_TIMEOUT_SECS * 1000,
                    )
                    log.info("[LinkedIn] Login successful — starting scrape.")
                except PWTimeout:
                    log.error("[LinkedIn] Login timed out after %d seconds — skipping.",
                              self.LOGIN_TIMEOUT_SECS)
                    ctx.close()
                    return []
            else:
                log.info("[LinkedIn] Already logged in — starting scrape.")

            for cat_name, queries in self.cfg.categories.items():
                for query in queries:
                    for loc in locations:
                        log.info("[LinkedIn] %s in %s — fetching...", query, loc)
                        try:
                            listings = self._scrape_query(page, query, loc, cat_name)
                            results.extend(listings)
                            log.info("[LinkedIn] %s in %s — found %d jobs", query, loc, len(listings))
                        except Exception as e:
                            log.warning("[LinkedIn] %s in %s — failed: %s", query, loc, e)

            ctx.close()

        return results

    def _dismiss_modals(self, page) -> None:
        for selector in [
            "button[aria-label='Dismiss']",
            "button[aria-label='Close']",
            "[data-tracking-control-name='public_jobs_contextual-sign-in-modal_modal_dismiss']",
        ]:
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(500)
            except Exception:
                pass

    def _scrape_query(self, page, query: str, location: str, category: str) -> list[JobListing]:
        listings: list[JobListing] = []
        start = 0

        while len(listings) < self.cfg.max_per_query:
            from urllib.parse import quote_plus
            url = (
                f"https://www.linkedin.com/jobs/search/"
                f"?keywords={quote_plus(query)}"
                f"&location={quote_plus(location)}"
                f"&f_E=3%2C4"        # mid-senior + associate
                f"&f_TPR=r2592000"   # past month
                f"&f_JT=F"           # full-time
                f"&start={start}"
            )

            try:
                _goto_with_retry(page, url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                log.warning("[LinkedIn] search page failed after retries: %s", e)
                break
            self._dismiss_modals(page)
            page.wait_for_timeout(2000)

            # Check if we got redirected to login
            if "linkedin.com/login" in page.url or "linkedin.com/authwall" in page.url:
                log.error("[LinkedIn] Redirected to login — not authenticated. "
                          "Run with --platforms linkedin first to log in.")
                return listings

            cards = page.query_selector_all(".job-card-container")
            if not cards:
                break

            job_links: list[dict] = []
            for card in cards:
                try:
                    a = card.query_selector(".job-card-list__title, a.job-card-container__link")
                    if not a:
                        continue
                    # Fix LinkedIn title-doubling bug
                    raw_title = a.inner_text().strip()
                    title = re.sub(r"(.{10,}?)\s*\1", r"\1", raw_title).strip()

                    co_el  = card.query_selector(".job-card-container__company-name, .artdeco-entity-lockup__subtitle")
                    loc_el = card.query_selector(".job-card-container__metadata-item")
                    company  = co_el.inner_text().strip()  if co_el  else ""
                    loc_text = loc_el.inner_text().strip()  if loc_el  else location

                    href = a.get_attribute("href") or ""
                    if not href.startswith("http"):
                        href = "https://www.linkedin.com" + href
                    href = href.split("?")[0]

                    job_links.append({"title": title, "company": company,
                                      "location": loc_text, "url": href})
                except Exception:
                    continue

            for job in job_links:
                if len(listings) >= self.cfg.max_per_query:
                    break
                self.cfg.delay()
                try:
                    jd_text = self._fetch_jd(page, job["url"])
                    company = job["company"] or self._extract_company_from_page(page)
                    if not company:
                        log.warning("[LinkedIn] could not extract company for '%s' — %s", job["title"], job["url"])
                    if jd_text:
                        listings.append(JobListing(
                            job_url=job["url"], title=job["title"],
                            company=company, location=job["location"],
                            jd_text=jd_text, source="linkedin", category=category,
                        ))
                except Exception as e:
                    log.debug("[LinkedIn] JD fetch failed %s: %s", job["url"], e)

            start += 25
            self.cfg.delay()

        return listings

    def _fetch_jd(self, page, url: str) -> str:
        _goto_with_retry(page, url, timeout=30000, wait_until="domcontentloaded")
        self._dismiss_modals(page)

        # Click "Show more" if present
        try:
            btn = page.query_selector("button.show-more-less-html__button--more")
            if btn:
                btn.click()
                page.wait_for_timeout(500)
        except Exception:
            pass

        for selector in [
            ".description__text",
            ".show-more-less-html__markup",
            "[class*='description']",
        ]:
            try:
                el = page.query_selector(selector)
                if el:
                    text = el.inner_text().strip()
                    if len(text) > 200:
                        return text
            except Exception:
                pass

        return page.evaluate("document.body.innerText")[:8000]

    def _extract_company_from_page(self, page) -> str:
        """Extract company name from current LinkedIn job detail page."""
        for selector in [
            ".jobs-unified-top-card__company-name a",
            ".jobs-unified-top-card__company-name",
            ".job-details-jobs-unified-top-card__company-name a",
            ".job-details-jobs-unified-top-card__company-name",
            ".topcard__org-name-link",
            ".topcard__flavor a",
        ]:
            try:
                el = page.query_selector(selector)
                if el:
                    text = el.inner_text().strip()
                    if text:
                        return text
            except Exception:
                pass
        return ""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class ScraperOrchestrator:
    PLATFORM_PRIORITY = {"linkedin": 1, "bayt": 2, "jobstreet": 3}

    def __init__(self, cfg: ScraperConfig, dry_run: bool = False) -> None:
        self.cfg       = cfg
        self.dry_run   = dry_run
        self.filter    = FilterEngine(cfg)
        self.dedup     = DeduplicatorDB(cfg.db_path)
        self.relevance = RelevanceFilter()

    def run(self) -> None:
        all_listings: list[JobListing] = []

        for platform in self.cfg.platforms:
            scraper = self._get_scraper(platform)
            if scraper is None:
                continue
            log.info("=== Scraping %s ===", platform.upper())

            # Run each platform with a hard timeout so one stuck browser can't block the others.
            # IMPORTANT: do NOT use `with ThreadPoolExecutor() as ex:` here — that context manager
            # calls shutdown(wait=True) on __exit__, which blocks until the thread finishes even
            # after a TimeoutError.  We use shutdown(wait=False) so a timed-out scraper thread
            # dies in the background (it's a daemon thread) without stalling the orchestrator.
            timeout = self.cfg.platform_timeout_secs
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = ex.submit(scraper.scrape)
            try:
                raw = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                log.error("[%s] scrape timed out after %ds — skipping platform",
                          platform, timeout)
                ex.shutdown(wait=False)
                continue
            except Exception as e:
                log.error("[%s] scraper failed: %s", platform, e)
                ex.shutdown(wait=False)
                continue
            ex.shutdown(wait=False)

            filtered: list[JobListing] = []
            irrelevant = 0
            for listing in raw:
                excluded, reason = self.filter.is_filtered(listing)
                if excluded:
                    log.info("[%s] filtered out '%s' @ %s: %s", platform, listing.title, listing.company, reason)
                    continue
                relevant, ratio = self.relevance.is_relevant(listing.jd_text)
                if not relevant:
                    log.info("[%s] relevance filtered '%s' @ %s (overlap %.0f%%)",
                             platform, listing.title, listing.company, ratio * 100)
                    irrelevant += 1
                    continue
                listing.relevance_score = ratio
                filtered.append(listing)

            new = [l for l in filtered if not self.dedup.is_duplicate(l.job_url)]

            # Rank best-first, then cap — ensures top-quality jobs from all categories
            new.sort(key=lambda l: l.relevance_score, reverse=True)
            new = new[:self.cfg.get_max_per_platform(platform)]

            log.info("[%s] %d scraped → %d after filters (%d irrelevant) → %d new",
                     platform, len(raw), len(filtered), irrelevant, len(new))
            all_listings.extend(new)

        # Cross-platform dedup (fuzzy title+company match)
        all_listings = self._cross_platform_dedup(all_listings)

        if self.dry_run:
            log.info("DRY RUN — would insert %d jobs:", len(all_listings))
            for l in all_listings:
                print(f"  [{l.source}] {l.company} — {l.title} ({l.location})")
            return

        self._insert_to_db(all_listings)
        log.info("Done. %d new jobs added to queue.", len(all_listings))

    def _get_scraper(self, platform: str):
        if platform == "bayt":
            return BaytScraper(self.cfg)
        elif platform == "jobstreet":
            return JobstreetScraper(self.cfg)
        elif platform == "linkedin":
            return LinkedInScraper(self.cfg)
        else:
            log.warning("Unknown platform: %s — skipping", platform)
            return None

    def _cross_platform_dedup(self, listings: list[JobListing]) -> list[JobListing]:
        """Remove duplicates across platforms; keep highest-priority source."""
        kept: list[JobListing] = []
        for listing in sorted(listings, key=lambda l: self.PLATFORM_PRIORITY.get(l.source, 9)):
            is_dup = False
            key = f"{listing.title.lower()} {listing.company.lower()}"
            for k in kept:
                other_key = f"{k.title.lower()} {k.company.lower()}"
                ratio = difflib.SequenceMatcher(None, key, other_key).ratio()
                if ratio > 0.85:
                    is_dup = True
                    log.debug("Cross-platform dup: '%s @ %s' ~ '%s @ %s' (%.0f%%)",
                              listing.title, listing.company, k.title, k.company, ratio * 100)
                    break
            if not is_dup:
                kept.append(listing)
        return kept

    def _insert_to_db(self, listings: list[JobListing]) -> None:
        conn = sqlite3.connect(self.cfg.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        now = datetime.now(timezone.utc).isoformat()
        inserted = 0
        for l in listings:
            app_id = str(uuid.uuid4())
            try:
                conn.execute(
                    """INSERT INTO applications
                       (id, company, role, jd_text, status, job_url, source, category, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (app_id, l.company, l.title, l.jd_text,
                     "queued", l.job_url, l.source, l.category, now, now),
                )
                self.dedup.mark_seen(l.job_url)
                inserted += 1
            except sqlite3.IntegrityError as e:
                log.warning("Insert failed for %s: %s", l.job_url, e)
        conn.commit()
        conn.close()
        log.info("Inserted %d jobs into DB.", inserted)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Job scraper — finds matching listings and queues them for resume tailoring",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config",    default="scraper_config.json", help="Config file path")
    parser.add_argument("--platforms", nargs="+",
                        help="Override platforms from config (e.g. --platforms bayt jobstreet)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Print findings without inserting into DB")
    args = parser.parse_args()

    if not Path(args.config).exists():
        log.error("Config file not found: %s", args.config)
        sys.exit(1)

    cfg = ScraperConfig.from_file(args.config)

    if args.platforms:
        cfg.platforms = args.platforms

    if not args.dry_run and not Path(cfg.db_path).exists():
        log.error("DB not found at %s — start the web app once first to create it.", cfg.db_path)
        sys.exit(1)

    log.info("Platforms: %s | Dry run: %s", cfg.platforms, args.dry_run)
    orchestrator = ScraperOrchestrator(cfg, dry_run=args.dry_run)
    orchestrator.run()


if __name__ == "__main__":
    main()
