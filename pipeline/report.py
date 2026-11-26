"""Per-property PDF report: score, map with zone rings and numbered findings, what to fix first.

The map is drawn from the run's own LidarBC orthophoto (open data), never from Google imagery.
"""
from __future__ import annotations

import io
import json
import time

import numpy as np
import pyproj
import rasterio
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Image as RLImage, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)
from shapely.geometry import Polygon, shape
from shapely.ops import transform as shp_transform

from .property import PROCESSED
from .score import ZONE_LABEL, ZONE_ORDER

ZONE_RGB = {"immediate": (178, 24, 43), "intermediate": (239, 138, 98), "extended": (253, 219, 120),
            "outer": (150, 200, 110)}
BAND_RGB = {"Low": (46, 125, 50), "Moderate": (230, 160, 0), "High": (230, 81, 0), "Extreme": (183, 28, 28)}
MAP_PX = 1400


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def map_image(pid: str, score: dict, fmt: str = "JPEG") -> bytes:
    """Map image (JPEG for the PDF, PNG on request): orthophoto, zone rings, home outline, numbered finding markers."""
    d = PROCESSED / pid
    summ = json.loads((d / "summary.json").read_text())
    epsg = int(summ["crs"].split(":")[1])
    to_utm = pyproj.Transformer.from_crs(4326, epsg, always_xy=True).transform
    zones = {f["properties"]["zone"]: shp_transform(to_utm, shape(f["geometry"]))
             for f in json.loads((d / "zones.geojson").read_text())["features"]}
    home = next(f for f in json.loads((d / "buildings.geojson").read_text())["features"]
                if f["properties"].get("home"))
    home: Polygon = shp_transform(to_utm, shape(home["geometry"]))  # type: ignore[assignment]
    minx, miny, maxx, maxy = zones["outer"].buffer(5).bounds
    side = max(maxx - minx, maxy - miny)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    minx, maxx, miny, maxy = cx - side / 2, cx + side / 2, cy - side / 2, cy + side / 2

    img = None
    if (d / "ortho.tif").exists():
        from rasterio.windows import from_bounds
        with rasterio.open(d / "ortho.tif") as src:
            rgb = src.read([1, 2, 3], window=from_bounds(minx, miny, maxx, maxy, src.transform),
                           boundless=True, fill_value=255, out_shape=(3, MAP_PX, MAP_PX))
        img = Image.fromarray(np.moveaxis(rgb, 0, -1).astype("uint8"))
    if img is None:
        img = Image.new("RGB", (MAP_PX, MAP_PX), (245, 245, 240))
    W = img.width
    px = lambda x, y: ((x - minx) / side * W, (maxy - y) / side * W)  # noqa: E731

    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(over)
    for z in reversed(ZONE_ORDER):
        g: Polygon = zones[z]  # type: ignore[assignment]
        for ring in [g.exterior] + list(g.interiors):
            dr.line([px(*c) for c in ring.coords], fill=ZONE_RGB[z] + (255,), width=5)
    dr.polygon([px(*c) for c in home.exterior.coords], outline=(0, 230, 255, 255), width=5)
    img = Image.alpha_composite(img.convert("RGBA"), over)

    dr = ImageDraw.Draw(img)
    f_num, f_lab = _font(30), _font(26)
    r = 22
    placed: list[tuple[float, float]] = []
    for f in score["findings"]:
        locs = [px(*to_utm(lng, lat)) for lng, lat in f["locations"]]
        if not locs:
            continue
        col = (183, 28, 28) if f["points"] > 0 else (90, 90, 90)
        for x, y in locs[1:]:
            dr.ellipse([x - 7, y - 7, x + 7, y + 7], fill=col + (255,), outline=(255, 255, 255, 255), width=2)
        x, y = locs[0]
        for _ in range(8):   # nudge a marker up until it no longer covers an earlier one
            if all((x - a) ** 2 + (y - b) ** 2 >= (2 * r + 4) ** 2 for a, b in placed):
                break
            y -= 2 * r + 6
        placed.append((x, y))
        dr.ellipse([x - r, y - r, x + r, y + r], fill=col + (255,), outline=(255, 255, 255, 255), width=4)
        dr.text((x, y), str(f["n"]), fill=(255, 255, 255, 255), font=f_num, anchor="mm")
    # Findings about a whole zone: their numbers on a tag at the top of that zone's ring.
    for z in ZONE_ORDER:
        ns = [str(f["n"]) for f in score["findings"] if f.get("whole_zone") and f["zone"] == z]
        if not ns:
            continue
        g = zones[z]
        x, y = px((g.bounds[0] + g.bounds[2]) / 2, g.bounds[3])
        text = "zone: " + ", ".join(ns)
        box = dr.textbbox((x, y), text, font=f_lab, anchor="mm")
        dr.rounded_rectangle([box[0] - 10, box[1] - 6, box[2] + 10, box[3] + 6], radius=10,
                             fill=ZONE_RGB[z] + (255,), outline=(255, 255, 255, 255), width=3)
        dr.text((x, y), text, fill=(0, 0, 0, 255) if z in ("extended", "outer") else (255, 255, 255, 255),
                font=f_lab, anchor="mm")
    # scale bar: 20 m
    bar = 20 / side * W
    dr.rectangle([30, W - 50, 30 + bar, W - 38], fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=2)
    dr.text((30 + bar + 10, W - 44), "20 m", fill=(255, 255, 255, 255), font=_font(26), anchor="lm",
            stroke_width=3, stroke_fill=(0, 0, 0, 255))
    dr.text((W - 40, 50), "N", fill=(255, 255, 255, 255), font=_font(40), anchor="mm",
            stroke_width=3, stroke_fill=(0, 0, 0, 255))
    dr.polygon([(W - 40, 70), (W - 52, 100), (W - 28, 100)], fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, fmt, **({"quality": 85} if fmt == "JPEG" else {"optimize": True}))
    return buf.getvalue()


