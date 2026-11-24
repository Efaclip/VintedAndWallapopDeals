import json
import re


def first_match(patterns, text, flags=re.IGNORECASE):
    for pattern in patterns:
        match = re.search(pattern, text or "", flags)
        if match:
            return match.group(1).strip()
    return None



def clean_processor(value):
    if not value:
        return None

    value = " ".join(value.split())
    value = re.sub(r"^Apple\s+", "", value, flags=re.I)

    # Canonical Apple Silicon casing: "m2" -> "M2", "m4 pro" -> "M4 Pro".
    apple_chip = re.fullmatch(
        r"[Mm]([1-9])(?:\s+(Pro|Max|Ultra))?", value
    )
    if apple_chip:
        suffix = f" {apple_chip.group(2).title()}" if apple_chip.group(2) else ""
        return f"M{apple_chip.group(1)}{suffix}"

    # Collapse repeated Core prefixes.
    value = re.sub(
        r"^(?:Core\s+)+i([3579])$",
        r"Core i\1",
        value,
        flags=re.I
    )

    # Add Core only when it is missing.
    if re.fullmatch(r"i[3579]", value, re.I):
        value = "Core " + value

    return value



def extract_processor(text):
    patterns = [
        r"\b(M[1-5]\s*(?:Pro|Max|Ultra)?)\b",
        r"\b(Core\s+Ultra\s+[3579]\s*\d{3}[A-Z]*)\b",
        r"\b(Core\s+i[3579](?:-\d{4,5}[A-Z]*)?)\b",
        r"\b(i[3579]-\d{4,5}[A-Z]*)\b",
        r"\b(i[3579])\b",
        r"\b(Ryzen\s+[3579]\s*\d{4,5}[A-Z]*)\b",
    ]
    return clean_processor(first_match(patterns, text))


def extract_gpu(text):
    return first_match([
        r"\b(RTX\s?\d{4}(?:\s?Ti\s?SUPER|\s?SUPER|\s?Ti)?)\b",
        r"\b(GTX\s?\d{4}(?:\s?Ti)?)\b",
        r"\b(RX\s?\d{4}(?:\s?XT|\s?XTX)?)\b",
        r"\b(ARC\s?[AB]\d{3})\b",
    ], text)

def extract_ram(text):
    text = text or ""

    # Reversed storage/RAM pair, e.g. 512GB 16GB
    reversed_pair = re.search(
        r"\b(\d{3,5})\s?GB\s+(\d{1,3})\s?GB\b",
        text,
        re.I
    )

    if reversed_pair:
        storage = int(reversed_pair.group(1))
        ram = int(reversed_pair.group(2))

        valid_ram = (4, 8, 12, 16, 18, 24, 32, 36, 48, 64, 96, 128)

        if storage >= 128 and ram in valid_ram:
            return ram

    patterns = [
    # Number followed by an explicit RAM label
    r"\b(\d{1,3})\s?GB\s*(?:de\s+)?(?:RAM|memoria|memory)\b",

    # Explicit RAM label followed by the number
    r"\b(?:RAM|memoria(?:\s+RAM)?|memory)\s*(?:de|of|:)?\s*(\d{1,3})\s?GB\b",

    # DDR memory
    r"\b(\d{1,3})\s?GB\s*DDR[345X]?\b",

    # RAM/storage pair
    r"\b(\d{1,3})\s?GB\s*/\s*\d{2,5}\s?GB\b",
    ]

    value = first_match(patterns, text)

    if value:
        number = int(value)
        if number in (4, 8, 12, 16, 18, 24, 32, 36, 48, 64, 96, 128):
            return number

    # Standalone RAM is accepted only when no larger storage
    # capacity appears elsewhere in the text.
    standalone = re.search(
        r"\b(4|8|12|16|18|24|32|36|48|64|96)\s?GB\b",
        text,
        re.I
    )

    if standalone:
        has_storage = re.search(
            r"\b(?:128|256|512|1024|2048|4096)\s?GB\b|\b\d+(?:[.,]\d+)?\s?TB\b",
            text,
            re.I
        )

        if not has_storage:
            return int(standalone.group(1))

    return None

