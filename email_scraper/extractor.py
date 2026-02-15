"""Email extraction with robust regex, deobfuscation, and validation."""

import re
import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Valid TLDs (most common ones - covers 99%+ of real emails)
VALID_TLDS = {
    "com", "org", "net", "edu", "gov", "mil", "int",
    "fr", "de", "uk", "es", "it", "nl", "be", "ch", "at", "pt", "pl",
    "ru", "cn", "jp", "kr", "br", "mx", "ar", "co", "cl", "pe",
    "us", "ca", "au", "nz", "in", "za",
    "io", "co", "me", "tv", "cc", "biz", "info", "name", "pro", "mobi",
    "eu", "asia", "tel", "coop", "museum", "aero", "jobs", "travel",
    "cat", "post", "xxx",
    "online", "store", "shop", "site", "website", "tech", "app", "dev",
    "cloud", "digital", "agency", "studio", "design", "media", "marketing",
    "consulting", "solutions", "services", "group", "team", "company",
    "paris", "london", "berlin", "nyc", "tokyo",
    "re", "gp", "mq", "gf", "nc", "pf", "pm", "wf", "yt",  # French overseas
}

# File extensions to reject as TLD (common false positives)
FILE_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "svg", "webp", "bmp", "ico",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "css", "js", "html", "htm", "xml", "json", "csv",
    "zip", "rar", "gz", "tar", "7z",
    "mp3", "mp4", "avi", "mov", "wmv", "flv",
    "ttf", "woff", "woff2", "eot", "otf",
    "php", "asp", "aspx", "jsp", "py", "rb",
}

# Permissive email regex for candidate extraction (cleaned up in post-processing).
# Captures generously; clean_email() trims garbage chars from boundaries.
EMAIL_REGEX = re.compile(
    r'([A-Za-z0-9][A-Za-z0-9._%+\-]*'   # Local part starts with alnum
    r'@'
    r'[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?'  # Domain segment
    r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*'  # More domain segments
    r'\.[A-Za-z]{2,15})',                 # TLD
)

# Obfuscation patterns
OBFUSCATION_PATTERNS = [
    # contact[at]domain[dot]com
    (re.compile(
        r'([A-Za-z0-9][A-Za-z0-9._%+\-]*)'
        r'\s*[\[\(]\s*(?:at|AT|arobase|@)\s*[\]\)]\s*'
        r'([A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?'
        r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*)'
        r'\s*[\[\(]\s*(?:dot|DOT|point|\.)\s*[\]\)]\s*'
        r'([A-Za-z]{2,15})',
        re.IGNORECASE
    ), r'\1@\2.\3'),
    # contact [at] domain.com
    (re.compile(
        r'([A-Za-z0-9][A-Za-z0-9._%+\-]*)'
        r'\s*[\[\(]\s*(?:at|AT|arobase|@)\s*[\]\)]\s*'
        r'([A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?'
        r'\.[A-Za-z]{2,15})',
        re.IGNORECASE
    ), r'\1@\2'),
    # contact @ domain . com (spaces around @ and dot)
    (re.compile(
        r'([A-Za-z0-9][A-Za-z0-9._%+\-]*)'
        r'\s+@\s+'
        r'([A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)'
        r'\s*\.\s*'
        r'([A-Za-z]{2,15})',
        re.IGNORECASE
    ), r'\1@\2.\3'),
    # contact(at)domain(dot)com - parentheses without brackets
    (re.compile(
        r'([A-Za-z0-9][A-Za-z0-9._%+\-]*)'
        r'\s*\(\s*at\s*\)\s*'
        r'([A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)'
        r'\s*\(\s*(?:dot|point)\s*\)\s*'
        r'([A-Za-z]{2,15})',
        re.IGNORECASE
    ), r'\1@\2.\3'),
]

# mailto: link pattern
MAILTO_REGEX = re.compile(
    r'mailto:([A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9.\-]+\.[A-Za-z]{2,15})',
    re.IGNORECASE
)


def clean_email(raw: str) -> Optional[str]:
    """
    Clean a raw regex match to extract the actual email.

    Fixes the core problem: in HTML text, emails get concatenated with
    surrounding content like "098775contact@nom-de-domaine.frDécouvrez".

    Strategy:
    0. Strip JSON/HTML Unicode escape prefixes (u003e → ">")
    1. Fix the TLD: if the captured TLD isn't valid, find the longest
       valid TLD prefix (e.g., "frDécouvrez" → "fr")
    2. Fix the local part: strip leading digit sequences that look like
       phone numbers (e.g., "098775contact" → "contact")
    """
    if '@' not in raw:
        return None

    # --- Strip Unicode escape artifacts ---
    # JSON-encoded HTML produces things like "u003epierremarie.desnoe@compta.com"
    # where u003e is the escaped ">" character
    raw = re.sub(r'^u[0-9a-fA-F]{4}', '', raw)

    local, domain = raw.rsplit('@', 1)

    # --- Fix TLD ---
    parts = domain.split('.')
    if len(parts) < 2:
        return None

    tld = parts[-1]
    # If TLD is not recognized, try to find a valid prefix
    if tld.lower() not in VALID_TLDS and tld.lower() not in FILE_EXTENSIONS:
        best_tld = None
        # Try longest-first (e.g., "consulting" before "co")
        for length in range(min(len(tld), 15), 1, -1):
            candidate = tld[:length].lower()
            if candidate in VALID_TLDS:
                best_tld = candidate
                break
        if best_tld:
            parts[-1] = best_tld
            domain = '.'.join(parts)
        else:
            return None  # No valid TLD found

    # --- Fix local part ---
    # Strip leading digit sequences that look like phone numbers concatenated
    # with the email. Pattern: 2+ digits followed by alpha chars → strip digits.
    # e.g. "098775contact" → "contact", "89contact" → "contact"
    # but "user123" stays (digits at end, not beginning)
    cleaned_local = re.sub(r'^\d{2,}(?=[a-zA-Z])', '', local)
    if not cleaned_local:
        return None  # Local part was all digits

    email = f"{cleaned_local}@{domain}".lower().strip()
    return email


