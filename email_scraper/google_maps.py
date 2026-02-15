"""Google Maps scraper - extract business listings from Google Maps search."""

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote_plus

import requests

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class BusinessListing:
    """A single business from Google Maps."""
    name: str = ""
    website: str = ""
    address: str = ""
    category: str = ""
    phone: str = ""
    rating: Optional[float] = None
    reviews_count: int = 0
    maps_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def search_google_maps(
    query: str,
    num_results: int = 20,
    lang: str = "fr",
    country: str = "fr",
    delay: float = 2.0,
    timeout: int = 15,
    user_agent: Optional[str] = None,
) -> List[BusinessListing]:
    """
    Search Google Maps for businesses and extract their listings.

    Args:
        query: Search query (e.g. "plombier Paris")
        num_results: Target number of results
        lang: Language
        country: Country
        delay: Delay between pagination requests
        timeout: Request timeout
        user_agent: Custom user agent

    Returns:
        List of BusinessListing objects
    """
    logger.info("Searching Google Maps for: '%s' (target: %d results)", query, num_results)

    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent or DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": f"{lang}-{country.upper()},{lang};q=0.9,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })

    # Fetch the Google Maps search page
    maps_url = (
        f"https://www.google.com/maps/search/{quote_plus(query)}"
        f"?hl={lang}&gl={country}"
    )

    all_listings: List[BusinessListing] = []
    seen_names: Set[str] = set()

    try:
        response = session.get(maps_url, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error("Failed to fetch Google Maps: %s", e)
        return []

    # Parse business data from the page
    listings = _parse_maps_page(response.text)

    for listing in listings:
        key = listing.name.lower().strip()
        if key and key not in seen_names:
            seen_names.add(key)
            all_listings.append(listing)

    logger.info(
        "Google Maps: found %d business(es) for '%s'",
        len(all_listings), query
    )

    # If we need more results, try paginating via the search with start offset
    # Google Maps pagination uses scrolling, so we use Google local search as fallback
    if len(all_listings) < num_results:
        extra = _search_google_local(
            query=query,
            num_results=num_results - len(all_listings),
            start_offset=0,
            lang=lang,
            country=country,
            delay=delay,
            timeout=timeout,
            session=session,
            seen_names=seen_names,
        )
        all_listings.extend(extra)

    logger.info(
        "Total: %d business(es) found for '%s'",
        len(all_listings), query
    )
    return all_listings[:num_results]


def _search_google_local(
    query: str,
    num_results: int,
    start_offset: int,
    lang: str,
    country: str,
    delay: float,
    timeout: int,
    session: requests.Session,
    seen_names: Set[str],
) -> List[BusinessListing]:
    """
    Fallback: search Google with local intent (tbm=lcl) for additional results.

    Google's local search tab returns structured business data that
    complements Google Maps results.
    """
    listings: List[BusinessListing] = []
    start = start_offset

    for page in range(5):  # Max 5 pages
        if len(listings) >= num_results:
            break

        url = (
            f"https://www.google.com/search"
            f"?q={quote_plus(query)}"
            f"&tbm=lcl"
            f"&hl={lang}"
            f"&gl={country}"
            f"&start={start}"
        )

        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                logger.warning("Rate limited, waiting 30s...")
                time.sleep(30)
                continue
            logger.error("HTTP error: %s", e)
            break
        except requests.exceptions.RequestException as e:
            logger.error("Request error: %s", e)
            break

        page_listings = _parse_local_search(resp.text)
        if not page_listings:
            break

        for bl in page_listings:
            key = bl.name.lower().strip()
            if key and key not in seen_names:
                seen_names.add(key)
                listings.append(bl)

        start += 20
        if page < 4:
            time.sleep(delay)

    return listings


def _parse_maps_page(html: str) -> List[BusinessListing]:
    """
    Parse Google Maps page source to extract business listings.

    Google Maps embeds data in AF_initDataCallback script blocks
    as deeply nested JavaScript arrays.
    """
    listings: List[BusinessListing] = []

    # Extract all AF_initDataCallback data blocks
    data_blocks = _extract_af_data_blocks(html)

    for data in data_blocks:
        # Walk the nested structure to find business entries
        found = _find_business_entries(data)
        listings.extend(found)

    # Fallback: if AF_initDataCallback parsing didn't work,
    # try regex-based extraction from raw HTML
    if not listings:
        listings = _regex_extract_from_maps(html)

    return listings


def _extract_af_data_blocks(html: str) -> List[Any]:
    """Extract data from AF_initDataCallback calls in script tags."""
    blocks = []

    # Pattern: AF_initDataCallback({key: '...', hash: '...', data: [...]});
    pattern = re.compile(
        r'AF_initDataCallback\(\{[^}]*data:\s*(\[[\s\S]*?\])\s*\}\);',
        re.DOTALL,
    )

    # Also try the window.APP_INITIALIZATION_STATE pattern
    app_state_pattern = re.compile(
        r'window\.APP_INITIALIZATION_STATE\s*=\s*(\[[\s\S]*?\]);\s*(?:window\.|</script>)',
        re.DOTALL,
    )

    for match in pattern.finditer(html):
        raw = match.group(1)
        parsed = _safe_json_parse(raw)
        if parsed is not None:
            blocks.append(parsed)

    for match in app_state_pattern.finditer(html):
        raw = match.group(1)
        parsed = _safe_json_parse(raw)
        if parsed is not None:
            blocks.append(parsed)

    return blocks


def _safe_json_parse(raw: str) -> Any:
    """Try to parse a JSON-like string, handling common quirks."""
    # Replace JavaScript-specific values
    cleaned = raw.replace("null", "null")  # already valid JSON

    # Handle cases where Google uses 'undefined'
    cleaned = re.sub(r'\bundefined\b', 'null', cleaned)

    # Handle trailing commas (invalid JSON but common in JS)
    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)

    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, RecursionError):
        return None


