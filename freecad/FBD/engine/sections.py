# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileNotice: Part of the FBD addon.

"""Structural section library and material properties.

Every section stores four items:  (A mm2, Iy mm4, h mm, profile_dict)
A and Iy for IPE / HEA / HEB are EN 10034 tabulated values.
All other sections are computed from geometry (no root-radius correction,
error < 5 % — acceptable for a teaching tool).

Profile dicts describe the cross-section geometry for drawing:
    'I'     h, b, tf, tw   symmetric I/H section
    'CHS'   D, t           circular hollow
    'RHS'   H, B, t        rectangular hollow  (H = tall dimension)
    'ROUND' D              solid round bar
    'RECT'  H, B           solid rectangular   (H = tall dimension)

Nothing here imports FreeCAD or Qt.
"""

import math

# ---------------------------------------------------------------------------
# Geometry helpers — build (A, I, h, profile) tuples
# ---------------------------------------------------------------------------

def _i(A, I, h, b, tf, tw):
    return (int(A), int(I), h, {"type": "I", "h": h, "b": b, "tf": tf, "tw": tw})


def _chs(D, t):
    di = D - 2 * t
    A = math.pi / 4 * (D ** 2 - di ** 2)
    I = math.pi / 64 * (D ** 4 - di ** 4)
    return (round(A), round(I), D, {"type": "CHS", "D": D, "t": t})


def _rhs(H, B, t):
    """Rectangular hollow; H is the tall dimension."""
    hi, bi = H - 2 * t, B - 2 * t
    A = H * B - hi * bi
    I = (B * H ** 3 - bi * hi ** 3) / 12
    return (round(A), round(I), H, {"type": "RHS", "H": H, "B": B, "t": t})


def _shs(b, t):
    return _rhs(b, b, t)


def _round(D):
    A = math.pi * D ** 2 / 4
    I = math.pi * D ** 4 / 64
    return (round(A), round(I), D, {"type": "ROUND", "D": D})


def _rect(H, B):
    """Solid rectangular; H is the tall dimension."""
    A = H * B
    I = B * H ** 3 / 12
    return (round(A), round(I), H, {"type": "RECT", "H": H, "B": B})


# ---------------------------------------------------------------------------
# Section catalogue  {name: (A, I, h, profile)}
# ---------------------------------------------------------------------------

