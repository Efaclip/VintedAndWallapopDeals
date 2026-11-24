
import re



CONDITION_RULES = [
    (
        "for_parts",
        [
            r"\bpara piezas\b",
            r"\bfor parts\b",
            r"\bparts only\b",
            r"\bno funciona\b",
            r"\bdoes not work\b",
            r"\bdefectuoso\b",
            r"\bfaulty\b",
            r"\bbroken\b",
            r"\baveriado\b",
            r"\bdamaged\b",
            r"\bdespiece\b",
            r"\broto\b",
            r"\brota\b",
        ],
    ),
    (
        "new",
        [
            r"\bnuevo\b",
            r"\bnueva\b",
            r"\bnew\b",
            r"\bprecintado\b",
            r"\bsealed\b",
            r"\bsin estrenar\b",
            r"\bunopened\b",
        ],
    ),
    (
        "like_new",
        [
            r"\bcomo nuevo\b",
            r"\bcomo nueva\b",
            r"\blike new\b",
            r"\bimpecable\b",
            r"\bpracticamente nuevo\b",
            r"\bprácticamente nuevo\b",
        ],
    ),
    (
        "used",
        [
            r"\busado\b",
            r"\busada\b",
            r"\bde segunda mano\b",
            r"\bsecond hand\b",
            r"\bused\b",
            r"\bfunciona correctamente\b",
            r"\bin good condition\b",
            # Mined from real listings whose condition was unknown:
            # lone quality adjectives are used, never like_new.
            r"\bbuen estado\b",
            r"\bperfecto estado\b",
            r"\bexcelente estado\b",
            r"\bfunciona perfectamente\b",
        ],
    ),
]




