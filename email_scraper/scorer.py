"""Confidence scoring for extracted emails."""

import re
import logging
from urllib.parse import urlparse
from typing import Optional

logger = logging.getLogger(__name__)


def compute_confidence(
    email: str,
    page_url: str,
    page_text: str,
    page_html: str,
    source_type: str = "regex",
) -> float:
    """
    Compute a confidence score (0.0 to 1.0) for an extracted email.

    Scoring factors:
    - Source method (mailto > obfuscated > regex)
    - Domain match (email domain matches site domain)
    - Page type (contact page > legal > about > homepage)
    - Context (near contact-related keywords)
    - Position (header/footer vs deep in content)

    Args:
        email: The extracted email address
        page_url: URL of the page where email was found
        page_text: Plain text of the page
        page_html: Raw HTML of the page
        source_type: How the email was found (mailto, obfuscated, regex)

    Returns:
        Confidence score between 0.0 and 1.0
    """
    score = 0.0

    # --- Factor 1: Source method (max 0.25) ---
    if source_type == "mailto":
        score += 0.25
    elif source_type == "obfuscated":
        score += 0.20
    else:
        score += 0.10

    # --- Factor 2: Domain matching (max 0.30, can be negative) ---
    email_domain = email.split("@")[1] if "@" in email else ""
    site_domain = urlparse(page_url).netloc.lower()

    # Remove www prefix for comparison
    clean_site = re.sub(r"^www\.", "", site_domain)
    clean_email_domain = re.sub(r"^www\.", "", email_domain)

    domain_match = "different"
    if clean_email_domain == clean_site:
        score += 0.30  # Perfect domain match
        domain_match = "exact"
    elif clean_site in clean_email_domain or clean_email_domain in clean_site:
        score += 0.20  # Partial match (subdomain)
        domain_match = "partial"
    else:
        # Different domain: likely a third-party (web agency, service provider)
        # Strong penalty — these are almost never the business's own email
        score -= 0.25
        domain_match = "different"

    # --- Factor 3: Page type (max 0.20) ---
    path = urlparse(page_url).path.lower()
    if any(kw in path for kw in ["/contact", "/contacter", "/contactez"]):
        score += 0.20
    elif any(kw in path for kw in ["/mentions", "/legal", "/impressum", "/cgu"]):
        score += 0.15
    elif any(kw in path for kw in ["/about", "/propos", "/equipe", "/team"]):
        score += 0.12
    else:
        score += 0.08  # Homepage or other page

    # --- Factor 3b: Extra penalty for third-party emails on legal/mentions pages ---
    # Web agencies are almost always found in mentions légales, not on contact pages.
    # An email with a different domain on a legal page is very likely a web agency.
    if domain_match == "different":
        is_legal_page = any(
            kw in path for kw in ["/mentions", "/legal", "/impressum", "/cgu",
                                  "/confidentialite", "/privacy", "/rgpd",
                                  "/donnees-personnelles"]
        )
        if is_legal_page:
            score -= 0.15  # Extra penalty: different domain on legal page

    # --- Factor 4: Semantic context (max 0.15) ---
    context_score = _check_context(email, page_text)
    score += context_score * 0.15

    # --- Factor 5: Email local part quality (max 0.10) ---
    local_part = email.split("@")[0] if "@" in email else ""
    quality_score = _assess_local_part(local_part)
    score += quality_score * 0.10

    final_score = round(max(min(score, 1.0), 0.0), 2)
    logger.debug("Confidence for %s on %s: %.2f", email, page_url, final_score)
    return final_score


def _check_context(email: str, text: str) -> float:
    """Check if the email appears near contact-related keywords."""
    if not text:
        return 0.3  # Neutral if no text

    # Find email position in text
    email_lower = email.lower()
    text_lower = text.lower()
    pos = text_lower.find(email_lower)

    if pos == -1:
        return 0.3  # Email not in plain text (found in HTML source)

    # Get surrounding context (200 chars before and after)
    context_start = max(0, pos - 200)
    context_end = min(len(text_lower), pos + len(email_lower) + 200)
    context = text_lower[context_start:context_end]

    # Contact-related keywords (French + English)
    contact_keywords = [
        r"contact", r"email", r"e-mail", r"courriel", r"mail",
        r"[eé]cri(?:re|vez)", r"joindre", r"reach", r"write",
        r"t[eé]l[eé]phone", r"phone", r"appel",
        r"adresse", r"address",
        r"formulaire", r"form",
        r"service\s+client", r"customer\s+service",
        r"support", r"assistance",
        r"renseignement", r"information",
    ]

    matches = sum(1 for kw in contact_keywords if re.search(kw, context))
    # Normalize: 3+ keyword matches = perfect context score
    return min(matches / 3.0, 1.0)


def _assess_local_part(local: str) -> float:
    """Assess the quality/professionalism of the email local part."""
    if not local:
        return 0.0

    # Professional patterns score higher
    professional = [
        r"^contact$", r"^info$", r"^hello$", r"^bonjour$",
        r"^support$", r"^commercial$", r"^direction$",
        r"^admin$", r"^webmaster$", r"^postmaster$",
        r"^service", r"^accueil$", r"^reception$",
        r"^rh$", r"^hr$", r"^recrutement$",
        r"^communication$", r"^presse$", r"^press$",
        r"^sales$", r"^vente", r"^marketing$",
    ]

    for pattern in professional:
        if re.match(pattern, local, re.IGNORECASE):
            return 1.0

    # Name-like patterns (firstname.lastname) are good
    if re.match(r"^[a-z]+\.[a-z]+$", local, re.IGNORECASE):
        return 0.8

    # Simple alphanumeric is okay
    if re.match(r"^[a-z][a-z0-9._-]+$", local, re.IGNORECASE):
        return 0.6

    return 0.4
