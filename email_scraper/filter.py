"""Campaign-ready email filtering.

Filters out emails that are not useful for B2B email campaigns:
- Web agencies, hosting providers, free email providers
- Non-business roles (DPO, RGPD, noreply, abuse, etc.)
- Emails whose domain doesn't match the target site
- Low-confidence emails below a score threshold
"""

import re
import logging
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Hosted website builder / platform domains.
# Sites on these platforms use the platform's domain, so the business email
# will naturally have a DIFFERENT domain. Domain match should be skipped.
HOSTED_PLATFORM_DOMAINS = {
    "site-solocal.com",
    "solocal.com",
    "wixsite.com",
    "weebly.com",
    "squarespace.com",
    "jimdo.com",
    "webnode.fr",
    "webnode.com",
    "monsite-orange.fr",
    "e-monsite.com",
    "sitew.com",
    "hubside.fr",
    "wordpress.com",
    "blogspot.com",
    "shopify.com",
    "strikingly.com",
}


def _is_hosted_platform(domain: str) -> bool:
    """Check if a domain belongs to a hosted website builder."""
    domain = domain.lower()
    for platform in HOSTED_PLATFORM_DOMAINS:
        if domain == platform or domain.endswith("." + platform):
            return True
    return False


# Free email / ISP / webmail domains — never useful for B2B campaigns
FREE_EMAIL_DOMAINS = {
    # French ISPs
    "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "bbox.fr",
    "numericable.fr", "laposte.net", "neuf.fr", "alice.fr",
    # Global webmail
    "gmail.com", "googlemail.com", "outlook.com", "outlook.fr",
    "hotmail.com", "hotmail.fr", "live.com", "live.fr",
    "yahoo.com", "yahoo.fr", "ymail.com",
    "msn.com", "aol.com", "aol.fr", "icloud.com", "me.com",
    "mail.com", "protonmail.com", "proton.me", "tutanota.com",
    "gmx.com", "gmx.fr", "zoho.com",
}

# Hosting / registrar / SaaS tool domains — not the business itself
TOOL_DOMAINS = {
    "ovh.com", "ovh.net", "gandi.net", "ionos.com", "1and1.com",
    "wix.com", "wixpress.com", "squarespace.com", "wordpress.com",
    "hubspot.com", "mailchimp.com", "sendinblue.com", "brevo.com",
    "sentry.io", "cloudflare.com", "amazonaws.com",
    "google.com", "microsoft.com", "apple.com",
}

# Email local parts that are NOT useful for campaigns
# These are functional/compliance roles, not decision-makers or contacts
NON_CAMPAIGN_LOCAL_PARTS = {
    # Privacy/compliance
    "dpo", "rgpd", "gdpr", "privacy", "donnees-personnelles",
    # System/technical
    "abuse", "postmaster", "hostmaster", "webmaster", "root",
    "noreply", "no-reply", "no_reply", "ne-pas-repondre",
    "mailer-daemon", "daemon",
    # Too generic / useless
    "test", "dev", "staging", "demo",
}


def is_campaign_worthy(
    email: str,
    target_domain: str,
    require_domain_match: bool = True,
) -> bool:
    """
    Check if an email is suitable for a B2B email campaign.

    Args:
        email: The email address to check
        target_domain: The domain of the website being scraped
        require_domain_match: If True, reject emails from different domains

    Returns:
        True if the email should be kept for campaigns
    """
    if "@" not in email:
        return False

    local, domain = email.rsplit("@", 1)
    domain = domain.lower()
    local = local.lower()

    # Strip www for comparison
    clean_target = re.sub(r"^www\.", "", target_domain.lower())
    clean_email_domain = re.sub(r"^www\.", "", domain)

    # 1. Reject free email / ISP domains
    if clean_email_domain in FREE_EMAIL_DOMAINS:
        logger.debug("Filtered %s: free email provider", email)
        return False

    # 2. Reject known tool/hosting domains
    if clean_email_domain in TOOL_DOMAINS:
        logger.debug("Filtered %s: tool/hosting domain", email)
        return False

    # 3. Reject non-campaign local parts
    # Check exact match and prefix match (e.g., "noreply-xxx@")
    if local in NON_CAMPAIGN_LOCAL_PARTS:
        logger.debug("Filtered %s: non-campaign role", email)
        return False
    for prefix in NON_CAMPAIGN_LOCAL_PARTS:
        if local.startswith(prefix + "-") or local.startswith(prefix + "_"):
            logger.debug("Filtered %s: non-campaign role prefix", email)
            return False

    # 4. Domain match check
    # Skip domain match for hosted platforms (Solocal, Wix, etc.)
    # where the business email naturally uses a different domain.
    if require_domain_match and not _is_hosted_platform(clean_target):
        # Allow exact match or subdomain relationship
        if clean_email_domain != clean_target:
            if not (
                clean_target.endswith("." + clean_email_domain)
                or clean_email_domain.endswith("." + clean_target)
            ):
                logger.debug(
                    "Filtered %s: domain mismatch (email=%s, target=%s)",
                    email, clean_email_domain, clean_target,
                )
                return False

    return True


