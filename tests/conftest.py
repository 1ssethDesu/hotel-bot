import os
import sys
import tempfile
from pathlib import Path

# Isolated data folder for every test run (must be set before the app is imported)
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="hotelbot-test-")
os.environ["FB_PAGE_ACCESS_TOKEN"] = ""
os.environ["FB_APP_SECRET"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