def extract_storage(text):
    text = text or ""

    # Reversed storage/RAM pair, e.g. 512GB 16GB
    reversed_pair = re.search(
        r"\b(\d{3,5})\s?GB\s+(\d{1,3})\s?GB\b",
        text,
        re.I
    )

    if reversed_pair:
        storage = int(reversed_pair.group(1))
        ram = int(reversed_pair.group(2))

        valid_ram = (4, 8, 12, 16, 18, 24, 32, 36, 48, 64, 96, 128)

        if storage >= 128 and ram in valid_ram:
            return storage

    # Explicit RAM/storage pair, e.g. 16GB/512GB
    pair = re.search(
        r"\b(\d{1,3})\s?GB\s*/\s*(\d{2,5})\s?GB\b",
        text,
        re.I
    )
    if pair:
        return int(pair.group(2))

    # Terabytes
    tb = re.search(
        r"\b(\d+(?:[.,]\d+)?)\s?TB\b",
        text,
        re.I
    )
    if tb:
        return int(float(tb.group(1).replace(",", ".")) * 1024)

    # Explicit storage labels
    patterns = [
        r"\b(?:almacenamiento|storage|disco(?:\s+duro)?|SSD|NVME|HDD)"
        r"\s*(?:de|of|:)?\s*(\d{2,5})\s?GB\b",

        r"\b(\d{2,5})\s?GB\s*(?:SSD|NVME|HDD)\b",

        r"\b(\d{2,5})\s*(?:GB)?\s*(?:de\s+)?almacenamiento\b",
    ]

    value = first_match(patterns, text)

    if value:
        return int(value)

    # Standalone storage capacity, e.g. "MacBook Air 256GB"
    standalone = re.search(
        r"\b(128|256|512|1024|2048|4096)\s?GB\b",
        text,
        re.I
    )

    if standalone:
        return int(standalone.group(1))

    return None