def detect_condition(title="", description=""):
    """
    Detect condition independently in title and description.

    Conflicting evidence is resolved conservatively:
    for_parts > used > like_new > new

    Returns the selected condition, confidence, matched phrases,
    and conflict information.
    """

    title_text = re.sub(r"\s+", " ", title or "").lower()
    description_text = re.sub(r"\s+", " ", description or "").lower()

    # Wallapop generates an SEO/meta sentence for every listing, sealed
    # or not: "¡Gran oportunidad! <title> de segunda mano por N EUR en
    # <town>." It is platform boilerplate, not seller evidence, and the
    # "de segunda mano" inside it must not classify anything. Only that
    # exact generated shape is stripped; a seller writing "de segunda
    # mano" themselves (no "por N EUR en <town>" tail) is untouched.
    boilerplate = re.compile(
        r"(?:¡\s*gran oportunidad\s*!)?[^.]*?\bde segunda mano\b"
        r"\s+por\s+[\d.,]+\s*eur\s+en\s+[^.]*\."
    )
    title_text = boilerplate.sub(" ", title_text)
    description_text = boilerplate.sub(" ", description_text)

    priority_rules = [
        ("for_parts", [
            r"\bpara piezas\b",
            r"\bfor parts\b",
            r"\bparts only\b",
            r"\bno funciona\b",
            r"\bdoes not work\b",
            r"\bdefectuoso\b",
            r"\bfaulty\b",
            r"\bbroken\b",
            r"\baveriado\b",
            r"\bdamaged\b",
            r"\bdespiece\b",
            r"\broto\b",
            r"\brota\b",
        ]),
        ("like_new", [
            r"\bcomo nuevo\b",
            r"\bcomo nueva\b",
            r"\blike new\b",
            r"\bimpecable\b",
            r"\bpracticamente nuevo\b",
            r"\bprácticamente nuevo\b",
        ]),
        ("new", [
            r"\bnuevo\b",
            r"\bnueva\b",
            r"\bnew\b",
            r"\bprecintado\b",
            r"\bsealed\b",
            r"\bsin estrenar\b",
            r"\bunopened\b",
        ]),
        ("used", [
            r"\busado\b",
            r"\busada\b",
            r"\bde segunda mano\b",
            r"\bsecond hand\b",
            r"\bused\b",
            r"\bfunciona correctamente\b",
            r"\bin good condition\b",
            # Mined from real listings whose condition was unknown.
            # Lone quality adjectives establish USED, never like_new:
            # a used item in perfect condition is still a used item, and
            # promoting it would pollute the cleaner comparable pool.
            # "a estrenar" alone is ambiguous (sealed vs unused gift) and
            # is deliberately NOT a keyword.
            r"\bbuen estado\b",
            r"\bperfecto estado\b",
            r"\bexcelente estado\b",
            r"\bfunciona perfectamente\b",
        ]),
    ]

    # Defect evidence: concrete defect nouns/phrases. Negated mentions
    # ("sin golpes", "ni una raya", "ningun fallo") are NOT defects.
    # "Ver fotos / lo que se ve en las fotos" means the seller defers
    # condition to photos: undisclosed, not clean-pool evidence.
    defect_hints = [
        r"\bfallo\b",
        r"\bfalla\b",
        r"\bdefecto\b",
        r"\bdetalle\b",
        r"\bgolpe\b",
        r"\bgolpes\b",
        r"\braya\b",
        r"\brayon\b",
        r"\brayones\b",
        r"\brayado\b",
        r"\brayada\b",
        r"\bno enciende\b",
        r"\bno carga\b",
        r"\bse apaga\b",
        r"\bver fotos\b",
        r"\blo que se ve\b",
        r"\bcomo se (?:ve|aprecia)\b",
    ]
    negation = re.compile(
        r"(?:sin|ni|no|ningun|ninguna|libre de)\s+(?:\w+\s+){0,2}$"
    )

    def is_defected(text):
        for hint in defect_hints:
            for match in re.finditer(hint, text, re.IGNORECASE):
                if hint in (r"\bver fotos\b", r"\blo que se ve\b",
                            r"\bcomo se (?:ve|aprecia)\b",
                            r"\bno enciende\b", r"\bno carga\b",
                            r"\bse apaga\b"):
                    return True  # never negated in practice
                prefix = text[:match.start()]
                if not negation.search(prefix):
                    return True
        return False

    severity = {"for_parts": 0, "used": 1, "like_new": 2, "new": 3}

    def find_condition(text):
        # Collect EVERY match in the text; the most conservative one
        # wins. "de segunda mano ... perfecto estado" is used, not
        # like_new, regardless of phrase order in the text.
        found = {}
        for condition, patterns in priority_rules:
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match and condition not in found:
                    found[condition] = match.group(0)

        if not found:
            return None, None

        if "for_parts" in found:
            return "for_parts", found["for_parts"]

        if is_defected(text):
            # Caveated or defective condition: not clean-pool evidence.
            return None, None

        condition = min(found, key=lambda c: severity[c])
        return condition, found[condition]

    title_condition, title_match = find_condition(title_text)
    description_condition, description_match = find_condition(
        description_text
    )

    conflict = (
        title_condition is not None
        and description_condition is not None
        and title_condition != description_condition
    )

    if conflict:
        # Choose the more conservative condition for valuation.
        severity = {
            "for_parts": 0,
            "used": 1,
            "like_new": 2,
            "new": 3,
        }

        if severity[title_condition] <= severity[description_condition]:
            condition = title_condition
            matched_text = title_match
        else:
            condition = description_condition
            matched_text = description_match

        confidence = 0.50

    elif title_condition and description_condition:
        condition = title_condition
        matched_text = title_match
        confidence = 0.95

    elif title_condition:
        condition = title_condition
        matched_text = title_match
        confidence = 0.90

    elif description_condition:
        condition = description_condition
        matched_text = description_match
        confidence = 0.80

    else:
        condition = "unknown"
        matched_text = None
        confidence = 0.0

    return {
        "condition": condition,
        "confidence": confidence,
        "matched_text": matched_text,
        "conflict": conflict,
        "title_condition": title_condition,
        "description_condition": description_condition,
        "title_matched_text": title_match,
        "description_matched_text": description_match,
    }

def run_tests():
    tests = [
        ("MacBook Pro nuevo precintado", "", "new"),
        ("MacBook Air como nuevo", "", "like_new"),
        ("MacBook Pro usado", "", "used"),
        ("MacBook Air para piezas", "", "for_parts"),
        ("MacBook Pro", "Está impecable", "like_new"),
        ("MacBook Air", "No funciona, para piezas", "for_parts"),
        ("MacBook Pro 2019 16GB RAM", "", "unknown"),
        ("MacBook nuevo", "No funciona, para piezas", "for_parts"),
        ("MacBook como nuevo", "Está usado", "used"),
        ("MacBook nuevo", "Está precintado", "new"),
    ]

    print("=== CONDITION DETECTOR TESTS ===\n")

    passed = 0

    for title, description, expected in tests:
        result = detect_condition(title, description)
        actual = result["condition"]

        if actual == expected:
            print(f"PASS: {title} -> {actual}")
            passed += 1
        else:
            print(
                f"CHECK: {title} -> {actual} "
                f"(expected {expected})"
            )

    print(f"\nResults: {passed}/{len(tests)} tests passed")


if __name__ == "__main__":
    run_tests()
