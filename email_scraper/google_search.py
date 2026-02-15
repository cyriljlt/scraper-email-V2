"""Google search module - find websites from keywords."""

import logging
import re
import time
from typing import List, Optional, Set
from urllib.parse import urlparse, quote_plus

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Domains to exclude from results (not real business sites)
EXCLUDED_DOMAINS = {
    "google.com", "google.fr", "youtube.com", "facebook.com",
    "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "pinterest.com", "tiktok.com", "snapchat.com",
    "wikipedia.org", "amazon.com", "amazon.fr",
    "ebay.com", "ebay.fr", "leboncoin.fr",
    "pagesjaunes.fr", "118712.fr", "118218.fr",
    "tripadvisor.com", "tripadvisor.fr",
    "yelp.com", "yelp.fr",
    "indeed.com", "indeed.fr", "glassdoor.com",
    "gouvernement.fr", "service-public.fr", "gouv.fr",
}


def _is_excluded(domain: str) -> bool:
    """Check if a domain should be excluded from results."""
    domain = domain.lower()
    for excluded in EXCLUDED_DOMAINS:
        if domain == excluded or domain.endswith("." + excluded):
            return True
    return False


def _extract_domain(url: str) -> str:
    """Extract clean domain from URL."""
    netloc = urlparse(url).netloc.lower()
    netloc = re.sub(r"^www\.", "", netloc)
    return netloc.split(":")[0]


def search_google(
    keywords: List[str],
    num_results: int = 20,
    lang: str = "fr",
    country: str = "fr",
    delay: float = 2.0,
    timeout: int = 15,
    user_agent: Optional[str] = None,
) -> List[str]:
    """
    Search Google for websites matching given keywords.

    Uses Google's HTML search (no API key required).
    Collects results across multiple pages if needed.

    Args:
        keywords: List of search keywords (combined with spaces)
        num_results: Target number of results to collect
        lang: Language for search results
        country: Country for search results
        delay: Delay between requests to avoid rate limiting
        timeout: Request timeout in seconds
        user_agent: Custom user agent string

    Returns:
        List of unique website URLs found
    """
    query = " ".join(keywords)
    logger.info("Searching Google for: '%s' (target: %d results)", query, num_results)

    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent or DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": f"{lang}-{country.upper()},{lang};q=0.9,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    })

    urls: List[str] = []
    seen_domains: Set[str] = set()
    start = 0
    max_pages = (num_results // 10) + 3  # Extra pages to compensate for filtering

    for page in range(max_pages):
        if len(urls) >= num_results:
            break

        search_url = (
            f"https://www.google.com/search"
            f"?q={quote_plus(query)}"
            f"&hl={lang}"
            f"&gl={country}"
            f"&num=10"
            f"&start={start}"
        )

        try:
            response = session.get(search_url, timeout=timeout)
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                logger.warning("Google rate limit hit, waiting 30s before retry...")
                time.sleep(30)
                try:
                    response = session.get(search_url, timeout=timeout)
                    response.raise_for_status()
                except Exception:
                    logger.error("Failed to fetch Google results after retry")
                    break
            else:
                logger.error("HTTP error fetching Google results: %s", e)
                break
        except requests.exceptions.RequestException as e:
            logger.error("Error fetching Google results: %s", e)
            break

        # Parse results
        page_urls = _parse_google_results(response.text)

        if not page_urls:
            logger.debug("No more results found on page %d", page + 1)
            break

        for url in page_urls:
            domain = _extract_domain(url)
            if not domain:
                continue
            if _is_excluded(domain):
                logger.debug("Excluded: %s (%s)", url, domain)
                continue
            if domain in seen_domains:
                logger.debug("Duplicate domain: %s", domain)
                continue

            seen_domains.add(domain)
            urls.append(url)
            logger.debug("Found: %s", url)

            if len(urls) >= num_results:
                break

        start += 10

        # Delay between pages
        if page < max_pages - 1 and len(urls) < num_results:
            time.sleep(delay)

    logger.info("Google search complete: found %d unique sites for '%s'", len(urls), query)
    return urls


def _parse_google_results(html: str) -> List[str]:
    """Parse Google search results HTML and extract URLs."""
    soup = BeautifulSoup(html, "html.parser")
    urls = []

    # Method 1: Standard result links in <div class="g"> blocks
    for div in soup.find_all("div", class_="g"):
        link = div.find("a", href=True)
        if link:
            href = link["href"]
            if href.startswith("http") and "google.com" not in href:
                urls.append(href)

    # Method 2: If method 1 yields nothing, try broader link extraction
    if not urls:
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            # Google wraps some results in /url?q=...
            if href.startswith("/url?q="):
                actual_url = href.split("/url?q=")[1].split("&")[0]
                if actual_url.startswith("http"):
                    urls.append(actual_url)
            elif href.startswith("http") and "google." not in href:
                urls.append(href)

    return urls


def search_multiple_keywords(
    keyword_groups: List[List[str]],
    num_results_per_keyword: int = 20,
    lang: str = "fr",
    country: str = "fr",
    delay: float = 3.0,
    timeout: int = 15,
    user_agent: Optional[str] = None,
) -> List[str]:
    """
    Search Google for multiple keyword groups and merge results.

    Each keyword group is searched separately, then results are merged
    with deduplication by domain.

    Args:
        keyword_groups: List of keyword groups (each group is a list of words)
        num_results_per_keyword: Results per keyword group
        lang: Language for search
        country: Country for search
        delay: Delay between searches
        timeout: Request timeout
        user_agent: Custom user agent

    Returns:
        Merged list of unique website URLs
    """
    all_urls: List[str] = []
    seen_domains: Set[str] = set()

    for i, keywords in enumerate(keyword_groups):
        logger.info(
            "Searching keyword group %d/%d: %s",
            i + 1, len(keyword_groups), " ".join(keywords)
        )

        results = search_google(
            keywords=keywords,
            num_results=num_results_per_keyword,
            lang=lang,
            country=country,
            delay=delay,
            timeout=timeout,
            user_agent=user_agent,
        )

        for url in results:
            domain = _extract_domain(url)
            if domain not in seen_domains:
                seen_domains.add(domain)
                all_urls.append(url)

        # Delay between keyword groups
        if i < len(keyword_groups) - 1:
            time.sleep(delay + 2)

    logger.info(
        "Multi-keyword search complete: %d unique sites from %d keyword group(s)",
        len(all_urls), len(keyword_groups)
    )
    return all_urls
