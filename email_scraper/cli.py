"""CLI interface for the email scraper."""

import argparse
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import pandas as pd
from tqdm import tqdm

from . import __version__
from .scraper import EmailScraper, ScrapeResult, EmailResult
from .filter import filter_results

logger = logging.getLogger("email_scraper")


def setup_logging(verbosity: int, log_file: Optional[str] = None):
    """Configure logging based on verbosity level."""
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity >= 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(formatter)
    logger.addHandler(console)
    logger.setLevel(level)

    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)  # Always DEBUG in file
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)


def load_urls(input_path: str) -> List[str]:
    """Load URLs from a CSV file."""
    path = Path(input_path)
    if not path.exists():
        logger.error("Input file not found: %s", input_path)
        sys.exit(1)

    try:
        df = pd.read_csv(path)
    except Exception as e:
        logger.error("Failed to read CSV: %s", e)
        sys.exit(1)

    # Find URL column (case-insensitive)
    url_col = None
    for col in df.columns:
        if col.strip().lower() in ("url", "urls", "website", "site", "link"):
            url_col = col
            break

    if url_col is None:
        logger.error(
            "No URL column found in CSV. Expected one of: url, urls, website, site, link. "
            "Found columns: %s", list(df.columns)
        )
        sys.exit(1)

    urls = df[url_col].dropna().astype(str).str.strip().tolist()
    urls = [u for u in urls if u and u.lower() != "nan"]

    logger.info("Loaded %d URLs from %s (column: '%s')", len(urls), input_path, url_col)
    return urls


def _extract_root_domain(url: str) -> str:
    """Extract root domain from URL for deduplication.

    Normalizes: http/https, www prefix, trailing paths, query strings.
    Examples:
        https://www.tacher-acogex.com/nous-connaitre/falaise/ → tacher-acogex.com
        http://www.tacher-acogex.com/ → tacher-acogex.com
        tacher-acogex.com → tacher-acogex.com
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    netloc = urlparse(url).netloc.lower()
    # Strip www. prefix
    netloc = re.sub(r"^www\.", "", netloc)
    # Strip port if present
    netloc = netloc.split(":")[0]
    return netloc


def deduplicate_urls(urls: List[str]) -> List[str]:
    """Deduplicate URLs by root domain, keeping the first occurrence.

    When the CSV has multiple URLs for the same site (http vs https,
    with/without www, different subpages), keep only one per domain.
    """
    seen: Dict[str, str] = {}
    for url in urls:
        domain = _extract_root_domain(url)
        if domain and domain not in seen:
            seen[domain] = url
    deduped = list(seen.values())
    if len(deduped) < len(urls):
        logger.info(
            "Deduplicated %d URLs → %d unique domains (removed %d duplicates)",
            len(urls), len(deduped), len(urls) - len(deduped)
        )
    return deduped


def save_csv(results: List[EmailResult], output_path: str):
    """Save results to CSV."""
    if not results:
        logger.warning("No emails found - creating empty output file")
        df = pd.DataFrame(columns=["url", "email", "source_page", "confidence_score"])
    else:
        df = pd.DataFrame([
            {
                "url": r.url,
                "email": r.email,
                "source_page": r.source_page,
                "confidence_score": r.confidence_score,
            }
            for r in results
        ])

    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info("Results saved to %s (%d rows)", output_path, len(df))


def save_json(results: List[EmailResult], output_path: str):
    """Save results to JSON with full metadata."""
    data = [r.to_dict() for r in results]
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("JSON results saved to %s", output_path)


def send_webhook(webhook_url: str, results: List[EmailResult], duration: float):
    """Send completion notification via webhook."""
    import requests as req

    payload = {
        "status": "completed",
        "total_emails": len(results),
        "unique_domains": len(set(r.url for r in results)),
        "duration_seconds": round(duration, 2),
        "summary": [
            {"url": r.url, "email": r.email, "confidence": r.confidence_score}
            for r in results[:50]  # First 50 results in summary
        ],
    }

    try:
        resp = req.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Webhook notification sent to %s", webhook_url)
    except Exception as e:
        logger.warning("Failed to send webhook: %s", e)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="email-scraper",
        description="Extract emails from URLs listed in a CSV file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -i urls.csv -o results.csv
  %(prog)s -i urls.csv -o results.csv -t 4 --timeout 20 -vv
  %(prog)s -i urls.csv -o results.csv --json results.json --cache .cache
  %(prog)s -i urls.csv -o results.csv --dry-run -v
  %(prog)s -i urls.csv -o results.csv --webhook https://hooks.example.com/notify
        """,
    )

    parser.add_argument(
        "-i", "--input",
        required=True,
        help="Input CSV file with a 'url' column",
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        help="Output CSV file path",
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=1,
        help="Number of parallel threads (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="Request timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=1.0,
        help="Minimum delay between requests to same domain in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--cache",
        type=str,
        default=None,
        help="Cache directory for fetched pages (optional)",
    )
    parser.add_argument(
        "--json",
        type=str,
        default=None,
        dest="json_output",
        help="Also export results as JSON with full metadata (optional)",
    )
    parser.add_argument(
        "--webhook",
        type=str,
        default=None,
        help="Webhook URL for completion notification (optional)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.7,
        help="Minimum confidence score to keep an email (default: 0.7)",
    )
    parser.add_argument(
        "--max-per-site",
        type=int,
        default=1,
        help="Max emails to keep per site (default: 1, best for campaigns. 0 = unlimited)",
    )
    parser.add_argument(
        "--no-filter",
        action="store_true",
        help="Disable campaign filtering (keep all emails including agencies, DPO, etc.)",
    )
    parser.add_argument(
        "--no-robots",
        action="store_true",
        help="Ignore robots.txt restrictions",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect pages without extracting emails (test mode)",
    )
    parser.add_argument(
        "--user-agent",
        type=str,
        default=None,
        help="Custom User-Agent string",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v for INFO, -vv for DEBUG)",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Save logs to file (optional)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    return parser