def _find_business_entries(data: Any, depth: int = 0) -> List[BusinessListing]:
    """
    Recursively walk nested data to find business listing entries.

    Business entries in Google Maps data typically contain:
    - A string that looks like a business name
    - An address string (contains postal code pattern)
    - A phone string (French phone number pattern)
    - A website URL
    - A category string
    """
    if depth > 20:
        return []

    listings: List[BusinessListing] = []

    if not isinstance(data, list):
        return []

    # Check if this array level contains business data
    listing = _try_extract_listing(data)
    if listing and listing.name:
        listings.append(listing)
        return listings  # Don't recurse deeper once we found a listing

    # Recurse into sub-arrays
    for item in data:
        if isinstance(item, list):
            listings.extend(_find_business_entries(item, depth + 1))

    return listings


def _try_extract_listing(arr: Any) -> Optional[BusinessListing]:
    """
    Try to extract a BusinessListing from a Google Maps data array.

    Google Maps stores business data in arrays with a recognizable pattern:
    The array contains strings at specific positions for name, address,
    phone, website, and category.
    """
    if not isinstance(arr, list) or len(arr) < 5:
        return None

    # Collect all strings from the array (flattened one level)
    strings = []
    for item in arr:
        if isinstance(item, str):
            strings.append(item)
        elif isinstance(item, list):
            for sub in item:
                if isinstance(sub, str):
                    strings.append(sub)

    if len(strings) < 2:
        return None

    # Look for patterns that indicate this is a business entry
    phone = None
    website = None
    address = None
    category = None
    name = None

    for s in strings:
        s_stripped = s.strip()
        if not s_stripped:
            continue

        # Phone: French formats
        if not phone and _is_french_phone(s_stripped):
            phone = _normalize_phone(s_stripped)
        # Website URL
        elif not website and _is_website_url(s_stripped):
            website = s_stripped
        # Address: contains French postal code (5 digits)
        elif not address and _looks_like_address(s_stripped):
            address = s_stripped
        # Short string without digits → could be category or name
        elif not category and _looks_like_category(s_stripped):
            category = s_stripped

    # Try to find the business name: first substantial string that's
    # not phone, website, address, or category
    for s in strings:
        s_stripped = s.strip()
        if not s_stripped or len(s_stripped) < 2 or len(s_stripped) > 100:
            continue
        if s_stripped in (phone, website, address, category):
            continue
        if _is_french_phone(s_stripped) or _is_website_url(s_stripped):
            continue
        if not name and not s_stripped.startswith("http"):
            name = s_stripped
            break

    # Only return if we have at least a name + one other field
    if name and (phone or website or address):
        return BusinessListing(
            name=name,
            website=website or "",
            address=address or "",
            category=category or "",
            phone=phone or "",
        )

    return None


