"""Smart page detection - find contact, legal, and about pages from homepage links."""

import re
import logging
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Set

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Page type patterns: slug keywords and link text patterns
PAGE_PATTERNS: Dict[str, Dict[str, list]] = {
    "contact": {
        "slugs": [
            r"/contact",
            r"/nous-contacter",
            r"/contactez-nous",
            r"/kontakt",
            r"/contacto",
            r"/get-in-touch",
            r"/reach-us",
            r"/write-to-us",
            r"/ecrivez-nous",
            r"/coordonnees",
        ],
        "link_text": [
            r"contact(?:ez)?(?:\s*-?\s*nous)?",
            r"nous\s+contacter",
            r"get\s+in\s+touch",
            r"reach\s+us",
            r"coordonn[eé]es",
            r"kontakt",
            r"contacto",
        ],
    },
    "legal": {
        "slugs": [
            r"/mentions-legales",
            r"/mentions-l[eé]gales",
            r"/legal-notice",
            r"/legal",
            r"/legales",
            r"/imprint",
            r"/impressum",
            r"/aviso-legal",
            r"/cgu",
            r"/cgv",
            r"/conditions-generales",
            r"/terms",
            r"/privacy",
            r"/politique-de-confidentialite",
            r"/confidentialite",
            r"/donnees-personnelles",
        ],
        "link_text": [
            r"mentions?\s+l[eé]gales?",
            r"legal\s+notice",
            r"imprint",
            r"impressum",
            r"aviso\s+legal",
            r"cgu",
            r"cgv",
            r"conditions\s+g[eé]n[eé]rales",
            r"politique\s+de\s+confidentialit[eé]",
            r"donn[eé]es\s+personnelles",
            r"privacy\s+policy",
        ],
    },
    "about": {
        "slugs": [
            r"/about",
            r"/a-propos",
            r"/qui-sommes-nous",
            r"/notre-equipe",
            r"/team",
            r"/equipe",
            r"/about-us",
            r"/[uü]ber-uns",
            r"/sobre-nosotros",
        ],
        "link_text": [
            r"[àa]\s+propos",
            r"qui\s+sommes[\s-]+nous",
            r"notre\s+[eé]quipe",
            r"about(?:\s+us)?",
            r"the\s+team",
            r"l['']?[eé]quipe",
        ],
    },
}


def _normalize_url(url: str) -> str:
    """Normalize a URL by removing trailing slashes and fragments."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _is_same_domain(url: str, base_url: str) -> bool:
    """Check if a URL belongs to the same domain as the base URL."""
    try:
        url_domain = urlparse(url).netloc.lower()
        base_domain = urlparse(base_url).netloc.lower()
        # Allow subdomains (e.g., www.example.com matches example.com)
        return (
            url_domain == base_domain
            or url_domain.endswith("." + base_domain)
            or base_domain.endswith("." + url_domain)
        )
    except Exception:
        return False


def detect_pages(html: str, base_url: str) -> Dict[str, List[str]]:
    """
    Analyze homepage HTML to detect contact, legal, and about pages.

    Args:
        html: Raw HTML of the homepage
        base_url: The base URL of the site

    Returns:
        Dict mapping page type to list of discovered URLs
    """
    soup = BeautifulSoup(html, "html.parser")
    found: Dict[str, Set[str]] = {ptype: set() for ptype in PAGE_PATTERNS}

    # Get all links from the page
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()

        # Skip empty, javascript, and anchor-only links
        if not href or href.startswith(("#", "javascript:", "tel:", "mailto:")):
            continue

        # Resolve relative URLs
        full_url = urljoin(base_url, href)

        # Only consider same-domain links
        if not _is_same_domain(full_url, base_url):
            continue

        # Get link text for text-based matching
        link_text = a_tag.get_text(strip=True).lower()
        # Also check title attribute
        title = (a_tag.get("title", "") or "").lower()

        parsed_path = urlparse(full_url).path.lower().rstrip("/")

        for page_type, patterns in PAGE_PATTERNS.items():
            matched = False

            # Check URL slug patterns
            for slug_pattern in patterns["slugs"]:
                if re.search(slug_pattern, parsed_path, re.IGNORECASE):
                    matched = True
                    break

            # Check link text patterns
            if not matched:
                for text_pattern in patterns["link_text"]:
                    if re.search(text_pattern, link_text, re.IGNORECASE):
                        matched = True
                        break
                    if title and re.search(text_pattern, title, re.IGNORECASE):
                        matched = True
                        break

            if matched:
                normalized = _normalize_url(full_url)
                found[page_type].add(normalized)
                logger.debug(
                    "Detected %s page: %s (link text: '%s')",
                    page_type, normalized, link_text
                )

    result = {ptype: sorted(urls) for ptype, urls in found.items()}

    for ptype, urls in result.items():
        if urls:
            logger.info("Found %d %s page(s) for %s", len(urls), ptype, base_url)
        else:
            logger.debug("No %s page found for %s", ptype, base_url)

    return result