def _hex(rgb) -> colors.Color:
    return colors.Color(*(c / 255 for c in rgb))


def build_pdf(pid: str, score: dict) -> bytes:
    summ = json.loads((PROCESSED / pid / "summary.json").read_text())
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["BodyText"], fontSize=9.5, leading=12.5)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10, textColor=colors.HexColor("#555555"))
    cell = ParagraphStyle("cell", parent=body, fontSize=8.5, leading=10.5)
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=19, leading=23, alignment=0, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=13, leading=16, spaceBefore=8, spaceAfter=4)
    esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731

    story = [Paragraph("Home wildfire risk report", h1),
             Paragraph(f"Property {pid} · {time.strftime('%Y-%m-%d')} · rules {score['rules_version']}", small),
             Paragraph(esc(score["label"]), small), Spacer(1, 5 * mm)]

    bc = _hex(BAND_RGB[score["band"]])
    head = Table([[Paragraph(f"<font size=26><b>{score['score']}</b></font><font size=11> / {score['max']}</font>",
                             body),
                   Paragraph(f"<font size=16 color='{bc.hexval()}'><b>{score['band']}</b></font><br/>"
                             f"LiDAR rules {score['lidar_points']} / {score['lidar_max']} · "
                             f"confirmed AI findings {score['ai_points']} / {score['ai_max']}", body)]],
                 colWidths=[45 * mm, 130 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 0), (-1, 0), 2, bc),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [head, Spacer(1, 4 * mm)]

    zrows = [["Zone", "LiDAR points", "AI points"]]
    for z in ZONE_ORDER:
        v = score["zones"][z]
        zrows.append([ZONE_LABEL[z], str(v["lidar_points"]), str(v["ai_points"])])
    zt = Table(zrows, colWidths=[60 * mm, 30 * mm, 30 * mm])
    zst = [("FONTSIZE", (0, 0), (-1, -1), 9), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
           ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.grey),
           ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    for i, z in enumerate(ZONE_ORDER, 1):
        zst.append(("LINEBEFORE", (0, i), (0, i), 5, _hex(ZONE_RGB[z])))
    zt.setStyle(TableStyle(zst))
    story += [zt, Spacer(1, 4 * mm)]

    png = map_image(pid, score)
    story += [RLImage(io.BytesIO(png), width=150 * mm, height=150 * mm),
              Paragraph("Rings: Home Ignition Zones at 1.5, 10, 30 and 100 m from the home outline (cyan). "
                        "Red numbers are scored findings; grey numbers are AI observations to check on site. "
                        "Small dots are the other trees a finding refers to. A coloured tag on a ring lists "
                        "findings about that whole zone (canopy cover, crown spacing, slope). Orthophoto: LidarBC 2019, 15 cm; "
                        "roofs lean 1–3 m in it, so the outline may look offset.", small),
              PageBreak()]

    by_n = {f["n"]: f for f in score["findings"]}
    story.append(Paragraph("What to fix first", h2))
    if score["fix_first"]:
        for k, n in enumerate(score["fix_first"], 1):
            f = by_n[n]
            story.append(Paragraph(f"<b>{k}. {esc(f['fix'])}</b> <font color='#555555'>(finding {n}, "
                                   f"{ZONE_LABEL[f['zone']]}, {f['points']} points)</font>", body))
            story.append(Spacer(1, 2))
    else:
        story.append(Paragraph("No scored findings.", body))

    story.append(Paragraph("Findings", h2))
    rows: list[list] = [["#", "Zone", "Finding", "Points", "Basis"]]
    for f in score["findings"]:
        rows.append([str(f["n"]), Paragraph(ZONE_LABEL[f["zone"]], cell),
                     Paragraph(f"<b>{esc(f['title'])}</b><br/>{esc(f['evidence'])}"
                               + (f"<br/><i>Fix: {esc(f['fix'])}</i>" if f["fix"] else ""), cell),
                     f"{f['points']} / {f['max_points']}", Paragraph(esc(f["basis"]), cell)])
    ft = Table(rows, colWidths=[8 * mm, 26 * mm, 101 * mm, 16 * mm, 27 * mm], repeatRows=1)
    ft.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8.5), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.lightgrey),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee"))]))
    story.append(ft)

    zero = [r for r in score["rules"] if r["points"] == 0]
    if zero:
        story.append(Paragraph("Checked, no points", h2))
        story.append(Paragraph("; ".join(f"{esc(r['title'])} ({esc(r['evidence'])})" for r in zero), small))

    ai_used = ", ".join(score["ai"]["backends_used"]) or "none (no current AI results)"
    story.append(KeepTogether([
        Paragraph("How this score works", h2),
        Paragraph("Up to 80 points come from fixed rules on LiDAR measurements in each zone (tree crowns near "
                  "the house, canopy cover, crown spacing, slope). Up to 20 points come from AI findings on things "
                  "LiDAR cannot see (roof covering, woodpiles, combustibles against the walls), counted only when "
                  "the operator marked them correct or both AI models report the same concern. Bands: Low under "
                  "20, Moderate 20–39, High 40–59, Extreme 60 and over. Full rules: docs/RISK_SCORE.md.", body),
        Paragraph(f"AI results used: {esc(ai_used)}. Operator marks: {score['ai']['marks']}. Home outline: "
                  f"{esc(score['home']['outline'] or '')}, {score['home']['area_m2']} m².", small),
        Spacer(1, 3 * mm),
        Paragraph("Limits: LiDAR (2019) cannot tell conifers from deciduous trees and does not show what changed "
                  "since 2019. This is a screening tool, not a site visit, and not an official FireSmart "
                  "assessment. A FireSmart Local Representative can do a free home assessment.", small),
        Paragraph("Data: " + esc(summ.get("attribution", "LidarBC, ParcelMap BC (Open Government Licence – "
                                                   "British Columbia)")), small),
    ]))

    buf = io.BytesIO()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(18 * mm, 10 * mm, "Based on FireSmart BC guidance. Not an official FireSmart assessment.")
        canvas.drawRightString(letter[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=15 * mm,
                            bottomMargin=18 * mm, title=f"Home wildfire risk report {pid}",
                            author="FS Risk Mapping prototype")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
