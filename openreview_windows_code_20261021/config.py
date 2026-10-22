from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / 'data' / 'raw'
PROCESSED = ROOT / 'data' / 'processed'
REPORT = ROOT / 'reports' / 'collection_report.md'
API = 'https://api2.openreview.net'
YEARS = (2024, 2025)
TIMEOUT = 30
ATTEMPTS = 3
PAGE_SIZE = 100
DELAY = 1.0