_CATALOG: dict = {

    # ---- IPE — EN 10034 tabulated A and Iy --------------------------------
    "IPE 100":  _i(  1030,      171_000,  100,  55,  5.7,  4.1),
    "IPE 120":  _i(  1320,      318_000,  120,  64,  6.3,  4.4),
    "IPE 140":  _i(  1640,      541_000,  140,  73,  6.9,  4.7),
    "IPE 160":  _i(  2010,      869_000,  160,  82,  7.4,  5.0),
    "IPE 180":  _i(  2390,    1_317_000,  180,  91,  8.0,  5.3),
    "IPE 200":  _i(  2850,    1_943_000,  200, 100,  8.5,  5.6),
    "IPE 220":  _i(  3340,    2_772_000,  220, 110,  9.2,  5.9),
    "IPE 240":  _i(  3910,    3_892_000,  240, 120,  9.8,  6.2),
    "IPE 270":  _i(  4590,    5_790_000,  270, 135, 10.2,  6.6),
    "IPE 300":  _i(  5380,    8_356_000,  300, 150, 10.7,  7.1),
    "IPE 330":  _i(  6260,   11_770_000,  330, 160, 11.5,  7.5),
    "IPE 360":  _i(  7270,   16_270_000,  360, 170, 12.7,  8.0),
    "IPE 400":  _i(  8450,   23_130_000,  400, 180, 13.5,  8.6),
    "IPE 450":  _i(  9880,   33_740_000,  450, 190, 14.6,  9.4),
    "IPE 500":  _i( 11550,   48_200_000,  500, 200, 16.0, 10.2),
    "IPE 550":  _i( 13440,   67_120_000,  550, 210, 17.2, 11.1),
    "IPE 600":  _i( 15600,   92_080_000,  600, 220, 19.0, 12.0),

    # ---- HEA — EN 10034 tabulated ----------------------------------------
    "HEA 100":  _i(  2120,    3_490_000,   96, 100,  8.0,  5.0),
    "HEA 120":  _i(  2530,    6_060_000,  114, 120,  8.0,  5.0),
    "HEA 140":  _i(  3140,   10_330_000,  133, 140,  8.5,  5.5),
    "HEA 160":  _i(  3880,   16_670_000,  152, 160,  9.0,  6.0),
    "HEA 180":  _i(  4530,   25_100_000,  171, 180,  9.5,  6.0),
    "HEA 200":  _i(  5380,   36_920_000,  190, 200, 10.0,  6.5),
    "HEA 220":  _i(  6430,   54_100_000,  210, 220, 11.0,  7.0),
    "HEA 240":  _i(  7680,   77_600_000,  230, 240, 12.0,  7.5),
    "HEA 260":  _i(  8680,  104_900_000,  250, 260, 12.5,  7.5),
    "HEA 280":  _i(  9730,  136_200_000,  270, 280, 13.0,  8.0),
    "HEA 300":  _i( 11250,  182_600_000,  290, 300, 14.0,  8.5),
    "HEA 320":  _i( 12400,  229_700_000,  310, 300, 15.5,  9.0),
    "HEA 360":  _i( 14280,  337_500_000,  350, 300, 17.5, 10.0),
    "HEA 400":  _i( 15930,  450_700_000,  390, 300, 19.0, 11.0),

    # ---- HEB — EN 10034 tabulated ----------------------------------------
    "HEB 100":  _i(  2600,    4_500_000,  100, 100, 10.0,  6.0),
    "HEB 120":  _i(  3400,    8_640_000,  120, 120, 11.0,  6.5),
    "HEB 140":  _i(  4300,   15_100_000,  140, 140, 12.0,  7.0),
    "HEB 160":  _i(  5430,   24_900_000,  160, 160, 13.0,  8.0),
    "HEB 180":  _i(  6525,   38_300_000,  180, 180, 14.0,  8.5),
    "HEB 200":  _i(  7810,   56_960_000,  200, 200, 15.0,  9.0),
    "HEB 220":  _i(  9104,   80_910_000,  220, 220, 16.0,  9.5),
    "HEB 240":  _i( 10600,  112_600_000,  240, 240, 17.0, 10.0),
    "HEB 260":  _i( 11840,  149_200_000,  260, 260, 17.5, 10.0),
    "HEB 280":  _i( 13140,  196_000_000,  280, 280, 18.0, 10.5),
    "HEB 300":  _i( 14910,  251_700_000,  300, 300, 19.0, 11.0),

    # ---- Circular hollow (CHS) — computed --------------------------------
    "CHS 48.3\u00d73.2":   _chs( 48.3,  3.2),
    "CHS 60.3\u00d73.6":   _chs( 60.3,  3.6),
    "CHS 76.1\u00d74.0":   _chs( 76.1,  4.0),
    "CHS 88.9\u00d74.0":   _chs( 88.9,  4.0),
    "CHS 88.9\u00d75.0":   _chs( 88.9,  5.0),
    "CHS 101.6\u00d74.0":  _chs(101.6,  4.0),
    "CHS 114.3\u00d75.0":  _chs(114.3,  5.0),
    "CHS 139.7\u00d75.0":  _chs(139.7,  5.0),
    "CHS 168.3\u00d76.3":  _chs(168.3,  6.3),
    "CHS 193.7\u00d76.3":  _chs(193.7,  6.3),
    "CHS 219.1\u00d78.0":  _chs(219.1,  8.0),
    "CHS 244.5\u00d78.0":  _chs(244.5,  8.0),
    "CHS 273.0\u00d78.0":  _chs(273.0,  8.0),
    "CHS 323.9\u00d710.0": _chs(323.9, 10.0),
    "CHS 406.4\u00d712.5": _chs(406.4, 12.5),

    # ---- Square hollow (SHS) — computed ----------------------------------
    "SHS 40\u00d740\u00d73":    _shs( 40,  3),
    "SHS 50\u00d750\u00d74":    _shs( 50,  4),
    "SHS 60\u00d760\u00d74":    _shs( 60,  4),
    "SHS 80\u00d780\u00d75":    _shs( 80,  5),
    "SHS 100\u00d7100\u00d75":  _shs(100,  5),
    "SHS 100\u00d7100\u00d76":  _shs(100,  6),
    "SHS 120\u00d7120\u00d76":  _shs(120,  6),
    "SHS 150\u00d7150\u00d76":  _shs(150,  6),
    "SHS 150\u00d7150\u00d78":  _shs(150,  8),
    "SHS 200\u00d7200\u00d78":  _shs(200,  8),
    "SHS 200\u00d7200\u00d710": _shs(200, 10),
    "SHS 250\u00d7250\u00d710": _shs(250, 10),
    "SHS 300\u00d7300\u00d710": _shs(300, 10),
    "SHS 300\u00d7300\u00d712": _shs(300, 12),

    # ---- Rectangular hollow (RHS H\xd7B\xd7t, H \u2265 B) — computed ----
    "RHS 60\u00d740\u00d74":    _rhs( 60,  40,  4),
    "RHS 80\u00d740\u00d74":    _rhs( 80,  40,  4),
    "RHS 80\u00d760\u00d75":    _rhs( 80,  60,  5),
    "RHS 100\u00d750\u00d75":   _rhs(100,  50,  5),
    "RHS 100\u00d760\u00d75":   _rhs(100,  60,  5),
    "RHS 120\u00d760\u00d75":   _rhs(120,  60,  5),
    "RHS 120\u00d780\u00d76":   _rhs(120,  80,  6),
    "RHS 150\u00d7100\u00d76":  _rhs(150, 100,  6),
    "RHS 200\u00d7100\u00d78":  _rhs(200, 100,  8),
    "RHS 200\u00d7150\u00d78":  _rhs(200, 150,  8),
    "RHS 250\u00d7150\u00d78":  _rhs(250, 150,  8),
    "RHS 300\u00d7150\u00d710": _rhs(300, 150, 10),
    "RHS 300\u00d7200\u00d710": _rhs(300, 200, 10),

    # ---- Solid round — computed ------------------------------------------
    "Round \u00d710":   _round( 10),
    "Round \u00d716":   _round( 16),
    "Round \u00d720":   _round( 20),
    "Round \u00d725":   _round( 25),
    "Round \u00d730":   _round( 30),
    "Round \u00d740":   _round( 40),
    "Round \u00d750":   _round( 50),
    "Round \u00d760":   _round( 60),
    "Round \u00d775":   _round( 75),
    "Round \u00d7100":  _round(100),

    # ---- Solid rectangular — computed ------------------------------------
    "Rect 50\u00d750":   _rect( 50,  50),
    "Rect 100\u00d750":  _rect(100,  50),
    "Rect 150\u00d750":  _rect(150,  50),
    "Rect 100\u00d7100": _rect(100, 100),
    "Rect 150\u00d7100": _rect(150, 100),
    "Rect 200\u00d7100": _rect(200, 100),
    "Rect 150\u00d7150": _rect(150, 150),
    "Rect 200\u00d7150": _rect(200, 150),
    "Rect 200\u00d7200": _rect(200, 200),
    "Rect 300\u00d7200": _rect(300, 200),
}

