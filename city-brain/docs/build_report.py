"""Rebuild docs/report-brain.pdf from docs/src/report-brain.html (needs: pip install playwright && playwright install chromium)."""
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
FOOTER = ('<div style="width:100%;font:7pt Inter,sans-serif;color:#8a88aa;padding:0 17mm;display:flex;justify-content:space-between">'
          '<span>Triplet 07 · City Brain report · TSYP14 Phase 1</span><span><span class=pageNumber></span> / <span class=totalPages></span></span></div>')

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page()
    page.goto((HERE / "src" / "report-brain.html").as_uri(), wait_until="networkidle")
    page.pdf(path=str(HERE / "report-brain.pdf"), format="A4", print_background=True, display_header_footer=True,
             header_template="<span></span>", footer_template=FOOTER,
             margin={"top": "16mm", "bottom": "16mm", "left": "17mm", "right": "17mm"})
    b.close()
print("docs/report-brain.pdf written")