def validate_email(email: str) -> bool:
    """Validate an extracted email address."""
    email = email.strip().lower()

    # Basic length checks
    if len(email) < 5 or len(email) > 254:
        return False

    if '@' not in email:
        return False

    local, domain = email.rsplit('@', 1)

    # Local part validation
    if not local or len(local) > 64:
        return False
    if local.startswith('.') or local.endswith('.'):
        return False
    if '..' in local:
        return False

    # Domain validation
    if not domain or len(domain) > 253:
        return False
    if domain.startswith('-') or domain.endswith('-'):
        return False
    if '..' in domain:
        return False

    # Extract TLD
    parts = domain.split('.')
    if len(parts) < 2:
        return False

    tld = parts[-1]

    # Reject file extensions
    if tld in FILE_EXTENSIONS:
        return False

    # Check TLD is valid (if we have a known set)
    # Allow unknown TLDs >= 2 chars to handle new TLDs
    if len(tld) < 2:
        return False

    # Reject obviously invalid patterns
    # No digits-only local parts (usually phone numbers captured by mistake)
    if local.replace('.', '').replace('-', '').isdigit():
        return False

    # Reject if domain is just an IP-like pattern
    if all(part.isdigit() for part in parts):
        return False

    # Reject Sentry DSN (hex hash @ *.ingest.*.sentry.io)
    if 'sentry.io' in domain:
        return False

    # Reject local parts that look like hashes (32+ hex chars = Sentry DSN, tracking IDs)
    if re.match(r'^[0-9a-f]{32,}$', local):
        return False

    # Reject common placeholder/invalid emails
    invalid_domains = {
        'example.com', 'example.org', 'example.net',
        'test.com', 'localhost', 'domain.com',
        'email.com', 'your-domain.com', 'yourdomain.com',
        'mail.com',  # example@mail.com
        'wixpress.com',
    }
    if domain in invalid_domains:
        return False

    # Reject French placeholder names (jean.dupont@gmail.com, jacques@martin.com, etc.)
    # These are the French equivalent of "John Doe" / "Jane Smith"
    _PLACEHOLDER_FIRSTNAMES = {
        'jean', 'jacques', 'pierre', 'paul', 'marie', 'dupont',
        'durand', 'martin', 'bernard', 'thomas', 'robert',
    }
    _PLACEHOLDER_PATTERNS = {
        # jean.dupont@gmail.com, jean.martin@gmail.com, etc.
        'jean.dupont', 'jean.martin', 'jean.durand', 'jean.bernard',
        'pierre.dupont', 'pierre.martin', 'pierre.durand',
        'marie.dupont', 'marie.martin', 'paul.dupont', 'paul.martin',
        'jacques.dupont', 'jacques.martin',
    }
    # Check full local part match against placeholder patterns
    if local in _PLACEHOLDER_PATTERNS:
        return False
    # Check pattern: placeholder_firstname@common_lastname_domain
    # e.g., jacques@martin.com (martin.com is a real domain but used as placeholder in FR sites)
    if local in _PLACEHOLDER_FIRSTNAMES and parts[0] in _PLACEHOLDER_FIRSTNAMES:
        return False

    return True


def extract_from_mailto(html: str) -> List[str]:
    """Extract emails from mailto: links."""
    return MAILTO_REGEX.findall(html)


def deobfuscate_emails(text: str) -> List[str]:
    """Find obfuscated email patterns and return deobfuscated emails."""
    results = []
    for pattern, replacement in OBFUSCATION_PATTERNS:
        matches = pattern.finditer(text)
        for match in matches:
            email = pattern.sub(replacement, match.group(0))
            email = email.strip()
            if validate_email(email):
                results.append(email.lower())
    return results


def extract_emails(text: str, html: str = "") -> List[str]:
    """
    Extract all emails from text content.

    Args:
        text: Plain text content (page text or raw HTML)
        html: Raw HTML for mailto extraction

    Returns:
        List of unique, validated email addresses
    """
    found = set()

    # 1. Extract from mailto: links (highest priority, most reliable)
    if html:
        for email in extract_from_mailto(html):
            email = email.strip().lower()
            if validate_email(email):
                found.add(email)
                logger.debug("Found mailto email: %s", email)

    # 2. Extract from obfuscated patterns
    for email in deobfuscate_emails(text):
        found.add(email)
        logger.debug("Found obfuscated email: %s", email)

    # 3. Extract with regex from text, then clean each candidate
    for match in EMAIL_REGEX.finditer(text):
        email = clean_email(match.group(1))
        if email and validate_email(email):
            found.add(email)
            logger.debug("Found regex email: %s", email)

    # 4. Also search HTML source (catches emails in attributes, comments, etc.)
    if html:
        for match in EMAIL_REGEX.finditer(html):
            email = clean_email(match.group(1))
            if email and validate_email(email):
                found.add(email)
                logger.debug("Found email in HTML source: %s", email)

    return sorted(found)