def filter_results(
    results: list,
    min_score: float = 0.7,
    require_domain_match: bool = True,
    max_per_site: int = 1,
) -> list:
    """
    Filter email results for campaign use.

    Args:
        results: List of EmailResult objects
        min_score: Minimum confidence score (0.0-1.0)
        require_domain_match: If True, only keep emails matching site domain
        max_per_site: Max emails to keep per input URL/site (0 = unlimited)

    Returns:
        Filtered list of EmailResult objects
    """
    original_count = len(results)
    filtered = []

    for r in results:
        # Score threshold
        if r.confidence_score < min_score:
            logger.debug(
                "Filtered %s: score %.2f < %.2f",
                r.email, r.confidence_score, min_score,
            )
            continue

        # Extract target domain from the input URL
        target_domain = _extract_domain(r.url)

        # Campaign worthiness check
        if not is_campaign_worthy(r.email, target_domain, require_domain_match):
            continue

        filtered.append(r)

    # Limit emails per site: keep the best N per input URL
    if max_per_site > 0:
        filtered = _limit_per_site(filtered, max_per_site)

    removed = original_count - len(filtered)
    if removed > 0:
        logger.info(
            "Filtering: %d → %d emails (%d removed)",
            original_count, len(filtered), removed,
        )

    return filtered


# Priority for selecting the best email per site.
# Lower number = higher priority. "contact@" is ideal for cold outreach.
_LOCAL_PART_PRIORITY = {
    "contact": 0,
    "info": 1,
    "hello": 2,
    "bonjour": 3,
    "accueil": 4,
    "commercial": 5,
    "direction": 6,
}


def _email_campaign_sort_key(result) -> tuple:
    """
    Sort key to pick the best email for campaigns.

    Priority order:
    1. Generic contact emails (contact@, info@, hello@) — best for cold outreach
    2. Highest confidence score
    3. Found on contact page (vs legal/about)
    """
    local = result.email.split("@")[0].lower()
    # Priority bucket: known generic roles get low number (= high priority)
    priority = _LOCAL_PART_PRIORITY.get(local, 50)
    # For non-generic emails, prefer firstname.lastname over random strings
    if priority == 50 and "." in local:
        priority = 30  # firstname.lastname = decent for campaigns
    # Page type bonus: contact page results are more reliable
    page_bonus = 0
    if "contact" in result.source_page.lower():
        page_bonus = 1
    # Sort: lower priority number first, then higher score, then contact page
    return (priority, -result.confidence_score, -page_bonus)


def _limit_per_site(results: list, max_per_site: int) -> list:
    """Keep only the best N emails per input URL (site)."""
    from collections import defaultdict

    by_site: Dict[str, list] = defaultdict(list)
    for r in results:
        site_key = _extract_domain(r.url)
        by_site[site_key].append(r)

    limited = []
    for site, emails in by_site.items():
        emails.sort(key=_email_campaign_sort_key)
        kept = emails[:max_per_site]
        limited.extend(kept)
        if len(emails) > max_per_site:
            logger.debug(
                "%s: kept %d/%d emails (best: %s)",
                site, max_per_site, len(emails), kept[0].email,
            )

    # Preserve original ordering (by confidence desc)
    limited.sort(key=lambda r: r.confidence_score, reverse=True)
    return limited


def _extract_domain(url: str) -> str:
    """Extract clean domain from URL."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    netloc = urlparse(url).netloc.lower()
    netloc = re.sub(r"^www\.", "", netloc)
    netloc = netloc.split(":")[0]
    return netloc
