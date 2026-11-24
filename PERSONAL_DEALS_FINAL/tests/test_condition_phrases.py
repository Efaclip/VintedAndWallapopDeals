"""Regression: condition detection semantics on real-listing patterns.

Rules under test:
- explicit used markers beat quality adjectives, whatever the order
- lone quality adjectives establish USED, never like_new
- "a estrenar" alone is ambiguous and stays unknown
- sealed/new evidence is preserved (precintado + nuevo = new)
- a caveat or defect hint blocks promotion into a clean pool
- for_parts always wins
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from condition_detector import detect_condition

CASES = [
    # real row 17: "de segunda mano" must beat "perfecto estado"
    ("MacBook Air M4 16GB/256GB",
     "de segunda mano. Bateria al 100% y en perfecto estado",
     "used"),
    # real row 107: sealed unit must stay new
    ("MacBook Air 13\" M5 16GB 512GB Precintado",
     "PRECINTADO TOTALMENTE NUEVO A ESTRENAR - SIN ABRIR, sin estrenar",
     "new"),
    # real row 117: defect hint blocks promotion
    ("MacBook Pro Apple Plateado",
     "Esta en buen estado de conservacion, pero al encender aparece "
     "lo que se ve en las fotos",
     "unknown"),
    # lone quality adjectives: used, not like_new
    ("Portatil HP", "Se vende en perfecto estado, sin marcas", "used"),
    ("iPhone 13", "Excelente estado, siempre con funda", "used"),
    # "a estrenar" alone is ambiguous: unknown
    ("iPad", "A estrenar, regalo que no uso", "unknown"),
    # unambiguous used phrases
    ("MacBook Air", "Buen estado general, marcas de uso", "used"),
    ("Lenovo", "Muy buen estado, funciona perfectamente", "used"),
    ("Dell XPS", "Funciona perfectamente, bateria al 90%", "used"),
    # explicit like_new language still works
    ("ThinkPad", "Como nuevo, apenas usado", "used"),  # "usado" beats "como nuevo"
    ("iPad Pro", "Impecable, como nuevo", "like_new"),
    # negatives
    ("HP Pavilion", "Precio negociable, envio incluido", "unknown"),
    ("MacBook Pro", "No funciona, en perfecto estado estetico", "for_parts"),
    ("iPhone 14 nuevo", "Precintado", "new"),
    ("ThinkPad", "Se vende para piezas, en buen estado exterior", "for_parts"),
]

# V5-review cases: boilerplate, negated defects, despiece/roto.
CASES += [
    ("MacBook Air 13\" M5 512Gb 16Gb NUEVO PRECINTADO",
     "¡Gran oportunidad! MacBook Air 13\" M5 NUEVO PRECINTADO de segunda mano por 1.250 EUR en Madrid. Nuevo, sin abrir, precinto intacto.",
     "new"),
    ("MacBook Neo 512 GB NUEVO PRECINTADO",
     "¡Gran oportunidad! MacBook Neo NUEVO PRECINTADO de segunda mano por 775 EUR en Madrid. Completamente nuevo, sin estrenar.",
     "new"),
    ("MacBook Air M4 16GB/256GB",
     "¡Gran oportunidad! MacBook Air M4 16GB/256GB de segunda mano por 800 EUR en Madrid. Bateria al 100% y en perfecto estado.",
     "used"),
    ("MacBook Pro Apple Plateado",
     "VER FOTOS. Esta en buen estado de conservacion, pero al encender aparece lo que se ve en las fotos.",
     "unknown"),
    ("iPhone 13", "En perfecto estado, sin golpes ni rayones", "used"),
    ("MacBook", "Usado, ninguna raya, funciona perfectamente", "used"),
    ("Portatil", "Buen estado, tiene un golpe en la esquina", "unknown"),
    ("Portatil", "Funciona bien, detalle: tecla floja", "unknown"),
    ("MacBook Pro", "Vendo para despiece, pantalla rota", "for_parts"),
    ("iPad", "Pantalla rota, no enciende", "for_parts"),
    ("ThinkPad", "Portatil de segunda mano en buen estado", "used"),
]

failed = []
for title, desc, expected in CASES:
    res = detect_condition(title, desc)
    cond = res["condition"] if isinstance(res, dict) else res[0]
    ok = cond == expected
    print(("PASS" if ok else "FAIL"),
          f"{cond:<10} (expected {expected:<10}) | {(title + ' / ' + desc)[:58]}")
    if not ok:
        failed.append((title, cond, expected))

print(f"\n{len(CASES) - len(failed)}/{len(CASES)} condition-phrase checks passed")
if failed:
    sys.exit(1)
print("ALL CONDITION-PHRASE CHECKS PASSED")
