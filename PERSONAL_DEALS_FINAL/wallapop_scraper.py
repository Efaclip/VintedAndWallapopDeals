
import asyncio
import csv
import json
from pathlib import Path
from urllib.parse import quote
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from database import initialize_database, save_listings, connect
OUTPUT_FILE = Path("wallapop_listings.csv")

# Keep False while diagnosing. Change to True later to hide the browser.
HEADLESS = False

MAX_LOAD_MORE_CLICKS = 10
MAX_DETAIL_PAGES = 100


async def accept_consent(page):
    for label in ["Aceptar todo", "Accept all", "Aceptar", "Accept"]:
        try:
            button = page.get_by_role(
                "button", name=label, exact=False
            ).first
            if await button.is_visible(timeout=700):
                await button.click(timeout=2500)
                print(f"Consent accepted: {label}")
                await page.wait_for_timeout(800)
                return
        except Exception:
            pass

async def collect_visible(page, listings):
    links = page.locator('a[href*="/item/"]')
    count = await links.count()
    added = 0

    for i in range(count):
        link = links.nth(i)

        try:
            href = await link.get_attribute("href")
            if not href or "/item/" not in href:
                continue

            title = " ".join((await link.inner_text()).split())
            if not title:
                title = (await link.get_attribute("aria-label") or "").strip()
            if not title:
                continue

            # Find a nearby card containing the title and price.
            card = link
            card_text = title

            for _ in range(5):
                parent = card.locator("xpath=..").first
                text = await parent.inner_text(timeout=1000)

                if title in text and len(text) < 2000:
                    card = parent
                    card_text = text
                    break

                card = parent

            
            price = ""
            for line in card_text.splitlines():
                line = line.strip()
                if "\u20ac" in line or "$" in line or "\u00a3" in line:
                    price = line
                    break

            # Fallback: search nearby parent elements if the card text had no price.
            if not price:
                for level in range(1, 7):
                    try:
                        parent = link.locator("xpath=" + "/.." * level).first
                        text = await parent.inner_text(timeout=1000)
                        for line in text.splitlines():
                            line = line.strip()
                            if "\u20ac" in line or "$" in line or "\u00a3" in line:
                                price = line
                                break
                        if price:
                            break
                    except Exception:
                        continue


            if href.startswith("/"):
                href = "https://es.wallapop.com" + href

            key = href.split("?")[0]

            if key not in listings:
                listings[key] = {
                    "title": title,
                    "price": price,
                    "description": "",
                    "url": key,
                }
                added += 1

        except Exception:
            continue

    return added


async def extract_description(page):
    try:
        body_text = await page.locator("body").inner_text()

        start_marker = "Descripción"
        end_marker = "Características"

        start = body_text.find(start_marker)

        if start == -1:
            return ""

        start += len(start_marker)

        end = body_text.find(end_marker, start)

        if end == -1:
            return ""

        if end <= start:
            return ""

        description = body_text[start:end].strip()

        if len(description) < 10:
            return ""

        lines = [
            line.strip()
            for line in description.splitlines()
            if line.strip()
        ]

        return "\n".join(lines)

    except Exception as error:
        print(f"Description extraction error: {error}")
        return ""

async def refresh_descriptions(browser):
    with connect() as connection:
        rows = connection.execute("""
            SELECT id, url, description
            FROM listings
            WHERE source = 'wallapop'
            ORDER BY id
        """).fetchall()

    print(f"Found {len(rows)} saved listings to check.")

    detail_page = await browser.new_page(locale="es-ES")
    refreshed = 0
    failed = 0

    for index, row in enumerate(rows, start=1):
        try:
            await detail_page.goto(
                row["url"],
                wait_until="domcontentloaded",
                timeout=25000,
            )
            await detail_page.wait_for_timeout(500)

            if any(word in detail_page.url.lower()
                   for word in ["captcha", "blocked", "access-denied"]):
                print("Access restriction detected; stopping.")
                break

            new_description = await extract_description(detail_page)
            old_description = row["description"] or ""

            if len(new_description) > len(old_description):
                with connect() as connection:
                    connection.execute("""
                        UPDATE listings
                        SET description = ?
                        WHERE id = ?
                    """, (new_description, row["id"]))

                refreshed += 1
                status = f"UPDATED ({len(old_description)} -> {len(new_description)} chars)"
            else:
                status = "unchanged"

            print(f"[{index}/{len(rows)}] ID {row['id']}: {status}")
            await detail_page.wait_for_timeout(1000)

        except PlaywrightTimeoutError:
            failed += 1
            print(f"[{index}/{len(rows)}] ID {row['id']}: timed out")

        except Exception as error:
            failed += 1
            print(f"[{index}/{len(rows)}] ID {row['id']}: {error}")

    await detail_page.close()

    print("\nDescription refresh complete.")
    print(f"Updated: {refreshed}")
    print(f"Failed: {failed}")
    print(f"Checked: {len(rows)}")