def extract_screen(text):
    if not text:
        return None

    patterns = [
        # 13.6-inch, 13.6 inch, 13.6 pulgadas
        r"\b(\d{1,2}(?:[.,]\d)?)\s*(?:-?inch(?:es)?|pulgadas)\b",

        # 13", 13.6", 13” or 13.6”
        r"\b(\d{1,2}(?:[.,]\d)?)\s*[\"”]",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            value = float(match.group(1).replace(",", "."))

            # Accept plausible laptop/tablet screen sizes only.
            if 7 <= value <= 20:
                return value

    return None



def extract_edition(text):
    return first_match([
        r"\b(Digital\s+Edition)\b", r"\b(Slim)\b", r"\b(Digital)\b"
    ], text)



def extract_from_both(title, description, extractor, field):
    title_value = extractor(title)
    description_value = extractor(description)

    # Normalize processor names before comparing them.
    if field == "processor":
        title_value = clean_processor(title_value)
        description_value = clean_processor(description_value)

    # A bare M4 in the title may be clarified as M4 Pro in the description.
    if field == "processor" and title_value and description_value:
        if (
            re.fullmatch(r"M[1-5]", title_value, re.I)
            and description_value.startswith(title_value + " ")
        ):
            return description_value, "description", None

    if (
        title_value is not None
        and description_value is not None
        and title_value != description_value
    ):
        return title_value, "title", [title_value, description_value]

    if title_value is not None:
        return title_value, "title", None

    if description_value is not None:
        return description_value, "description", None

    return None, None, None




def detect_category(title, description=""):
    title = title or ""
    description = description or ""

    # Use the title as the primary indicator of what is being sold.
    title_lower = title.lower()
    text = f"{title} {description}".lower()

    accessory_pattern = (
        r"\b(charger|charging cable|cargador|cable|adaptador|adapter|"
        r"funda|case|sleeve|dock|docking station|usb hub|hub usb|"
        r"soporte|stand|stylus|pencil|screen protector|protector de pantalla|"
        r"keyboard|teclado|mouse|rat[oó]n|auriculares|headphones)\b"
    )

    # Accessory indicators that identify the actual product being sold.
    accessory_start = (
        r"^\s*(charger|charging cable|cargador|cable|adaptador|adapter|"
        r"funda|case|sleeve|dock|docking station|usb hub|hub usb|"
        r"soporte|stand|stylus|pencil|screen protector|protector de pantalla|"
        r"keyboard|teclado|mouse|rat[oó]n|auriculares|headphones)\b"
    )

    if re.search(accessory_start, title_lower):
        return "accessory"

    # Put complete devices before individual components.
    # This prevents a MacBook containing "RAM" or "SSD" from
    # being incorrectly classified as RAM or storage.
    rules = [
        (
            "laptop",
            r"\b(macbook|laptop|notebook|portátil|portatil|thinkpad|"
            r"vivobook|zenbook|ideapad|pavilion|elitebook|probook|"
            r"xps|inspiron|latitude|rog\s+strix|zephyrus|tuf\s+gaming|"
            r"aspire|swift|predator|legion|yoga|surface\s+laptop|"
            r"matebook|galaxy\s+book|lg\s+gram|katana|stealth|"
            r"raider|omen|victus|chromebook)\b"
        ),
        (
            "phone",
            r"\b(iphone|smartphone|galaxy\s+s\d+|galaxy\s+z|"
            r"pixel\s+\d+|móvil|movil|teléfono|telefono|"
            r"redmi|poco\s?[a-z]?\d|oneplus|moto\s+g)\b"
        ),
        ("tablet", r"\b(ipad|galaxy tab|tablet)\b"),
        (
            "console",
            r"\b(ps5|playstation|xbox|nintendo switch|steam deck|consola)\b"
        ),
        ("gpu", r"\b(rtx|gtx|geforce|radeon|rx\s?\d{3,4}|graphics card|tarjeta gráfica|gpu)\b"),
        ("cpu", r"\b(ryzen\s?\d|intel\s+core|core\s+i[3579]|core\s+ultra|xeon|procesador|cpu)\b"),
        ("ram", r"\b(ddr[345]\s?ram|memory kit|módulo de memoria|modulo de memoria)\b"),
        ("storage", r"\b(nvme|ssd|hard drive|hdd|disco duro)\b"),
        ("motherboard", r"\b(motherboard|placa base|placa madre)\b"),
        ("monitor", r"\b(monitor|ultrawide|displayport monitor)\b"),
        ("desktop", r"\b(desktop pc|gaming pc|pc gaming|ordenador de sobremesa|torre pc)\b"),
    ]

    for category, pattern in rules:
        if re.search(pattern, title_lower, re.I):
            return category

    # Description-only accessory listings should not become laptops
    # merely because they mention a MacBook or another device.
    if re.search(accessory_pattern, text, re.I):
        return "accessory"

    return "other"



def normalize_listing(title, description="", price=None, url=None, source=None):
    title = title or ""
    description = description or ""
    text = f"{title}\n{description}"
    result = {
        "title": title, "category": "other", "brand": None, "model": None,
        "processor": None, "gpu": None, "ram_gb": None, "storage_gb": None,
        "storage_type": None, "screen_inches": None, "edition": None,
        "price": price, "url": url, "source": source,
        "spec_sources": {}, "conflicts": {}, "spec_confidence": {},
    }

    result["category"] = detect_category(title, description)


    brands = [
        ("Apple", r"\b(apple|macbook|iphone|ipad)\b"),
        ("Samsung", r"\b(samsung|galaxy)\b"),
        ("Google", r"\b(google|pixel)\b"),
        ("Sony", r"\b(sony|playstation|ps5)\b"),
        ("Microsoft", r"\b(microsoft|xbox|surface)\b"),
        ("Nintendo", r"\b(nintendo)\b"),
        ("NVIDIA", r"\b(nvidia|geforce|rtx|gtx)\b"),
        ("AMD", r"\b(amd|ryzen|radeon)\b"),
        ("Intel", r"\b(intel|core i[3579]|core ultra)\b"),
        ("ASUS", r"\b(asus|rog strix|zenbook|vivobook)\b"),
        ("Lenovo", r"\b(lenovo|thinkpad|ideapad|legion)\b"),
        ("HP", r"\b(hp pavilion|hewlett-packard|hp elitebook|hp omen)\b"),
        ("Dell", r"\b(dell|alienware|latitude|inspiron|xps)\b"),
        ("Acer", r"\b(acer|predator helios|aspire)\b"),
        ("MSI", r"\b(msi|micro-star)\b"),
        ("Corsair", r"\b(corsair)\b"),
        ("Kingston", r"\b(kingston)\b"),
        ("Western Digital", r"\b(western digital|wd black|wd blue)\b"),
        ("Seagate", r"\b(seagate)\b"),
        ("Gigabyte", r"\b(gigabyte|aorus)\b"),
        ("ASRock", r"\b(asrock)\b"),
        ("Crucial", r"\b(crucial)\b"),
        ("Huawei", r"\b(huawei|matebook)\b"),
        ("LG", r"\b(lg\s+gram|ultrawide|lg\b)\b"),
        ("Xiaomi", r"\b(xiaomi|redmi|poco)\b"),
        ("OnePlus", r"\b(oneplus)\b"),
        ("Motorola", r"\b(motorola|moto\s+g)\b"),
        ("Logitech", r"\b(logitech)\b"),
        ("Razer", r"\b(razer)\b"),
        ("Fractal Design", r"\b(fractal design)\b"),
        ("Thermaltake", r"\b(thermaltake)\b"),
        ("NZXT", r"\b(nzxt)\b"),
        ("Noctua", r"\b(noctua)\b"),
    ]

    for brand, pattern in brands:
        if re.search(pattern, text, re.I):
            result["brand"] = brand
            break


    model_patterns = [
        # Apple
        r"\b(MacBook\s+(?:Pro|Air)\s+(?:\d{2}\s+)?M[1-9](?:\s+(?:Pro|Max|Ultra))?(?:\s+\d{2})?)\b",
        r"\b(MacBook\s+(?:Pro|Air)\s+\d{2})\b",
        r"\b(MacBook\s+(?:Pro|Air))\b",

        r"\b(iPhone\s+\d+(?:\s?(?:Pro\s?Max|Pro|Plus|Max|Mini))?)\b",
        r"\b(iPad\s+(?:Pro|Air|Mini)?\s*\d*)\b",

        r"\b(iPhone\s+\d+(?:\s?(?:Pro\s?Max|Pro|Plus|Max|Mini))?)\b",

        r"\b(iPad\s+(?:Pro|Air|Mini)?\s*\d*)\b",

        # Samsung and Google
        r"\b(Galaxy\s+S\d+(?:\s?(?:Ultra|Plus|FE))?)\b",
        r"\b(Galaxy\s+Z\s?(?:Fold|Flip)\s?\d*)\b",
        r"\b(Galaxy\s+Tab\s+S\d+(?:\s?(?:Ultra|FE))?)\b",
        r"\b(Pixel\s+\d+(?:\s?(?:Pro|XL|a))?)\b",

        # Consoles
        r"\b(PlayStation\s?5(?:\s?(?:Slim|Pro))?)\b",
        r"\b(PS5(?:\s?(?:Slim|Pro))?)\b",
        r"\b(Xbox\s+(?:Series\s+[XS]|One))\b",
        r"\b(Steam\s+Deck(?:\s+OLED)?)\b",
        r"\b(Nintendo\s+Switch\s+2)\b",
        r"\b(Nintendo\s+Switch(?:\s+OLED)?)\b",

        # Graphics cards
        r"\b(RTX\s?\d{4}(?:\s?Ti(?:\s?SUPER)?|\s?SUPER)?)\b",
        r"\b(GTX\s?\d{4}(?:\s?Ti)?)\b",
        r"\b(RX\s?\d{4}(?:\s?XT|\s?XTX)?)\b",

        # Processors
        r"\b(Ryzen\s+[3579]\s+\d{4,5}[A-Z0-9]*)\b",
        r"\b(Core\s?Ultra\s?[3579]\s?\d{3}[A-Z]{0,2})\b",
        r"\b(Core\s?i[3579]-?\d{4,5}[A-Z]{0,2})\b",

        # More phones
        r"\b(iPhone\s+SE(?:\s+\d{4})?)\b",
        r"\b(Redmi\s+Note\s+\d+(?:\s+Pro)?)\b",
        r"\b(POCO\s+[A-Z]\d(?:\s+Pro)?)\b",
        r"\b(OnePlus\s+\d+[T]?)\b",
        r"\b(Moto\s+G\d+)\b",

        # Laptops
        r"\b(MateBook\s+[XD]\s*\d{0,2})\b",
        r"\b(LG\s+gram\s+\d{2})\b",
        r"\b(Galaxy\s+Book\d?(?:\s+Pro)?)\b",
        r"\b(ThinkPad\s+[A-Z]\d{1,2})\b",
        r"\b(ROG\s+Strix\s+[A-Z]\d{1,2})\b",
        r"\b(ROG\s+Zephyrus\s+[A-Z]\d{1,2})\b",
        r"\b(HP\s+EliteBook\s+\d{3,4})\b",
        r"\b(XPS\s+\d{2}(?:\s+\d{2})?)\b",
    ]

    result["model"] = (
        first_match(model_patterns, title)
        or first_match(model_patterns, description)
    )

    if result["model"]:
        result["model"] = re.sub(
            r"\s+", " ", result["model"]
        ).strip()

    # Canonical MacBook model: family + size ONLY. The chip is kept in
    # the processor field, never in the model string, so "MacBook Air M4"
    # and "MacBook Air" (with processor M4) share one model and pool
    # correctly under the estimator's exact-match rules. Configuration
    # safety is unchanged: the estimator still requires processor, RAM,
    # storage and the laptop year/generation rules to agree.
    if result["model"] and result["model"].startswith("MacBook"):
        match = re.match(
            r"^(MacBook\s+(?:Pro|Air))(?:\s+(\d{2}))?",
            result["model"]
        )
        if match:
            size = match.group(2)
            if size is None:
                # Chip-first titles ("MacBook Air M5 13", "Pro M1 13\u201d")
                # put the size after the chip. Recover a plausible laptop
                # size (11-17) from the title, never a year (preceded by
                # a digit), never RAM/storage (followed by GB).
                size_match = re.search(
                    r"(?<![\d.])(1[1-7])(?![\d.])(?!\s?GB)",
                    title,
                    re.IGNORECASE,
                )
                if size_match:
                    size = size_match.group(1)
            result["model"] = (
                f"{match.group(1)} {size}" if size else match.group(1)
            )

    # Laptop fallback: use the most specific model found
    if not result["model"] and result["category"] == "laptop":
        result["model"] = first_match([
            r"\b(ThinkPad\s+[A-Z]\d{1,2})\b",
            r"\b(ROG\s+Strix\s+[A-Z]\d{1,2})\b",
            r"\b(ROG\s+Zephyrus\s+[A-Z]\d{1,2})\b",
            r"\b(HP\s+EliteBook\s+\d{3,4})\b",
            r"\b(XPS\s+\d{2}(?:\s+\d{2})?)\b",
            r"\b(MacBook\s+(?:Pro|Air))\b",
        ], title) or first_match([
            r"\b(ThinkPad\s+[A-Z]\d{1,2})\b",
            r"\b(ROG\s+Strix\s+[A-Z]\d{1,2})\b",
            r"\b(ROG\s+Zephyrus\s+[A-Z]\d{1,2})\b",
            r"\b(HP\s+EliteBook\s+\d{3,4})\b",
            r"\b(XPS\s+\d{2}(?:\s+\d{2})?)\b",
        ], description)

    extractors = {
        "processor": extract_processor, "gpu": extract_gpu, "ram_gb": extract_ram,
        "storage_gb": extract_storage, "screen_inches": extract_screen, "edition": extract_edition,
    }
    # Confidence per extracted spec: title evidence is strongest,
    # description-only is weaker, conflicts drop confidence sharply.
    SOURCE_CONFIDENCE = {"title": 0.90, "description": 0.75}
    for field, extractor in extractors.items():
        value, source_name, conflict = extract_from_both(title, description, extractor, field)
        result[field] = value
        if source_name:
            result["spec_sources"][field] = source_name
            confidence = SOURCE_CONFIDENCE[source_name]
            if conflict:
                confidence = 0.40
            result["spec_confidence"][field] = confidence
        if conflict:
            result["conflicts"][field] = conflict

    if re.search(r"\b(?:SSD|NVME)\b", text, re.I):
        result["storage_type"] = "SSD"
    elif re.search(r"\bHDD\b", text, re.I):
        result["storage_type"] = "HDD"

    # Bare GB is only interpreted as phone/tablet storage when it appears in the title.
    if result["category"] in ("phone", "tablet") and result["storage_gb"] is None:
        value = first_match([r"\b(\d{2,5})\s?GB\b"], title)
        if value:
            result["storage_gb"] = int(value)
            result["spec_sources"]["storage_gb"] = "title"
    return result


def run_tests():
    cases = [
        ("MacBook Pro M4", "16 GB de memoria RAM. SSD de 512 GB. Chip Apple M4 Pro.", 16, 512, "M4 Pro"),
        ("MacBook Air", "8GB RAM, 256GB SSD, Intel i5", 8, 256, "Core i5"),
        ("MacBook Pro 16 24GB/512GB", "En perfecto estado", 24, 512, None),
        ("MacBook Pro M4", "32GB RAM y 1TB SSD", 32, 1024, "M4"),
    ]
    for title, desc, ram, storage, processor in cases:
        result = normalize_listing(title, desc)
        passed = (result["ram_gb"], result["storage_gb"], result["processor"]) == (ram, storage, processor)
        print(("PASS" if passed else "CHECK"), title, result["ram_gb"], result["storage_gb"], result["processor"], result["spec_sources"], result["conflicts"])

def test_categories():
    samples = [
        ("NVIDIA RTX 4070 Ti Super", "gpu"),
        ("AMD Ryzen 7 7800X3D", "cpu"),
        ("Corsair DDR5 RAM 32GB", "ram"),
        ("Samsung 990 PRO NVMe SSD", "storage"),
        ("ASUS ROG Strix laptop", "laptop"),
        ("iPhone 16 Pro", "phone"),
        ("Samsung Galaxy Tab S9", "tablet"),
        ("PlayStation 5 Slim", "console"),
        ("LG UltraWide Monitor", "monitor"),
        ("ASUS motherboard", "motherboard"),
        ("Apple USB-C charger", "accessory"),
        ("Dell XPS 15", "laptop"),
        ("HP EliteBook 840", "laptop"),
        ("ASUS ROG Strix G16", "laptop"),
        ("Acer Aspire 5", "laptop"),
        ("Lenovo Legion 5", "laptop"),
        ("Cargador Apple MagSafe 1 para MacBook", "accessory"),
        ("Cargador Apple MacBook USB-C", "accessory"),
        ("MacBook Pro M4 16GB", "laptop"),
        ("MacBook Pro 2019 13 i5 8GB RAM 128GB SSD", "laptop"),
    ]

    for title, expected in samples:
        actual = detect_category(title)
        status = "PASS" if actual == expected else "FAIL"
        print(status, title, "=>", actual, "| expected:", expected)

    
    ram_storage_tests = [
        ("MacBook Air 13 M5 512GB 16GB NUEVO", 16, 512),
        ("MacBook Pro 2018 i7 16GB", 16, None),
        ("MacBook Pro 16GB RAM 512GB SSD", 16, 512),
        ("MacBook Pro 24GB/512GB", 24, 512),
        ("MacBook Pro 1TB SSD", None, 1024),
        ("MacBook Air 256GB", None, 256),
    ]

    for text, expected_ram, expected_storage in ram_storage_tests:
        ram = extract_ram(text)
        storage = extract_storage(text)

        assert ram == expected_ram, (
            f"RAM test failed: {text}: expected {expected_ram}, got {ram}"
        )

        assert storage == expected_storage, (
            f"Storage test failed: {text}: "
            f"expected {expected_storage}, got {storage}"
        )

    print("RAM/storage extraction tests passed:", len(ram_storage_tests))

    screen_tests = [
    ('MacBook Air 13.6"', 13.6),
    ('MacBook Pro 14"', 14.0),
    ('MacBook Pro 16 pulgadas', 16.0),
    ('Laptop 15.6-inch', 15.6),
    ('MacBook Air 2019', None),
    ('MacBook Air 512GB', None),
    ]

    for text, expected in screen_tests:
        result = extract_screen(text)
        assert result == expected, (
            f"Screen test failed: {text} "
            f"Expected {expected}, got {result}"
        )

    print("Screen extraction tests passed:", len(screen_tests))



def test_brands_and_models():
    samples = [
        ("NVIDIA RTX 4070 Ti Super", "NVIDIA", "RTX 4070 Ti Super"),
        ("AMD Ryzen 7 7800X3D", "AMD", "Ryzen 7 7800X3D"),
        ("Samsung Galaxy S24 Ultra", "Samsung", "Galaxy S24 Ultra"),
        ("Google Pixel 9 Pro", "Google", "Pixel 9 Pro"),
        ("Lenovo ThinkPad T14", "Lenovo", "ThinkPad T14"),
        ("ASUS ROG Strix G16", "ASUS", "ROG Strix G16"),
        ("XPS 15", "Dell", "XPS 15"),
        ("PlayStation 5 Slim", "Sony", "PlayStation 5 Slim"),
        # Model is family + size; the chip lives in processor.
        ("MacBook Pro M4", "Apple", "MacBook Pro"),
        ("MacBook Air 15 M5", "Apple", "MacBook Air 15"),
        ("MacBook Pro 16 M5 Pro", "Apple", "MacBook Pro 16"),
        ("MacBook Pro M5 Pro 16", "Apple", "MacBook Pro 16"),
        ("MacBook Pro M1 13", "Apple", "MacBook Pro 13"),
    ]

    # Real titles from the working database: identical products must
    # produce ONE model string, with the chip only in processor.
    real_cases = [
        ("MacBook Air 13 (M5)", "MacBook Air 13", "M5"),
        ("MacBook Air 13\u201d M5 16GB 512GB Precintado", "MacBook Air 13", "M5"),
        ("MacBook Air M5 13 16GB/512GB Plata", "MacBook Air 13", "M5"),
        ("MacBook Air M4", "MacBook Air", "M4"),
        ("Apple MacBook Pro 16 M5 Pro 48GB RAM 2TB", "MacBook Pro 16", "M5 Pro"),
        ("MacBook Pro M1 13\u201d 8GB 256GB", "MacBook Pro 13", "M1"),
        ("Apple MacBook Air 13 Chip m2", "MacBook Air 13", "M2"),
    ]
    for title, expected_model, expected_processor in real_cases:
        result = normalize_listing(title)
        passed = (
            result["model"] == expected_model
            and result["processor"] == expected_processor
        )
        print(
            "PASS" if passed else "CHECK",
            title[:45],
            "=> model:", result["model"],
            "| processor:", result["processor"],
        )

    for title, expected_brand, expected_model in samples:
        result = normalize_listing(title)

        passed = (
            result["brand"] == expected_brand
            and result["model"] == expected_model
        )

        print(
            "PASS" if passed else "CHECK",
            title,
            "=>",
            result["brand"],
            "|",
            result["model"],
        )

if __name__ == "__main__":
    run_tests()
    test_categories()
    test_brands_and_models()