def _regex_extract_from_maps(html: str) -> List[BusinessListing]:
    """
    Fallback: extract business info using regex patterns on raw HTML.

    Looks for structured patterns in Google Maps page source.
    """
    listings: List[BusinessListing] = []

    # Google Maps embeds business data in specific patterns within the HTML.
    # Look for arrays containing [name, ..., address, ..., phone, ..., website]
    # Pattern: business data often appears near coordinates and rating data

    # Find all phone numbers with surrounding context
    phone_pattern = re.compile(
        r'(?:0[1-9][\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2})'
        r'|(?:\+33[\s.]?\d[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2})',
    )

    # Find all website URLs in the data (not Google/Maps URLs)
    url_pattern = re.compile(
        r'"(https?://(?!(?:www\.)?google\.|maps\.|play\.|schema\.|googleapis\.|gstatic\.)'
        r'[a-zA-Z0-9][a-zA-Z0-9\-]*\.[a-zA-Z]{2,}[^"]*)"'
    )

    # Find address-like patterns (French postal code)
    address_pattern = re.compile(
        r'"([^"]{5,150}?\b\d{5}\b[^"]{0,80}?)"'
    )

    # Extract blocks that contain business data
    # Google Maps wraps each listing in recognizable array patterns
    # Look for patterns like: ["Business Name",... ,"address",...,"0X XX XX XX XX"]
    listing_pattern = re.compile(
        r'\["([^"]{2,80})"\s*,(?:[^]]*?)'  # name
        r'"((?:0[1-9]|\\u002B33)[\d\s.\\u002D]{8,20})"',  # phone
        re.DOTALL,
    )

    seen = set()
    for match in listing_pattern.finditer(html):
        name = match.group(1).strip()
        phone_raw = match.group(2).strip()

        # Clean unicode escapes
        name = _clean_unicode(name)
        phone_raw = _clean_unicode(phone_raw)

        if not name or name.lower() in seen:
            continue
        if len(name) < 2 or len(name) > 100:
            continue

        # Get context around this match to find website and address
        ctx_start = max(0, match.start() - 2000)
        ctx_end = min(len(html), match.end() + 2000)
        context = html[ctx_start:ctx_end]

        website = ""
        for url_match in url_pattern.finditer(context):
            candidate = _clean_unicode(url_match.group(1))
            if candidate and not any(x in candidate.lower() for x in [
                "google.", "gstatic.", "googleapis.", "schema.org",
                "facebook.", "instagram.", "twitter.", "youtube.",
            ]):
                website = candidate
                break

        address = ""
        for addr_match in address_pattern.finditer(context):
            candidate = _clean_unicode(addr_match.group(1))
            if candidate and re.search(r'\b\d{5}\b', candidate):
                # Sanity check: should look like an address
                if len(candidate) < 150 and not candidate.startswith("http"):
                    address = candidate
                    break

        phone = _normalize_phone(phone_raw)

        seen.add(name.lower())
        listings.append(BusinessListing(
            name=name,
            website=website,
            address=address,
            phone=phone,
        ))

    return listings


def _clean_unicode(s: str) -> str:
    """Clean Google's unicode escape sequences."""
    s = s.replace("\\u0026", "&")
    s = s.replace("\\u003d", "=")
    s = s.replace("\\u002B", "+")
    s = s.replace("\\u002D", "-")
    s = s.replace("\\u002F", "/")
    s = s.replace("\\u003C", "<")
    s = s.replace("\\u003E", ">")
    s = s.replace("\\n", " ")
    s = s.replace("\\t", " ")
    s = s.replace("\\/", "/")
    # Handle \uXXXX sequences
    try:
        s = s.encode().decode("unicode_escape", errors="ignore")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return s.strip()


def _is_french_phone(s: str) -> bool:
    """Check if a string looks like a French phone number."""
    cleaned = re.sub(r'[\s.\-()]', '', s)
    if re.match(r'^0[1-9]\d{8}$', cleaned):
        return True
    if re.match(r'^\+33\d{9}$', cleaned):
        return True
    return False


def _normalize_phone(s: str) -> str:
    """Normalize a phone number to standard French format: 0X XX XX XX XX."""
    cleaned = re.sub(r'[\s.\-()]', '', s)
    # Convert +33 to 0
    if cleaned.startswith("+33"):
        cleaned = "0" + cleaned[3:]
    elif cleaned.startswith("0033"):
        cleaned = "0" + cleaned[4:]

    if len(cleaned) == 10 and cleaned[0] == "0":
        return f"{cleaned[0:2]} {cleaned[2:4]} {cleaned[4:6]} {cleaned[6:8]} {cleaned[8:10]}"
    return s.strip()


def _is_website_url(s: str) -> bool:
    """Check if a string is a website URL (not Google/Maps)."""
    if not s.startswith(("http://", "https://")):
        return False
    excluded = [
        "google.", "gstatic.", "googleapis.", "schema.org",
        "facebook.com", "instagram.com", "twitter.com", "youtube.com",
        "maps.", "play.google",
    ]
    s_lower = s.lower()
    return not any(x in s_lower for x in excluded)