def main(argv: Optional[List[str]] = None):
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Setup logging
    setup_logging(args.verbose, args.log_file)

    # Load URLs and deduplicate by domain
    urls = load_urls(args.input)
    if not urls:
        logger.error("No URLs to process")
        sys.exit(1)
    urls = deduplicate_urls(urls)

    # Create scraper
    scraper_kwargs = {
        "timeout": args.timeout,
        "respect_robots": not args.no_robots,
        "cache_dir": args.cache,
        "rate_limit": args.rate_limit,
    }
    if args.user_agent:
        scraper_kwargs["user_agent"] = args.user_agent

    start_time = time.time()
    all_emails: List[EmailResult] = []
    errors: List[str] = []

    if args.threads > 1:
        # Parallel execution
        logger.info("Starting parallel scraping with %d threads", args.threads)
        scraper = EmailScraper(**scraper_kwargs)

        with ThreadPoolExecutor(max_workers=args.threads) as executor:
            futures = {
                executor.submit(scraper.scrape_url, url, args.dry_run): url
                for url in urls
            }

            with tqdm(total=len(urls), desc="Scraping", unit="url") as pbar:
                for future in as_completed(futures):
                    url = futures[future]
                    try:
                        result = future.result()
                        all_emails.extend(result.emails)
                        errors.extend(result.errors)
                    except Exception as e:
                        logger.error("Unexpected error for %s: %s", url, e)
                        errors.append(f"Unexpected error for {url}: {e}")
                    pbar.update(1)
    else:
        # Sequential execution
        logger.info("Starting sequential scraping")
        scraper = EmailScraper(**scraper_kwargs)

        for url in tqdm(urls, desc="Scraping", unit="url"):
            try:
                result = scraper.scrape_url(url, args.dry_run)
                all_emails.extend(result.emails)
                errors.extend(result.errors)
            except Exception as e:
                logger.error("Unexpected error for %s: %s", url, e)
                errors.append(f"Unexpected error for {url}: {e}")

    duration = time.time() - start_time

    # Apply campaign filtering
    raw_count = len(all_emails)
    if not args.no_filter and not args.dry_run:
        all_emails = filter_results(
            all_emails,
            min_score=args.min_score,
            require_domain_match=True,
            max_per_site=args.max_per_site,
        )

    # Summary
    print(f"\n{'=' * 60}")
    print(f"Scraping complete")
    print(f"{'=' * 60}")
    print(f"  URLs processed:  {len(urls)}")
    print(f"  Emails found:    {raw_count}")
    if not args.no_filter and not args.dry_run:
        print(f"  After filtering: {len(all_emails)}  (min score: {args.min_score})")
        print(f"  Filtered out:    {raw_count - len(all_emails)}")
    print(f"  Errors:          {len(errors)}")
    print(f"  Duration:        {duration:.1f}s")
    print(f"{'=' * 60}")

    if not args.dry_run:
        # Save CSV
        save_csv(all_emails, args.output)

        # Save JSON if requested
        if args.json_output:
            save_json(all_emails, args.json_output)

        # Send webhook if configured
        if args.webhook:
            send_webhook(args.webhook, all_emails, duration)

    # Log errors summary
    if errors:
        logger.warning("Encountered %d error(s):", len(errors))
        for err in errors[:20]:  # Show first 20 errors
            logger.warning("  - %s", err)
        if len(errors) > 20:
            logger.warning("  ... and %d more", len(errors) - 20)

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
