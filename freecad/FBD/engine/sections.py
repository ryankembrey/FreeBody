# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileNotice: Part of the FBD addon.

"""Standard structural section library and material properties.

Sections are stored as (area_mm2, second_moment_mm4, depth_mm) tuples.
All section values are from EN 10034 / Arcelor-Mittal section tables.

Materials carry density (kg/m3), yield stress fy (N/mm2), and Young's
modulus E (N/mm2).  fy is the nominal value for t <= 16 mm per EN 10025.

Nothing here imports FreeCAD or Qt: this module is importable and testable
on its own, the same as the rest of the engine.
"""

# ---------------------------------------------------------------------------
# Section tables  {name: (A mm2, Iy mm4, h mm)}
# h is the total section depth (outer flange to outer flange).
# ---------------------------------------------------------------------------

SECTIONS: dict = {
    # ---- IPE (European I-beams) -------------------------------------------
    "IPE 100":  (  1030,      171_000,  100),
    "IPE 120":  (  1320,      318_000,  120),
    "IPE 140":  (  1640,      541_000,  140),
    "IPE 160":  (  2010,      869_000,  160),
    "IPE 180":  (  2390,    1_317_000,  180),
    "IPE 200":  (  2850,    1_943_000,  200),
    "IPE 220":  (  3340,    2_772_000,  220),
    "IPE 240":  (  3910,    3_892_000,  240),
    "IPE 270":  (  4590,    5_790_000,  270),
    "IPE 300":  (  5380,    8_356_000,  300),
    "IPE 330":  (  6260,   11_770_000,  330),
    "IPE 360":  (  7270,   16_270_000,  360),
    "IPE 400":  (  8450,   23_130_000,  400),
    "IPE 450":  (  9880,   33_740_000,  450),
    "IPE 500":  ( 11550,   48_200_000,  500),
    "IPE 550":  ( 13440,   67_120_000,  550),
    "IPE 600":  ( 15600,   92_080_000,  600),
    # ---- HEA (European wide-flange, series A) --------------------------------
    # h here is the actual section height, which is less than the designation.
    "HEA 100":  (  2120,    3_490_000,   96),
    "HEA 120":  (  2530,    6_060_000,  114),
    "HEA 140":  (  3140,   10_330_000,  133),
    "HEA 160":  (  3880,   16_670_000,  152),
    "HEA 180":  (  4530,   25_100_000,  171),
    "HEA 200":  (  5380,   36_920_000,  190),
    "HEA 220":  (  6430,   54_100_000,  210),
    "HEA 240":  (  7680,   77_600_000,  230),
    "HEA 260":  (  8680,  104_900_000,  250),
    "HEA 280":  (  9730,  136_200_000,  270),
    "HEA 300":  ( 11250,  182_600_000,  290),
    "HEA 320":  ( 12400,  229_700_000,  310),
    "HEA 360":  ( 14280,  337_500_000,  350),
    "HEA 400":  ( 15930,  450_700_000,  390),
}

# ---------------------------------------------------------------------------
# Material table  {name: {density kg/m3, fy N/mm2, E N/mm2}}
# ---------------------------------------------------------------------------

MATERIALS: dict = {
    "Steel S235":  {"density": 7850, "fy": 235, "E": 210_000},
    "Steel S275":  {"density": 7850, "fy": 275, "E": 210_000},
    "Steel S355":  {"density": 7850, "fy": 355, "E": 210_000},
    "Aluminium":   {"density": 2700, "fy": 270, "E":  70_000},
    "Timber C24":  {"density":  500, "fy":  24, "E":  11_000},
}

DEFAULT_MATERIAL = "Steel S275"


# ---------------------------------------------------------------------------
# Derived quantities
# ---------------------------------------------------------------------------

def section_ea_ei(section_name: str, material: str = DEFAULT_MATERIAL):
    """(EA N, EI N.mm2) for the given section and material.

    Returns (None, None) when section_name is not in SECTIONS.
    """
    props = SECTIONS.get(section_name)
    if props is None:
        return None, None
    A, I, _h = props
    mat = MATERIALS.get(material, MATERIALS[DEFAULT_MATERIAL])
    E = mat["E"]
    return float(E * A), float(E * I)


def section_self_weight(section_name: str, material: str = DEFAULT_MATERIAL) -> float:
    """Self-weight in N/mm (positive, acts downward when applied as member.g).

    Derivation (keeping g in SI m/s²):
        mass/length [kg/mm] = rho [kg/m3] * A [mm2] * 1e-6 [m2/mm2] / 1000 [mm/m]
                            = rho * A * 1e-9  kg/mm
        weight/length [N/mm] = mass/length * g [m/s2]
                             = rho * A * 9.81 * 1e-9  N/mm
    """
    props = SECTIONS.get(section_name)
    if props is None:
        return 0.0
    A, _I, _h = props
    mat = MATERIALS.get(material, MATERIALS[DEFAULT_MATERIAL])
    rho = mat["density"]
    return rho * A * 9.81e-9


def utilisation(section_name: str, material: str, N: float, M: float):
    """Elastic utilisation ratio (dimensionless), or None if section unknown.

    Returns max(axial utilisation, bending utilisation) where:
        axial    = |N| / (A * fy)
        bending  = |M| / (Wel * fy),  Wel = Iy / (h/2)

    Values above 1.0 indicate overstress.  The caller is responsible for
    clamping or colouring overstressed members appropriately.
    """
    props = SECTIONS.get(section_name)
    if props is None:
        return None
    A, I, h = props
    mat = MATERIALS.get(material, MATERIALS[DEFAULT_MATERIAL])
    fy = mat["fy"]
    if fy <= 0 or A <= 0:
        return None
    axial_u = abs(N) / (A * fy)
    Z = I / (h / 2.0) if (I > 0 and h > 0) else 0.0
    bending_u = abs(M) / (Z * fy) if Z > 0 else 0.0
    return max(axial_u, bending_u)
