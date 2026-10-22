"""Download current files referenced by locally saved ICLR Notes only."""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.iclr_attachments import main

if __name__=='__main__':
    raise SystemExit(main())