async def main():

    initialize_database()

    mode = input(
        "Choose mode:\n"
        "1 - Normal search\n"
        "2 - Refresh saved descriptions\n"
        "Enter 1 or 2: "
    ).strip()

    if mode not in ("1", "2"):
        print("Invalid choice.")
        return

    search = ""

    if mode == "1":
        search = input("What do you want to search for? ").strip()
        if not search:
            print("Enter a search term.")
            return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page(locale="es-ES")

        url = f"https://es.wallapop.com/search?keywords={quote(search)}"
        print(f"Opening search: {url}")

        if mode == "2":
            print("Starting saved-description refresh...")

            await refresh_descriptions(browser)

            await browser.close()
            return

        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=45000
            )
        except PlaywrightTimeoutError:
            print("Page navigation timed out; checking the page.")

        await page.wait_for_timeout(1500)
        await accept_consent(page)

        print("Page:", page.url)
        print("Waiting for listings...")

        try:
            await page.locator('a[href*="/item/"]').first.wait_for(
                state="visible", timeout=15000
            )
        except PlaywrightTimeoutError:
            print("No listing links detected.")
            print("Check the browser for an error or access restriction.")
            if not HEADLESS:
                input("If results are visible, press ENTER to retry: ")
            await accept_consent(page)

        listings = {}
        await collect_visible(page, listings)
        print(f"Found {len(listings)} listing links.")

        # Load additional search results when the button is available.
        for click_number in range(MAX_LOAD_MORE_CLICKS):
            button = None

            for label in ["Cargar m\u00e1s", "Load more", "Ver m\u00e1s"]:
                try:
                    candidate = page.get_by_text(
                        label, exact=False
                    ).last
                    if await candidate.is_visible(timeout=700):
                        button = candidate
                        break
                except Exception:
                    pass

            if button is None:
                print("No more load-more button found.")
                break

            try:
                await button.scroll_into_view_if_needed()
                await button.click(timeout=3000)
                await page.wait_for_timeout(1200)
                added = await collect_visible(page, listings)
                print(
                    f"Load {click_number + 1}: "
                    f"{len(listings)} listings (+{added})"
                )

                if added == 0:
                    print("No new listings appeared; stopping.")
                    break

            except Exception as error:
                print(f"Could not load more: {error}")
                break

        # Visit each listing page to retrieve its description.
        detail_page = await browser.new_page(locale="es-ES")
        urls = list(listings.keys())[:MAX_DETAIL_PAGES]

        print(f"\nReading descriptions from {len(urls)} listing pages...")

        for index, listing_url in enumerate(urls, start=1):
            try:
                await detail_page.goto(
                    listing_url,
                    wait_until="domcontentloaded",
                    timeout=25000,
                )
                await detail_page.wait_for_timeout(500)

                if any(word in detail_page.url.lower()
                       for word in ["captcha", "blocked", "access-denied"]):
                    print("Access restriction detected; stopping.")
                    break

                description = await extract_description(detail_page)
                listings[listing_url]["description"] = description
                print(f"Description for {listing_url}: {len(description)} characters")

                status = "description found" if description else "no description found"
                print(f"[{index}/{len(urls)}] {status}")

                # Keep requests sequential and modest.
                await detail_page.wait_for_timeout(1000)

            except PlaywrightTimeoutError:
                print(f"[{index}/{len(urls)}] Page timed out; skipping.")
            except Exception as error:
                print(f"[{index}/{len(urls)}] Could not read page: {error}")

        result = save_listings(listings, source="wallapop")
        print("Database results:", result)

        with OUTPUT_FILE.open(
            "w", newline="", encoding="utf-8-sig"
        ) as file:
            writer = csv.DictWriter(
                file, fieldnames=["title", "price", "description"]
            )
            writer.writeheader()

            for listing in listings.values():
                writer.writerow({
                    "title": listing["title"],
                    "price": listing["price"],
                    "description": listing["description"],
                })

        found_descriptions = sum(
            bool(item["description"]) for item in listings.values()
        )

        print(f"\nExported {len(listings)} listings.")
        print(f"Descriptions found: {found_descriptions}")
        print(f"CSV: {OUTPUT_FILE.resolve()}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
