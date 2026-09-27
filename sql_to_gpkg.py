#!/usr/bin/env python3
"""
sql_to_gpkg.py

Convert a PostGIS ``pg_dump`` SQL file (in particular a Kartverket N50
"Basisdata" dump) into a GeoPackage (.gpkg), keeping only the tables
listed in a YAML/JSON config file.

The script never needs a running PostgreSQL/PostGIS server: it parses the
plain-text SQL dump itself. It understands the two pieces of the dump
that matter:

    CREATE TABLE schema.table (
        col1 type,
        col2 type,
        ...
    );

    COPY schema.table (col1, col2, ...) FROM stdin;
    <tab-separated data rows, PostgreSQL COPY TEXT format>
    \\.

Each wanted table becomes one layer in the output GeoPackage, named after
the table. Geometry columns are stored as EWKB hex in the dump; they are
decoded with shapely. All tables in this dump share SRID 25832
(ETRS89 / UTM zone 32N), which is used as the layer CRS.

Usage
-----
    python sql_to_gpkg.py INPUT.sql OUTPUT.gpkg [--config config.yaml]

Example
-------
    python sql_to_gpkg.py Basisdata_25832_N50Kartdata_PostGIS.sql n50.gpkg
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import geopandas as gpd
import pandas as pd
from shapely import wkb as shapely_wkb
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)

try:
    import yaml
except ImportError:  # pragma: no cover - fallback if PyYAML isn't installed
    yaml = None


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------

def load_wanted_tables(config_path: Path) -> list[str]:
    """Read the config file and return a flat, de-duplicated list of table
    names to extract. The config may be YAML or JSON, and may nest table
    names under arbitrary category keys (e.g. punkter/linjer/flater) or
    provide a flat top-level list -- both shapes are accepted.
    """
    text = config_path.read_text(encoding="utf-8")

    if config_path.suffix.lower() in (".yml", ".yaml"):
        if yaml is None:
            raise RuntimeError(
                "PyYAML is required to read a .yaml/.yml config file. "
                "Install it with: pip install pyyaml"
            )
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)

    tables: list[str] = []

    def collect(node):
        if isinstance(node, str):
            tables.append(node)
        elif isinstance(node, list):
            for item in node:
                collect(item)
        elif isinstance(node, dict):
            for item in node.values():
                collect(item)

    collect(data)

    # de-duplicate while preserving order
    seen = set()
    ordered = []
    for t in tables:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


# --------------------------------------------------------------------------
# SQL dump parsing
# --------------------------------------------------------------------------

@dataclass
class ColumnDef:
    name: str
    pg_type: str  # normalised: integer, smallint, double precision,
    #                            boolean, date, text, geometry


@dataclass
class TableDef:
    name: str
    columns: dict[str, ColumnDef] = field(default_factory=dict)
    geom_column: Optional[str] = None


CREATE_TABLE_RE = re.compile(r"^CREATE TABLE\s+([\w.]+)\s*\($")
COPY_RE = re.compile(r"^COPY\s+([\w.]+)\s*\(([^)]*)\)\s+FROM\s+stdin;$")


def normalise_type(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("geometry"):
        return "geometry"
    if raw in ("integer", "smallint", "bigint"):
        return raw
    if raw == "double precision":
        return raw
    if raw == "boolean":
        return raw
    if raw == "date":
        return raw
    # text, character varying, etc. -> treat as text
    return "text"


def parse_create_table_block(lines: list[str], table_name: str) -> TableDef:
    """Parse the column lines between 'CREATE TABLE x (' and the closing
    ');' line."""
    tdef = TableDef(name=table_name)
    for raw_line in lines:
        line = raw_line.strip().rstrip(",")
        if not line or line == ");":
            continue
        # e.g. "objid integer NOT NULL" / "omrade public.geometry(Geometry,25832)"
        m = re.match(r'^"?([A-Za-z_][A-Za-z0-9_]*)"?\s+(.+)$', line)
        if not m:
            continue
        col_name, rest = m.group(1), m.group(2)
        rest = re.sub(r"\s+NOT NULL.*$", "", rest)
        rest = re.sub(r"\s+DEFAULT.*$", "", rest)
        rest = rest.replace("public.", "")
        pg_type = normalise_type(rest)
        tdef.columns[col_name] = ColumnDef(col_name, pg_type)
        if pg_type == "geometry":
            tdef.geom_column = col_name
    return tdef


def unescape_copy_text(value: str) -> str:
    """Undo PostgreSQL COPY TEXT format backslash-escaping for a single
    field value (the field is not NULL -- callers handle \\N separately)."""
    if "\\" not in value:
        return value
    out = []
    i = 0
    n = len(value)
    simple = {
        "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
        "v": "\v", "\\": "\\",
    }
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            nxt = value[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if nxt.isdigit():
                # up to 3 octal digits
                j = i + 1
                digits = ""
                while j < n and len(digits) < 3 and value[j].isdigit():
                    digits += value[j]
                    j += 1
                out.append(chr(int(digits, 8)))
                i = j
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# Fallback WKB parser for non-linear ("curve") geometry types
# --------------------------------------------------------------------------
#
# A handful of N50 dump records use PostGIS curve types (CompoundCurve,
# CircularString, CurvePolygon, MultiCurve, MultiSurface) that GEOS/shapely
# cannot decode directly. These are rare (usually just a few road-centre-
# line features) and, in practice, their "curved" sub-parts are almost
# always plain straight LineString segments anyway. This parser reads the
# raw (E)WKB by hand and linearises anything curved into ordinary
# Point/LineString/Polygon geometries, sampling true circular arcs
# (CircularString) into short line segments so the output is still a
# faithful GeoPackage-compatible geometry.

_WKB_POINT = 1
_WKB_LINESTRING = 2
_WKB_POLYGON = 3
_WKB_MULTIPOINT = 4
_WKB_MULTILINESTRING = 5
_WKB_MULTIPOLYGON = 6
_WKB_GEOMETRYCOLLECTION = 7
_WKB_CIRCULARSTRING = 8
_WKB_COMPOUNDCURVE = 9
_WKB_CURVEPOLYGON = 10
_WKB_MULTICURVE = 11
_WKB_MULTISURFACE = 12


def _circular_arc_points(p0, p1, p2, num_segments=16):
    """Sample a circular arc defined by three points (start, any point on
    the arc, end) into a list of (x, y) points, including both endpoints."""
    import math

    ax, ay = p0
    bx, by = p1
    cx, cy = p2

    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        # Collinear -- degenerates to a straight line.
        return [p0, p2]

    ux = ((ax ** 2 + ay ** 2) * (by - cy) + (bx ** 2 + by ** 2) * (cy - ay) + (cx ** 2 + cy ** 2) * (ay - by)) / d
    uy = ((ax ** 2 + ay ** 2) * (cx - bx) + (bx ** 2 + by ** 2) * (ax - cx) + (cx ** 2 + cy ** 2) * (bx - ax)) / d
    center = (ux, uy)
    r = math.hypot(ax - ux, ay - uy)

    a0 = math.atan2(ay - uy, ax - ux)
    a1 = math.atan2(by - uy, bx - ux)
    a2 = math.atan2(cy - uy, cx - ux)

    def norm(a, ref):
        while a - ref > math.pi:
            a -= 2 * math.pi
        while a - ref < -math.pi:
            a += 2 * math.pi
        return a

    # Walk consistently from a0 towards a2 while passing through a1.
    a1n = norm(a1, a0)
    a2n = norm(a2, a0)
    if not (min(a0, a2n) <= a1n <= max(a0, a2n)):
        # a1 not between a0 and a2 going the short way -> go the long way
        a2n = a2n + (2 * math.pi if a2n < a0 else -2 * math.pi)

    pts = []
    for i in range(num_segments + 1):
        t = a0 + (a2n - a0) * (i / num_segments)
        pts.append((center[0] + r * math.cos(t), center[1] + r * math.sin(t)))
    return pts


class _WKBReader:
    def __init__(self, data: bytes):
        self.b = data
        self.off = 0

    def read_header(self):
        order = self.b[self.off]
        self.off += 1
        endian = "<" if order == 1 else ">"
        gtype = struct.unpack_from(endian + "I", self.b, self.off)[0]
        self.off += 4
        has_z = bool(gtype & 0x80000000)
        has_m = bool(gtype & 0x40000000)
        has_srid = bool(gtype & 0x20000000)
        if has_srid:
            self.off += 4
        base = gtype & 0xFF
        return endian, base, has_z, has_m

    def read_points(self, endian, n, dim):
        fmt = endian + "d" * dim
        size = 8 * dim
        pts = []
        for _ in range(n):
            vals = struct.unpack_from(fmt, self.b, self.off)
            self.off += size
            pts.append((vals[0], vals[1]))
        return pts

    def read_uint(self, endian):
        v = struct.unpack_from(endian + "I", self.b, self.off)[0]
        self.off += 4
        return v

    def read_linear_ring_flat(self, endian, dim):
        n = self.read_uint(endian)
        return self.read_points(endian, n, dim)

    def read_curve_as_points(self):
        """Read one curve sub-geometry (LineString, CircularString or
        CompoundCurve, each with its own WKB header) and return a flat
        list of (x, y) points."""
        endian, base, has_z, has_m = self.read_header()
        dim = 2 + has_z + has_m
        if base == _WKB_LINESTRING:
            return self.read_linear_ring_flat(endian, dim)
        if base == _WKB_CIRCULARSTRING:
            n = self.read_uint(endian)
            raw_pts = self.read_points(endian, n, dim)
            pts = [raw_pts[0]]
            i = 0
            while i + 2 < len(raw_pts):
                arc = _circular_arc_points(raw_pts[i], raw_pts[i + 1], raw_pts[i + 2])
                pts.extend(arc[1:])
                i += 2
            return pts
        if base == _WKB_COMPOUNDCURVE:
            ncomp = self.read_uint(endian)
            pts: list = []
            for _ in range(ncomp):
                seg = self.read_curve_as_points()
                if pts and seg and pts[-1] == seg[0]:
                    pts.extend(seg[1:])
                else:
                    pts.extend(seg)
            return pts
        raise ValueError(f"Unsupported curve component type: {base}")

    def read_geometry(self):
        endian, base, has_z, has_m = self.read_header()
        dim = 2 + has_z + has_m

        if base == _WKB_POINT:
            (pt,) = self.read_points(endian, 1, dim)
            return Point(pt)

        if base == _WKB_LINESTRING:
            pts = self.read_linear_ring_flat(endian, dim)
            return LineString(pts) if len(pts) >= 2 else None

        if base == _WKB_POLYGON:
            nrings = self.read_uint(endian)
            rings = [self.read_linear_ring_flat(endian, dim) for _ in range(nrings)]
            if not rings:
                return None
            return Polygon(rings[0], rings[1:])

        if base == _WKB_MULTIPOINT:
            n = self.read_uint(endian)
            pts = []
            for _ in range(n):
                g = self.read_geometry()
                if g is not None:
                    pts.append(g)
            return MultiPoint(pts)

        if base == _WKB_MULTILINESTRING:
            n = self.read_uint(endian)
            lines = [g for _ in range(n) if (g := self.read_geometry()) is not None]
            return MultiLineString(lines)

        if base == _WKB_MULTIPOLYGON:
            n = self.read_uint(endian)
            polys = [g for _ in range(n) if (g := self.read_geometry()) is not None]
            return MultiPolygon(polys)

        if base == _WKB_GEOMETRYCOLLECTION:
            n = self.read_uint(endian)
            geoms = [g for _ in range(n) if (g := self.read_geometry()) is not None]
            return GeometryCollection(geoms)

        if base == _WKB_CIRCULARSTRING:
            pts = self.read_curve_as_points_from_current(base, endian, dim)
            return LineString(pts) if len(pts) >= 2 else None

        if base == _WKB_COMPOUNDCURVE:
            ncomp = self.read_uint(endian)
            pts = []
            for _ in range(ncomp):
                seg = self.read_curve_as_points()
                if pts and seg and pts[-1] == seg[0]:
                    pts.extend(seg[1:])
                else:
                    pts.extend(seg)
            return LineString(pts) if len(pts) >= 2 else None

        if base == _WKB_CURVEPOLYGON:
            nrings = self.read_uint(endian)
            rings = []
            for _ in range(nrings):
                rings.append(self.read_curve_as_points())
            if not rings:
                return None
            return Polygon(rings[0], rings[1:])

        if base == _WKB_MULTICURVE:
            n = self.read_uint(endian)
            lines = []
            for _ in range(n):
                pts = self.read_curve_as_points()
                if len(pts) >= 2:
                    lines.append(LineString(pts))
            return MultiLineString(lines)

        if base == _WKB_MULTISURFACE:
            n = self.read_uint(endian)
            polys = []
            for _ in range(n):
                g = self.read_geometry()
                if g is not None:
                    polys.append(g)
            return MultiPolygon(polys)

        raise ValueError(f"Unsupported WKB geometry base type: {base}")

    def read_curve_as_points_from_current(self, base, endian, dim):
        # Used only when the top-level geometry itself is a CircularString.
        n = self.read_uint(endian)
        raw_pts = self.read_points(endian, n, dim)
        pts = [raw_pts[0]]
        i = 0
        while i + 2 < len(raw_pts):
            arc = _circular_arc_points(raw_pts[i], raw_pts[i + 1], raw_pts[i + 2])
            pts.extend(arc[1:])
            i += 2
        return pts


def parse_wkb_linearized(data: bytes):
    """Parse (E)WKB bytes, linearising any curve geometry types, and
    return a shapely geometry."""
    return _WKBReader(data).read_geometry()


# Maps shapely's geom_type strings to the simple tag requested for the
# "geometry_type" attribute added to every feature.
_GEOM_TYPE_TAG = {
    "Point": "POINT",
    "MultiPoint": "POINT",
    "LineString": "LINE",
    "MultiLineString": "LINE",
    "LinearRing": "LINE",
    "Polygon": "POLYGON",
    "MultiPolygon": "POLYGON",
}


def geometry_type_tag(geom) -> Optional[str]:
    if geom is None:
        return None
    return _GEOM_TYPE_TAG.get(geom.geom_type, geom.geom_type.upper())


def convert_value(raw: str, col: ColumnDef):
    if raw == r"\N":
        return None
    if col.pg_type == "geometry":
        wkb_bytes = bytes.fromhex(raw)
        try:
            return shapely_wkb.loads(wkb_bytes)
        except Exception:
            # Curve geometry types (CompoundCurve, CircularString, ...)
            # aren't supported by GEOS/shapely -- fall back to our own
            # linearising WKB parser.
            return parse_wkb_linearized(wkb_bytes)
    if col.pg_type in ("integer", "smallint", "bigint"):
        return int(raw)
    if col.pg_type == "double precision":
        return float(raw)
    if col.pg_type == "boolean":
        return raw in ("t", "true", "TRUE", "1")
    if col.pg_type == "date":
        try:
            return datetime.date.fromisoformat(raw)
        except ValueError:
            # dumps sometimes contain sentinel/placeholder dates; keep as text
            return raw
    return unescape_copy_text(raw)


# --------------------------------------------------------------------------
# Main extraction
# --------------------------------------------------------------------------

def strip_schema(qualified_name: str) -> str:
    return qualified_name.rsplit(".", 1)[-1]


def extract_tables(sql_path: Path, wanted: set[str]) -> dict[str, gpd.GeoDataFrame]:
    """Single sequential pass over the dump: first collect column
    definitions from CREATE TABLE blocks, then build rows from matching
    COPY blocks."""

    table_defs: dict[str, TableDef] = {}
    rows: dict[str, list[list]] = {name: [] for name in wanted}
    found_tables: set[str] = set()

    state = "SCANNING"  # SCANNING | IN_CREATE | IN_COPY
    current_table = None
    current_lines: list[str] = []
    current_tdef: Optional[TableDef] = None
    current_cols: list[str] = []

    total_lines = 0
    with sql_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            total_lines += 1
            stripped = line.rstrip("\n")

            if state == "SCANNING":
                m = CREATE_TABLE_RE.match(stripped)
                if m:
                    current_table = strip_schema(m.group(1))
                    current_lines = []
                    state = "IN_CREATE"
                    continue
                m = COPY_RE.match(stripped)
                if m:
                    tname = strip_schema(m.group(1))
                    if tname in wanted:
                        found_tables.add(tname)
                        current_table = tname
                        current_tdef = table_defs.get(tname)
                        current_cols = [c.strip() for c in m.group(2).split(",")]
                        state = "IN_COPY"
                    continue

            elif state == "IN_CREATE":
                if stripped == ");":
                    tdef = parse_create_table_block(current_lines, current_table)
                    table_defs[current_table] = tdef
                    state = "SCANNING"
                    current_table = None
                    current_lines = []
                else:
                    current_lines.append(stripped)

            elif state == "IN_COPY":
                if stripped == r"\.":
                    state = "SCANNING"
                    current_table = None
                    current_tdef = None
                    current_cols = []
                    continue
                if current_tdef is None:
                    # Column defs for this table weren't found (shouldn't
                    # happen in a well-formed dump) -- skip its rows.
                    continue
                fields = stripped.split("\t")
                if len(fields) != len(current_cols):
                    # Defensive: malformed / wrapped line, skip it rather
                    # than crash the whole conversion.
                    continue
                record = []
                for col_name, raw_val in zip(current_cols, fields):
                    col = current_tdef.columns.get(col_name)
                    if col is None:
                        record.append(raw_val if raw_val != r"\N" else None)
                        continue
                    record.append(convert_value(raw_val, col))
                rows[current_table].append(record)

    missing = wanted - found_tables
    if missing:
        print(
            f"Warning: {len(missing)} configured table(s) were not found "
            f"in the dump (skipped): {', '.join(sorted(missing))}",
            file=sys.stderr,
        )

    # Build GeoDataFrames
    result: dict[str, gpd.GeoDataFrame] = {}
    for tname in sorted(found_tables):
        tdef = table_defs.get(tname)
        if tdef is None:
            print(f"Warning: no CREATE TABLE found for '{tname}', skipping.", file=sys.stderr)
            continue
        data = rows[tname]
        if not data:
            print(f"Note: '{tname}' has 0 rows in the dump, skipping empty layer.", file=sys.stderr)
            continue

        # Recover the column order actually used in the COPY statement by
        # re-reading the first parsed row's keys via tdef.columns order as
        # a fallback; but we already know the exact column order in the
        # dump, so track it per-table.
        col_order = list(tdef.columns.keys())
        df = pd.DataFrame(data, columns=col_order)

        geom_col = tdef.geom_column
        if geom_col is None or geom_col not in df.columns:
            print(f"Warning: '{tname}' has no geometry column, skipping.", file=sys.stderr)
            continue

        gdf = gpd.GeoDataFrame(df, geometry=geom_col, crs="EPSG:25832")
        gdf["geometry_type"] = gdf.geometry.apply(geometry_type_tag)
        result[tname] = gdf

    return result


def write_geopackage(tables: dict[str, gpd.GeoDataFrame], out_path: Path) -> None:
    if out_path.exists():
        out_path.unlink()
    for i, (name, gdf) in enumerate(tables.items()):
        print(f"Writing layer '{name}' ({len(gdf)} features)...")
        gdf.to_file(out_path, layer=name, driver="GPKG")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_sql", type=Path, help="Path to the PostGIS pg_dump .sql file")
    parser.add_argument("output_gpkg", type=Path, help="Path to write the output .gpkg file")
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.yaml"),
        help="Path to config file listing which tables to extract (default: config.yaml next to this script)",
    )
    args = parser.parse_args()

    if not args.input_sql.exists():
        sys.exit(f"Input SQL file not found: {args.input_sql}")
    if not args.config.exists():
        sys.exit(f"Config file not found: {args.config}")

    wanted = set(load_wanted_tables(args.config))
    if not wanted:
        sys.exit("No tables listed in config file.")

    print(f"Looking for {len(wanted)} table(s) from config: {', '.join(sorted(wanted))}")
    print(f"Parsing {args.input_sql} ...")

    tables = extract_tables(args.input_sql, wanted)

    if not tables:
        sys.exit("No matching, non-empty tables found -- nothing to write.")

    print(f"Writing {len(tables)} layer(s) to {args.output_gpkg} ...")
    write_geopackage(tables, args.output_gpkg)
    print("Done.")


if __name__ == "__main__":
    main()
