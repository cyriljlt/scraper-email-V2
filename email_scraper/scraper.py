"""Main scraper orchestration - fetches pages and coordinates extraction."""

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .detector import detect_pages
from .extractor import extract_emails, extract_from_mailto, deobfuscate_emails, validate_email
from .scorer import compute_confidence

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; EmailScraper/1.0; +https://github.com/scraper-email)"
)
RATE_LIMIT_DELAY = 1.0  # seconds between requests to same domain


@dataclass
class EmailResult:
    """A single email extraction result."""
    url: str
    email: str
    source_page: str
    confidence_score: float
    source_type: str = "regex"
    page_type: str = "homepage"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScrapeResult:
    """Results for a single input URL."""
    input_url: str
    emails: List[EmailResult] = field(default_factory=list)
    pages_scraped: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class EmailScraper:
    """Main scraper that orchestrates page fetching and email extraction."""

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
        respect_robots: bool = True,
        cache_dir: Optional[str] = None,
        rate_limit: float = RATE_LIMIT_DELAY,
    ):
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.rate_limit = rate_limit
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._last_request_time: Dict[str, float] = {}
        self._robots_cache: Dict[str, Optional[RobotFileParser]] = {}

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        })

        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def scrape_url(self, url: str, dry_run: bool = False) -> ScrapeResult:
        """
        Scrape emails from a URL and its contact/legal/about pages.

        Args:
            url: The base URL to scrape
            dry_run: If True, only detect pages without actually scraping

        Returns:
            ScrapeResult with all found emails
        """
        result = ScrapeResult(input_url=url)

        # Normalize URL
        url = self._normalize_url(url)
        logger.info("Scraping: %s", url)

        # Check robots.txt
        if self.respect_robots and not self._is_allowed(url):
            logger.warning("Blocked by robots.txt: %s", url)
            result.errors.append(f"Blocked by robots.txt: {url}")
            return result

        # 1. Fetch and process homepage
        homepage_html = self._fetch_page(url)
        if homepage_html is None:
            result.errors.append(f"Failed to fetch homepage: {url}")
            return result

        result.pages_scraped.append(url)

        if dry_run:
            # In dry-run mode, detect pages but don't extract emails
            pages = detect_pages(homepage_html, url)
            logger.info(
                "DRY RUN - Would scrape: homepage + %d contact, %d legal, %d about pages",
                len(pages.get("contact", [])),
                len(pages.get("legal", [])),
                len(pages.get("about", [])),
            )
            for ptype, urls in pages.items():
                for u in urls:
                    logger.info("  [%s] %s", ptype, u)
            return result

        # Extract emails from homepage
        self._extract_from_page(
            homepage_html, url, "homepage", result
        )

        # 2. Detect and scrape subpages
        pages = detect_pages(homepage_html, url)

        for page_type, page_urls in pages.items():
            for page_url in page_urls:
                # Check robots.txt for subpage
                if self.respect_robots and not self._is_allowed(page_url):
                    logger.warning("Blocked by robots.txt: %s", page_url)
                    result.errors.append(f"Blocked by robots.txt: {page_url}")
                    continue

                # Rate limit
                self._rate_limit(page_url)

                page_html = self._fetch_page(page_url)
                if page_html is None:
                    result.errors.append(f"Failed to fetch {page_type} page: {page_url}")
                    continue

                result.pages_scraped.append(page_url)
                self._extract_from_page(
                    page_html, page_url, page_type, result
                )

        # Deduplicate: keep highest confidence per email
        result.emails = self._deduplicate(result.emails)

        logger.info(
            "Finished %s: found %d unique email(s) from %d page(s)",
            url, len(result.emails), len(result.pages_scraped)
        )

        return result

    def _extract_from_page(
        self, html: str, page_url: str, page_type: str, result: ScrapeResult
    ):
        """Extract emails from a page and add to results."""
        soup = BeautifulSoup(html, "html.parser")
        # Remove script and style tags for cleaner text extraction
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)

        # Extract emails using all methods
        emails = extract_emails(text, html)

        for email in emails:
            # Determine source type
            source_type = "regex"
            if email in [e.lower() for e in extract_from_mailto(html)]:
                source_type = "mailto"
            elif email in deobfuscate_emails(text):
                source_type = "obfuscated"

            confidence = compute_confidence(
                email, page_url, text, html, source_type
            )

            result.emails.append(EmailResult(
                url=result.input_url,
                email=email,
                source_page=page_url,
                confidence_score=confidence,
                source_type=source_type,
                page_type=page_type,
            ))

    def _deduplicate(self, emails: List[EmailResult]) -> List[EmailResult]:
        """Keep only the highest-confidence result per email address."""
        best: Dict[str, EmailResult] = {}
        for er in emails:
            if er.email not in best or er.confidence_score > best[er.email].confidence_score:
                best[er.email] = er
        return sorted(best.values(), key=lambda e: e.confidence_score, reverse=True)

    def _normalize_url(self, url: str) -> str:
        """Ensure URL has a scheme."""
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url

    def _fetch_page(self, url: str) -> Optional[str]:
        """
        Fetch a page's HTML content, with caching support.

        Returns None on failure.
        """
        # Check cache first
        if self.cache_dir:
            cached = self._get_from_cache(url)
            if cached is not None:
                logger.debug("Cache hit: %s", url)
                return cached

        try:
            response = self.session.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
            )
            response.raise_for_status()

            # Check content type - only process HTML
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type and "application/xhtml" not in content_type:
                logger.warning("Non-HTML content at %s: %s", url, content_type)
                return None

            html = response.text

            # Save to cache
            if self.cache_dir:
                self._save_to_cache(url, html)

            return html

        except requests.exceptions.Timeout:
            logger.warning("Timeout fetching %s", url)
        except requests.exceptions.TooManyRedirects:
            logger.warning("Too many redirects for %s", url)
        except requests.exceptions.HTTPError as e:
            logger.warning("HTTP error for %s: %s", url, e)
        except requests.exceptions.ConnectionError:
            logger.warning("Connection error for %s", url)
        except requests.exceptions.RequestException as e:
            logger.warning("Request failed for %s: %s", url, e)

        return None

    def _is_allowed(self, url: str) -> bool:
        """Check if URL is allowed by robots.txt."""
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

        if robots_url not in self._robots_cache:
            rp = RobotFileParser()
            rp.set_url(robots_url)
            try:
                rp.read()
                self._robots_cache[robots_url] = rp
            except Exception:
                logger.debug("Could not fetch robots.txt for %s", parsed.netloc)
                self._robots_cache[robots_url] = None

        rp = self._robots_cache[robots_url]
        if rp is None:
            return True  # No robots.txt = allowed

        return rp.can_fetch(self.session.headers["User-Agent"], url)

    def _rate_limit(self, url: str):
        """Apply rate limiting per domain."""
        domain = urlparse(url).netloc
        now = time.time()
        if domain in self._last_request_time:
            elapsed = now - self._last_request_time[domain]
            if elapsed < self.rate_limit:
                sleep_time = self.rate_limit - elapsed
                logger.debug("Rate limiting: sleeping %.2fs for %s", sleep_time, domain)
                time.sleep(sleep_time)
        self._last_request_time[domain] = time.time()

    def _cache_key(self, url: str) -> str:
        """Generate a cache key for a URL."""
        return hashlib.sha256(url.encode()).hexdigest()

    def _get_from_cache(self, url: str) -> Optional[str]:
        """Get a page from cache."""
        if not self.cache_dir:
            return None
        cache_file = self.cache_dir / f"{self._cache_key(url)}.html"
        if cache_file.exists():
            return cache_file.read_text(encoding="utf-8")
        return None

    def _save_to_cache(self, url: str, html: str):
        """Save a page to cache."""
        if not self.cache_dir:
            return
        cache_file = self.cache_dir / f"{self._cache_key(url)}.html"
        cache_file.write_text(html, encoding="utf-8")
        # Also save a mapping file for debugging
        mapping_file = self.cache_dir / "url_mapping.json"
        mapping = {}
        if mapping_file.exists():
            try:
                mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        mapping[self._cache_key(url)] = url
        mapping_file.write_text(
            json.dumps(mapping, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
