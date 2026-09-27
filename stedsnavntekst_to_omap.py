#!/usr/bin/env python3
"""
stedsnavntekst_to_omap.py

Extract place-name labels (the `stedsnavntekst` table) from a Kartverket
N50 PostGIS pg_dump SQL file and write them directly as text objects in
an OpenOrienteering Mapper file (.omap, XML format version 9) -- no
GeoPackage / table-import step involved, since OOM's GPKG table importer
does not support text/label fields.

Each label is styled from its `skriftkode` (text-style code) using
Kartverket's own screen-cartography specification ("Spesifikasjon for
skjermkartografi", FKB og N50-N5000 Kartdata v2.2): font weight/style,
size in mm, colour (black or the water-feature blue), and whether the
text should be shown in capitals. This is used as a starting point, not
matched exactly -- sizes are rounded and the typeface is swapped out
(the spec says Arial; this script uses Verdana instead, chosen mainly
for being open/cross-platform and easy to read at small sizes -- change
FONT_FAMILY below or override with --font to use something else).

The output is a real, georeferenced .omap file: it declares a
projected_crs (ETRS89 / UTM zone 32N, EPSG:25832, as a PROJ.4 string,
which is the CRS language Mapper's XML format expects) and a reference
point, and every label's internal ("native", 1/1000 mm) coordinates are
computed with the same formula Mapper itself uses internally
(Georeferencing::updateTransformation() in its C++ source), so the file
opens already aligned to the real world -- you can add other templates
(e.g. the .gpkg made by sql_to_gpkg.py, or background maps) and they'll
line up.

Usage
-----
    python stedsnavntekst_to_omap.py INPUT.sql OUTPUT.omap
    python stedsnavntekst_to_omap.py INPUT.sql OUTPUT.omap --scale 25000 --font "DejaVu Sans"

--scale is the map's nominal cartographic scale denominator (default
50000, i.e. 1:50 000, matching the reference scale the Kartverket spec
sizes are defined for). It only affects the internal paper-mm
bookkeeping, not where the labels end up in the real world.

Every original column from the `stedsnavntekst` row (objid,
navneobjekthovedgruppe, sprak, referansemalestokk, skriftkode, ... --
everything except the raw geometry) is kept as an OOM object tag, so
it's still there to filter, query or inspect inside Mapper (Edit menu
-> Object tags), even though the GPKG table importer can't carry it
across.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from shapely import wkb as shapely_wkb

COPY_RE = re.compile(r"^COPY\s+([\w.]+)\s*\(([^)]*)\)\s+FROM\s+stdin;$")

# PROJ.4 string for ETRS89 / UTM zone 32N (EPSG:25832) -- the CRS used
# throughout the N50 "Basisdata" dumps.
PROJ4_25832 = "+proj=utm +zone=32 +ellps=GRS80 +units=m +no_defs"

# Typeface to use instead of the spec's Arial. Verdana was picked simply
# as a widely-available, legible-at-small-sizes alternative -- swap it
# for anything else installed on your system (e.g. "DejaVu Sans",
# "Noto Sans", "Trebuchet MS") via --font.
FONT_FAMILY = "Verdana"

# --------------------------------------------------------------------------
# Kartverket text-style table (skriftkode -> style), condensed from
# "Spesifikasjon for skjermkartografi, FKB og N50-N5000 Kartdata v2.2"
# (2025-04-01), section N50_Stedsnavn_tekstplassering. Sizes are the
# spec's mm values rounded to 1 decimal; colour is "black" (#000000) or
# "blue" (#3278FF, used for water/ice features).
# Fields: (category, bold, italic, caps, size_mm, color)
# --------------------------------------------------------------------------
SKRIFTKODE_STYLE = {
    101: ("By og tettsted", True, False, True, 4.5, "black"),
    102: ("By og tettsted", True, False, True, 3.4, "black"),
    103: ("By og tettsted", True, False, False, 3.4, "black"),
    104: ("By og tettsted", True, False, False, 3.1, "black"),
    105: ("By og tettsted", True, False, False, 2.5, "black"),
    112: ("Bygd og tettbebyggelse", False, False, False, 3.6, "black"),
    113: ("Bygd og tettbebyggelse", False, False, False, 3.0, "black"),
    121: ("Gård", False, False, False, 2.8, "black"),
    122: ("Gård, bygning og severdighet", False, False, False, 2.2, "black"),
    131: ("Anlegg", False, False, False, 3.4, "black"),
    132: ("Bygning og anlegg", False, False, False, 2.9, "black"),
    133: ("Bygning og anlegg", False, False, False, 2.5, "black"),
    134: ("Bygning og anlegg", False, False, False, 2.1, "black"),
    142: ("Administrative områder", False, False, False, 2.1, "black"),
    201: ("Områdenavn", False, True, True, 4.9, "black"),
    202: ("Områdenavn", False, True, False, 4.2, "black"),
    203: ("Områdenavn", False, True, False, 3.3, "black"),
    204: ("Områdenavn", False, True, False, 2.6, "black"),
    205: ("Områdenavn", False, True, False, 2.3, "black"),
    206: ("Høydepunkt", False, True, False, 1.8, "black"),
    211: ("Områdenavn", False, True, False, 4.9, "black"),
    212: ("Områdenavn", False, True, False, 4.1, "black"),
    213: ("Områdenavn", False, True, False, 3.4, "black"),
    214: ("Områdenavn", False, True, False, 2.8, "black"),
    215: ("Områdenavn", False, True, False, 2.4, "black"),
    221: ("Havområde", False, True, True, 6.2, "blue"),
    222: ("Kyst, innsjø og vassdrag", False, True, True, 5.5, "blue"),
    223: ("Kyst, innsjø og vassdrag", False, True, True, 4.0, "blue"),
    224: ("Kyst, innsjø og vassdrag", False, True, True, 3.4, "blue"),
    225: ("Kyst, innsjø og vassdrag", False, True, False, 3.4, "blue"),
    226: ("Kyst, innsjø og vassdrag", False, True, False, 2.9, "blue"),
    227: ("Kyst, innsjø og vassdrag", False, True, False, 2.4, "blue"),
    228: ("Vannhøydetall", False, True, False, 1.8, "blue"),
    231: ("Sjø", False, True, True, 3.5, "black"),
    232: ("Sjø", False, True, False, 3.2, "black"),
    233: ("Sjø", False, True, False, 2.8, "black"),
    234: ("Sjø", False, True, False, 2.4, "black"),
    241: ("Bre", False, True, True, 5.0, "blue"),
    242: ("Bre", False, True, False, 4.3, "blue"),
    243: ("Bre", False, True, False, 3.1, "blue"),
    244: ("Bre", False, True, False, 2.6, "blue"),
    245: ("Bre", False, True, False, 2.3, "blue"),
    246: ("Brehøyde", False, True, False, 1.8, "blue"),
    307: ("Riksrøys, grensemerke", False, True, False, 1.8, "black"),
    502: ("By og tettsted, sidenavn", False, False, True, 3.2, "black"),
    503: ("By og tettsted, sidenavn", False, False, False, 3.2, "black"),
    504: ("By og tettsted, sidenavn", False, False, False, 2.6, "black"),
    505: ("By og tettsted, sidenavn", False, False, False, 2.4, "black"),
    512: ("Bygd og tettbebyggelse, sidenavn", False, False, False, 3.2, "black"),
    513: ("Bygd og tettbebyggelse, sidenavn", False, False, False, 2.6, "black"),
    521: ("Bygning og anlegg, sidenavn", False, False, False, 2.4, "black"),
    522: ("Bygning og anlegg, sidenavn", False, False, False, 2.1, "black"),
    534: ("Bygning og anlegg, sidenavn", False, False, False, 2.1, "black"),
    601: ("Områdenavn, sidenavn", False, True, True, 4.8, "black"),
    602: ("Områdenavn, sidenavn", False, True, False, 4.1, "black"),
    603: ("Områdenavn, sidenavn", False, True, False, 3.3, "black"),
    604: ("Områdenavn, sidenavn", False, True, False, 2.6, "black"),
    605: ("Områdenavn, sidenavn", False, True, False, 2.3, "black"),
    611: ("Områdenavn, sidenavn", False, True, False, 4.7, "black"),
    612: ("Områdenavn, sidenavn", False, True, False, 3.4, "black"),
    613: ("Områdenavn, sidenavn", False, True, False, 3.1, "black"),
    614: ("Områdenavn, sidenavn", False, True, False, 2.5, "black"),
    615: ("Områdenavn, sidenavn", False, True, False, 2.1, "black"),
    622: ("Kyst, innsjø og vassdrag, sidenavn", False, True, True, 5.3, "blue"),
    623: ("Kyst, innsjø og vassdrag, sidenavn", False, True, True, 3.9, "blue"),
    624: ("Kyst, innsjø og vassdrag, sidenavn", False, True, True, 3.4, "blue"),
    625: ("Kyst, innsjø og vassdrag, sidenavn", False, True, False, 3.4, "blue"),
    626: ("Kyst, innsjø og vassdrag, sidenavn", False, True, False, 2.9, "blue"),
    627: ("Kyst, innsjø og vassdrag, sidenavn", False, True, False, 2.4, "blue"),
    634: ("Sjø, sidenavn", False, True, False, 2.4, "black"),
    641: ("Bre, sidenavn", False, True, True, 4.2, "blue"),
    642: ("Bre, sidenavn", False, True, False, 3.4, "blue"),
    643: ("Bre, sidenavn", False, True, False, 2.6, "blue"),
    644: ("Bre, sidenavn", False, True, False, 2.1, "blue"),
    645: ("Bre, sidenavn", False, True, False, 1.9, "blue"),
}
# Fallback style for any skriftkode not covered above.
DEFAULT_STYLE = ("Ukjent", False, False, False, 2.5, "black")

COLOR_RGB = {
    "black": (0.0, 0.0, 0.0),
    "blue": (50 / 255, 120 / 255, 255 / 255),
}


def strip_schema(qualified_name: str) -> str:
    return qualified_name.rsplit(".", 1)[-1]


def unescape_copy_text(value: str) -> str:
    """Undo PostgreSQL COPY TEXT format backslash-escaping."""
    if "\\" not in value:
        return value
    out = []
    i, n = 0, len(value)
    simple = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            nxt = value[i + 1]
            if nxt in simple:
                out.append(simple[nxt]); i += 2; continue
            if nxt.isdigit():
                j = i + 1
                digits = ""
                while j < n and len(digits) < 3 and value[j].isdigit():
                    digits += value[j]; j += 1
                out.append(chr(int(digits, 8))); i = j; continue
            out.append(nxt); i += 2; continue
        out.append(ch); i += 1
    return "".join(out)


def extract_stedsnavntekst(sql_path: Path) -> list[dict]:
    """Stream through the dump and pull out every row of the
    `stedsnavntekst` table as a dict of {column_name: raw_field_text}."""
    rows: list[dict] = []
    cols: list[str] | None = None
    in_block = False
    with sql_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.rstrip("\n")
            if not in_block:
                m = COPY_RE.match(stripped)
                if m and strip_schema(m.group(1)) == "stedsnavntekst":
                    cols = [c.strip() for c in m.group(2).split(",")]
                    in_block = True
                continue
            if stripped == r"\.":
                break
            fields = stripped.split("\t")
            if len(fields) != len(cols):
                continue  # defensive: skip malformed lines rather than crash
            rows.append(dict(zip(cols, fields)))
    if cols is None:
        sys.exit("Table 'stedsnavntekst' was not found in the dump.")
    return rows


def xml_attr_escape(s: str) -> str:
    return xml_escape(s, {'"': "&quot;"})


def row_to_label(row: dict):
    """Return (x, y, text, code, tags) for one stedsnavntekst row, or
    None if it has no usable geometry/text. `geometri` is a 2-point
    LineString (an Esri-style annotation baseline); we use its first
    vertex as the label anchor point. `tags` carries every other column
    from the row (de-escaped), to be written as OOM object tags."""
    geom_hex = row.get("geometri")
    if not geom_hex or geom_hex == r"\N":
        return None
    geom = shapely_wkb.loads(bytes.fromhex(geom_hex))
    coords = list(geom.coords)
    if not coords:
        return None
    x, y = coords[0][0], coords[0][1]

    text = row.get("fulltekst")
    if not text or text == r"\N":
        text = row.get("streng")
    if not text or text == r"\N":
        return None
    text = unescape_copy_text(text)
    if not text.strip():
        return None

    raw_code = row.get("skriftkode")
    try:
        code = int(raw_code) if raw_code and raw_code != r"\N" else None
    except ValueError:
        code = None

    tags = {
        key: unescape_copy_text(value)
        for key, value in row.items()
        if key != "geometri" and value not in (None, r"\N")
    }

    return x, y, text, code, tags


# --------------------------------------------------------------------------
# .omap writing
# --------------------------------------------------------------------------

def build_omap(labels: list[tuple[float, float, str, int, dict]], scale_denominator: int, font_family: str) -> str:
    xs = [lab[0] for lab in labels]
    ys = [lab[1] for lab in labels]
    ref_x = sum(xs) / len(xs)
    ref_y = sum(ys) / len(ys)

    # Same formula as Georeferencing::updateTransformation() in Mapper's
    # C++ source, with grivation = 0 and combined_scale_factor = 1:
    #   native_x = round((proj_x - ref_x) * 1e6 / scale_denominator)
    #   native_y = round((ref_y - proj_y) * 1e6 / scale_denominator)
    # (native units = 1/1000 mm on paper; y is flipped because Mapper's
    # internal Y axis grows downward while northing grows upward.)
    def to_native(x: float, y: float) -> tuple[int, int]:
        nx = round((x - ref_x) * 1_000_000 / scale_denominator)
        ny = round((ref_y - y) * 1_000_000 / scale_denominator)
        return nx, ny

    max_native = max(max(abs(n) for n in to_native(x, y)) for x, y, _, _, _ in labels)
    if max_native > 900_000_000:
        print(
            f"Warning: label coordinates reach {max_native:,} native units, close to the "
            "qint32 limit (~2.1e9). Consider a larger --scale value.",
            file=sys.stderr,
        )

    # One symbol per distinct skriftkode actually used (falls back to
    # DEFAULT_STYLE for anything not in the table), so each label gets
    # its proper size/weight/colour/caps treatment.
    used_codes = sorted({code for _x, _y, _text, code, _tags in labels}, key=lambda c: (c is None, c))
    symbol_id_by_code: dict[int | None, str] = {}
    symbol_defs = []
    color_id_by_name = {"black": 0, "blue": 1}

    for i, code in enumerate(used_codes):
        category, bold, italic, caps, size_mm, color_name = SKRIFTKODE_STYLE.get(code, DEFAULT_STYLE)
        sym_id = str(i)
        symbol_id_by_code[code] = sym_id
        size_native = round(size_mm * 1000)
        attrs = f'family="{xml_escape(font_family)}" size="{size_native}"'
        if bold:
            attrs += ' bold="true"'
        if italic:
            attrs += ' italic="true"'
        name = f"{category} ({code if code is not None else 'ukjent'})"
        symbol_defs.append(
            f'<symbol type="8" id="{sym_id}" code="" name="{xml_escape(name)}">'
            f'<text_symbol icon_text="" rotatable="true">'
            f"<font {attrs}/>"
            f'<text color="{color_id_by_name[color_name]}" line_spacing="1" '
            f'paragraph_spacing="0" character_spacing="0" kerning="true"/>'
            f"</text_symbol></symbol>"
        )

    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>\n')
    parts.append('<map xmlns="http://openorienteering.org/apps/mapper/xml/v2" version="9">\n')
    parts.append("<notes></notes>\n")
    parts.append(f'<georeferencing scale="{scale_denominator}">')
    parts.append('<projected_crs id="EPSG:25832">')
    parts.append(f'<spec language="PROJ.4">{xml_escape(PROJ4_25832)}</spec>')
    parts.append(f'<ref_point x="{ref_x:.3f}" y="{ref_y:.3f}"/>')
    parts.append("</projected_crs>")
    parts.append("</georeferencing>\n")

    parts.append('<colors count="2">')
    for name, cid in color_id_by_name.items():
        r, g, b = COLOR_RGB[name]
        parts.append(
            f'<color priority="{cid}" name="{name.capitalize()}" c="0" m="0" y="0" k="0" opacity="1">'
            f'<cmyk method="rgb"/><rgb method="custom" r="{r:.4f}" g="{g:.4f}" b="{b:.4f}"/></color>'
        )
    parts.append("</colors>\n")

    parts.append(f'<symbols count="{len(symbol_defs)}" id="stedsnavntekst">')
    parts.extend(symbol_defs)
    parts.append("</symbols>\n")

    parts.append(f'<parts count="1" current="0">\n<part name="Stedsnavn"><objects count="{len(labels)}">\n')
    for x, y, text, code, tags in labels:
        nx, ny = to_native(x, y)
        _category, _bold, _italic, caps, *_ = SKRIFTKODE_STYLE.get(code, DEFAULT_STYLE)
        display_text = text.upper() if caps else text
        sym_id = symbol_id_by_code[code]
        tags_xml = "".join(
            f'<t k="{xml_attr_escape(k)}">{xml_escape(v)}</t>' for k, v in tags.items()
        )
        parts.append(
            f'<object type="4" symbol="{sym_id}" h_align="1" v_align="2">'
            f"<tags>{tags_xml}</tags>"
            f'<coords count="1">{nx} {ny};</coords>'
            f"<text>{xml_escape(display_text)}</text></object>\n"
        )
    parts.append("</objects></part>\n</parts>\n")
    parts.append("</map>\n")
    return "".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_sql", type=Path, help="Path to the PostGIS pg_dump .sql file")
    parser.add_argument("output_omap", type=Path, help="Path to write the output .omap file")
    parser.add_argument("--scale", type=int, default=50000, help="Map scale denominator, e.g. 50000 for 1:50 000 (default: 50000)")
    parser.add_argument("--font", type=str, default=FONT_FAMILY, help=f"Font family for all labels (default: {FONT_FAMILY})")
    args = parser.parse_args()

    if not args.input_sql.exists():
        sys.exit(f"Input SQL file not found: {args.input_sql}")

    print(f"Parsing {args.input_sql} for table 'stedsnavntekst' ...")
    rows = extract_stedsnavntekst(args.input_sql)
    print(f"Found {len(rows)} stedsnavntekst row(s) in the dump.")

    labels = []
    for row in rows:
        lab = row_to_label(row)
        if lab is not None:
            labels.append(lab)
    print(f"{len(labels)} usable label(s) (with geometry and text).")

    unmapped = sorted({code for _x, _y, _text, code, _tags in labels if code not in SKRIFTKODE_STYLE})
    if unmapped:
        print(f"Note: {len(unmapped)} skriftkode value(s) not in the spec table, using default style: {unmapped}", file=sys.stderr)

    if not labels:
        sys.exit("No usable labels -- nothing to write.")

    xml_text = build_omap(labels, args.scale, args.font)
    args.output_omap.write_text(xml_text, encoding="utf-8")
    print(f"Wrote {len(labels)} text objects to {args.output_omap}")


if __name__ == "__main__":
    main()

