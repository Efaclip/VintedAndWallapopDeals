"""
Scraper fixture tests - NO browser, NO network.

Run:  python3 tests/test_scraper_fixtures.py

extract_description is tested against fake page objects carrying fixture
HTML text, so the parsing logic is verified without touching Wallapop.
collect_visible is DOM-bound and stays untested here by design; the real
browser flow remains the user's to run.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wallapop_scraper import extract_description
from database import parse_price, save_listings, initialize_database

RESULTS = []


def check(name, condition):
    RESULTS.append((name, bool(condition)))
    print(("PASS" if condition else "FAIL"), name)


class FakeLocator:
    def __init__(self, text):
        self._text = text

    async def inner_text(self):
        return self._text


class FakePage:
    def __init__(self, body_text):
        self._body_text = body_text

    def locator(self, selector):
        assert selector == "body"
        return FakeLocator(self._body_text)


LISTING_PAGE = """
Wallapop
MSI RTX 4060 Ti 8GB Ventus 2X Black y Noctua U12S
260€
Descripción
Se vende tarjeta grafica Nvidia RTX 4060 Ti de 8 GB de memoria
edicion MSI Ventus 2x Black en perfectas condiciones, como nueva.
Incluye disipador noctua U12S con anclajes AM4.
Envios por Wallapop
Características
Tipo: Grafica
Estado: Como nuevo
"""

NO_DESCRIPTION_PAGE = """
Wallapop
iPhone 13
400€
Características
Estado: Bueno
"""

SHORT_DESCRIPTION_PAGE = """
Descripción
ok
Características
"""


async def run_fixtures():
    description = await extract_description(FakePage(LISTING_PAGE))
    check("description extracted between markers",
          "RTX 4060 Ti de 8 GB" in description and "Envios por Wallapop" in description)
    check("marker text excluded",
          "Descripción" not in description and "Características" not in description)
    check("page chrome excluded", "Wallapop\n" not in description[:10])

    check("missing markers -> empty",
          await extract_description(FakePage(NO_DESCRIPTION_PAGE)) == "")
    check("too-short description -> empty",
          await extract_description(FakePage(SHORT_DESCRIPTION_PAGE)) == "")
    check("empty page -> empty",
          await extract_description(FakePage("")) == "")


asyncio.run(run_fixtures())

# price strings exactly as the scraper captures them from cards
check("card price '260€'", parse_price("260€") == 260.0)
check("card price '1.200€'", parse_price("1.200€") == 1200.0)
check("card price with label", parse_price("450€ Negociable") == 450.0)
check("empty card price", parse_price("") is None)

failed = [name for name, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} scraper fixture checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
print("ALL SCRAPER FIXTURE CHECKS PASSED")
