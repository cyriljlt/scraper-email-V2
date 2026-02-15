"""Allow running with: python -m email_scraper"""

from .cli import main
import sys

sys.exit(main())