# ---------------------------------------------------------------------------
# Public dicts derived from the catalogue
# ---------------------------------------------------------------------------

# Structural properties  {name: (A mm2, I mm4, h mm)}
SECTIONS: dict = {name: (A, I, h) for name, (A, I, h, _p) in _CATALOG.items()}

# Profile geometry for cross-section drawing  {name: profile_dict}
PROFILES: dict = {name: p for name, (*_, p) in _CATALOG.items()}

# Type categories for the two-level section picker (order = UI display order)
SECTIONS_BY_TYPE: dict = {
    "IPE":                [n for n in _CATALOG if n.startswith("IPE")],
    "HEA":                [n for n in _CATALOG if n.startswith("HEA")],
    "HEB":                [n for n in _CATALOG if n.startswith("HEB")],
    "Circular hollow":    [n for n in _CATALOG if n.startswith("CHS")],
    "Square hollow":      [n for n in _CATALOG if n.startswith("SHS")],
    "Rectangular hollow": [n for n in _CATALOG if n.startswith("RHS")],
    "Solid round":        [n for n in _CATALOG if n.startswith("Round")],
    "Solid rectangular":  [n for n in _CATALOG if n.startswith("Rect")],
}

SECTION_TYPES: list = list(SECTIONS_BY_TYPE.keys())

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
    """(EA N, EI N.mm2). Returns (None, None) for unknown sections."""
    props = SECTIONS.get(section_name)
    if props is None:
        return None, None
    A, I, _h = props
    mat = MATERIALS.get(material, MATERIALS[DEFAULT_MATERIAL])
    E = mat["E"]
    return float(E * A), float(E * I)


def section_self_weight(section_name: str, material: str = DEFAULT_MATERIAL) -> float:
    """Self-weight in N/mm (positive = downward, use as member.g).

    Formula (g in m/s2 to keep the exponent consistent):
        w [N/mm] = rho [kg/m3] * A [mm2] * 1e-6 [m2/mm2] * g [m/s2] / 1000 [mm/m]
                 = rho * A * 9.81e-9
    """
    props = SECTIONS.get(section_name)
    if props is None:
        return 0.0
    A, _I, _h = props
    mat = MATERIALS.get(material, MATERIALS[DEFAULT_MATERIAL])
    return mat["density"] * A * 9.81e-9


def utilisation(section_name: str, material: str, N: float, M: float):
    """Elastic utilisation ratio, or None if section unknown.

    Returns max(|N|/(A*fy),  |M|/(Wel*fy))  where Wel = I/(h/2).
    Values above 1.0 indicate overstress.
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