def _looks_like_address(s: str) -> bool:
    """Check if a string looks like a French address."""
    if len(s) < 10 or len(s) > 200:
        return False
    # Must contain a French postal code (5 digits)
    if not re.search(r'\b\d{5}\b', s):
        return False
    # Should not be a URL or phone number
    if s.startswith("http") or _is_french_phone(s):
        return False
    return True


def _looks_like_category(s: str) -> bool:
    """Check if a string looks like a business category."""
    if len(s) < 3 or len(s) > 60:
        return False
    # Categories are typically short, no digits, no URLs
    if re.search(r'\d{3,}', s):
        return False
    if s.startswith("http") or "@" in s:
        return False
    return True


def _parse_local_search(html: str) -> List[BusinessListing]:
    """
    Parse Google local search results (tbm=lcl).

    Returns business listings extracted from the local search page.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    listings: List[BusinessListing] = []

    # Google local results are in div blocks with business info
    # Try to find structured data in the page
    for script in soup.find_all("script"):
        text = script.string or ""
        if "AF_initDataCallback" not in text:
            continue
        # Extract the data payload
        data_match = re.search(
            r'AF_initDataCallback\(\{[^}]*data:\s*(\[[\s\S]*?\])\s*\}\)',
            text,
        )
        if data_match:
            parsed = _safe_json_parse(data_match.group(1))
            if parsed:
                found = _find_business_entries(parsed)
                listings.extend(found)

    # Fallback: parse from visible HTML structure
    if not listings:
        listings = _parse_local_html(soup)

    return listings


def _parse_local_html(soup) -> List[BusinessListing]:
    """Parse business listings from local search HTML structure."""
    listings = []

    # Local results often appear in divs with specific classes
    # Look for result blocks that contain business names and details
    for div in soup.find_all("div", class_=re.compile(r"VkpGBb|rllt__details")):
        name_el = div.find(["span", "div"], class_=re.compile(r"OSrXXb|dbg0pd"))
        if not name_el:
            continue

        name = name_el.get_text(strip=True)
        if not name:
            continue

        # Extract other details from the same block or parent
        parent = div.parent or div
        text = parent.get_text(" ", strip=True)

        phone = ""
        phone_match = re.search(
            r'(?:0[1-9][\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2}[\s.]?\d{2})',
            text,
        )
        if phone_match:
            phone = _normalize_phone(phone_match.group())

        address = ""
        addr_match = re.search(r'[\d]+[^,]*,?\s*\d{5}\s+[A-Za-zÀ-ÿ\-\s]+', text)
        if addr_match:
            address = addr_match.group().strip()

        website = ""
        for a_tag in parent.find_all("a", href=True):
            href = a_tag["href"]
            if href.startswith("http") and "google." not in href:
                website = href
                break

        category = ""
        cat_el = div.find("span", class_=re.compile(r"YhemCb|rllt__details"))
        if cat_el:
            cat_text = cat_el.get_text(strip=True)
            if cat_text and len(cat_text) < 60:
                category = cat_text

        listings.append(BusinessListing(
            name=name,
            website=website,
            address=address,
            category=category,
            phone=phone,
        ))

    return listings


def search_maps_multiple_keywords(
    keyword_list: List[str],
    num_results_per_keyword: int = 20,
    lang: str = "fr",
    country: str = "fr",
    delay: float = 3.0,
    timeout: int = 15,
    user_agent: Optional[str] = None,
) -> List[BusinessListing]:
    """
    Search Google Maps for multiple keyword queries and merge results.

    Args:
        keyword_list: List of search queries
        num_results_per_keyword: Results per query
        lang: Language
        country: Country
        delay: Delay between searches
        timeout: Request timeout
        user_agent: Custom user agent

    Returns:
        Merged list of unique BusinessListing objects
    """
    all_listings: List[BusinessListing] = []
    seen_names: Set[str] = set()

    for i, query in enumerate(keyword_list):
        logger.info("Searching %d/%d: '%s'", i + 1, len(keyword_list), query)

        results = search_google_maps(
            query=query,
            num_results=num_results_per_keyword,
            lang=lang,
            country=country,
            delay=delay,
            timeout=timeout,
            user_agent=user_agent,
        )

        for listing in results:
            key = listing.name.lower().strip()
            if key not in seen_names:
                seen_names.add(key)
                all_listings.append(listing)

        if i < len(keyword_list) - 1:
            time.sleep(delay + 2)

    logger.info(
        "Multi-keyword Maps search: %d unique businesses from %d queries",
        len(all_listings), len(keyword_list),
    )
    return all_listings
