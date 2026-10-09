"""One measured A4 layout drives both HTML/SVG printing and server-side PDF.

Coordinates are points from the top left, measured against Titus PO.pdf.
Financial strings are already calculated by domain.py; floats here are geometry only.
"""
from base64 import b64encode
from html import escape
from io import BytesIO
from pathlib import Path
import os
from reportlab.pdfbase.pdfmetrics import stringWidth, registerFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

WIDTH, HEIGHT = 595.32, 841.92
LEFT, RIGHT = 71.5, 510
LOGO = Path(__file__).resolve().parent.parent / "static" / "po-logo.png"
FONT_DIR = Path(os.getenv("PO_FONT_DIR", str(Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts")))
FONT_FILES = {"Times-Roman": "LFAX.TTF", "Times-Bold": "LFAXD.TTF", "Times-Italic": "LFAXI.TTF", "Helvetica-Bold": "GOTHICB.TTF"}
FONTS = {}
for logical, filename in FONT_FILES.items():
    path = FONT_DIR / filename
    if path.is_file():
        name = "PO-" + logical
        registerFont(TTFont(name, str(path)))
        FONTS[logical] = name


def text_width(value, font, size):
    return stringWidth(value, FONTS.get(font, font), size)


def wrap(value, width, font="Times-Roman", size=8):
    lines = []
    for paragraph in str(value).split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = (line + " " + word).strip()
            if text_width(candidate, font, size) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ""
            for char in word:
                if line and text_width(line + char, font, size) > width:
                    lines.append(line)
                    line = ""
                line += char
        lines.append(line)
    return lines or [""]


class Page:
    def __init__(self):
        self.commands = []

    def text(self, x, y, value, size=8, font="Times-Roman", align="left", max_width=None):
        value = str(value)
        if max_width:
            while size > 5 and text_width(value, font, size) > max_width:
                size -= .25
        self.commands.append(("text", x, y, value, size, font, align))

    def line(self, x1, y1, x2, y2, shade=.75, weight=.35):
        self.commands.append(("line", x1, y1, x2, y2, shade, weight))

    def rect(self, x, y, width, height, fill=None):
        self.commands.append(("rect", x, y, width, height, fill))

    def block(self, x, y, value, width, size=8, font="Times-Roman", leading=11):
        for line in wrap(value, width, font, size):
            self.text(x, y, line, size, font)
            y += leading
        return y


def header(doc):
    page = Page()
    company = doc["company"]
    if LOGO.exists():
        page.commands.append(("logo", 72.36, 70.8, 110.28, 48))
    else:
        page.text(72, 116, "mizitco", 27, "Helvetica-Bold")
    page.text(188, 89, company["name"], 12, "Helvetica-Bold")
    page.text(290, 89, "(" + company["registration"] + ")", 9)
    address, country = company["address"].rsplit(", ", 1)
    page.text(188, 102, address + ",", 8, max_width=322)
    page.text(188, 111, f"{country}   Tel: {company['telephone']}   Fax: {company['fax']}   e-mail:{company['email']}", 8, max_width=322)
    supplier = doc["supplier"]
    sy = page.block(100, 147, supplier["name"], 235, 9, "Times-Bold")
    sy = page.block(100, sy + 3, supplier["details"], 235)
    sy = max(199, sy + 8)
    sy = max(page.block(100, sy, "Attn.: " + supplier["attention"], 124),
             page.block(230, sy, "Fax: " + supplier["fax"], 110))
    page.text(72, 133, "Order From:", 9)
    page.text(502, 135, "PURCHASE ORDER", 12, "Times-Bold", "right")
    ry = 150
    for label, value in (("Purchase No:", doc.get("legacy_number") or doc.get("number", "Pending")),
                         ("Date:", doc["date"]), ("Terms:", doc["terms"]),
                         ("Replacement:", doc["replacement"]), ("Currency:", doc["currency"])):
        page.text(355, ry, label, 9)
        ry = page.block(422, ry, value, 87, 8, leading=10) + 4
    bottom = max(207, sy - 3, ry - 13)
    page.rect(LEFT, 123, 273, bottom - 123)
    page.line(LEFT, 123, RIGHT, 123)
    top = bottom + 6
    page.rect(LEFT, top, RIGHT - LEFT, 14, .78)
    for x, label, align in ((75, "No.", "left"), (97, "Description", "left"),
                            (390, "Quantity", "right"), (456, "Unit Price", "right"), (509, "Amount", "right")):
        page.text(x, top + 10, label, 9, "Times-Bold", align)
    return page, top + 26


def item_lines(item):
    lines = [(97, t) for t in wrap(item["description"], 245)]
    # Two parallel columns, matching the reference's Id/Size and Mssid/Matl pairs.
    for left_label, left, right_label, right in (("Id:", item["product_id"], "Size:", item["size"]),
                                               ("Mssid:", item["mssid"], "Matl:", item["material"])):
        a, b = wrap(left, 124), wrap(right, 51)
        for i in range(max(len(a), len(b))):
            lines.append(((97, left_label if i == 0 else ""), (128.6, a[i] if i < len(a) else ""),
                          (259, right_label if i == 0 else ""), (292.8, b[i] if i < len(b) else "")))
    if item["notes"]:
        lines.append((97, ""))
        lines.extend((97, t) for t in wrap(item["notes"], 245))
    return lines


def layout(doc):
    pages = []
    page, y = header(doc)
    if y > 520:
        raise ValueError("Supplier details or order terms are too tall for the A4 header; shorten them.")
    start = y
    for index, item in enumerate(doc["items"], 1):
        lines = item_lines(item)
        height = len(lines) * 11 + 9
        if y + height > 642 and y > start:
            pages.append(page)
            page, y = header(doc)
        if y + height > 642:
            raise ValueError(f"Item {index} is too tall for one page; shorten its description or notes.")
        page.text(91, y, index, align="right")
        for x, key, width in ((390, "quantity", 44), (456, "unit_price", 62), (509, "amount", 51)):
            page.text(x, y, item[key], align="right", max_width=width)
        for line in lines:
            pairs = line if isinstance(line[0], tuple) else (line,)
            for x, value in pairs:
                page.text(x, y, value)
            y += 11
        page.line(LEFT, y, RIGHT, y, .9)
        y += 9
    # Footer is anchored like the sample and occurs only on the final page.
    page.line(LEFT, 655, RIGHT, 655)
    page.text(77, 667, "Delivery Date: " + doc["delivery_date"])
    page.text(268, 667, "E. & O. E.")
    page.text(360, 648, "Subtotal:")
    page.text(503, 648, doc["subtotal"], align="right", max_width=95)
    page.text(360, 667, "Discount:")
    page.text(503, 667, doc["discount"], align="right", max_width=95)
    page.rect(352, 673, 154, 17)
    page.text(376, 684, "Total:")
    page.text(503, 684, doc["total"], align="right", max_width=95)
    page.text(427, 704, doc["company"]["name"], align="center")
    page.line(352, 753, 496, 753, 0, .6)
    page.text(427, 764, "Authorised Signature", align="center")
    pages.append(page)
    for index, p in enumerate(pages, 1):
        p.text(510, 83, f"Page {index} of {len(pages)}", 9, "Times-Italic", "right")
        status = doc.get("status", "draft")
        if status != "finalised":
            p.text(LEFT, 795, status.upper() + " — NOT FINALISED" if status == "draft" else "CANCELLED", 8, "Helvetica-Bold")
        if index < len(pages):
            p.text(RIGHT, 667, "Continued on next page", 8, "Times-Italic", "right")
    return pages


def pdf_bytes(doc):
    output = BytesIO()
    canvas = Canvas(output, pagesize=(WIDTH, HEIGHT), pageCompression=1)
    canvas.setTitle("Purchase Order " + doc.get("number", "Preview"))
    canvas.setAuthor(doc["company"]["name"])
    for page in layout(doc):
        for command in page.commands:
            kind, *args = command
            if kind == "text":
                x, y, value, size, font, align = args
                canvas.setFillGray(0)
                canvas.setFont(FONTS.get(font, font), size)
                draw = {"left": canvas.drawString, "right": canvas.drawRightString, "center": canvas.drawCentredString}[align]
                draw(x, HEIGHT-y, value)
            elif kind == "line":
                x1, y1, x2, y2, shade, weight = args
                canvas.setStrokeGray(shade)
                canvas.setLineWidth(weight)
                canvas.line(x1, HEIGHT-y1, x2, HEIGHT-y2)
            elif kind == "rect":
                x, y, w, h, fill = args
                canvas.setStrokeGray(.65)
                canvas.setLineWidth(.35)
                if fill is not None:
                    canvas.setFillGray(fill)
                canvas.rect(x, HEIGHT-y-h, w, h, fill=int(fill is not None))
            elif kind == "logo":
                x, y, w, h = args
                canvas.drawImage(str(LOGO), x, HEIGHT-y-h, w, h, mask="auto")
        canvas.showPage()
    canvas.save()
    return output.getvalue()


def svg_pages(doc):
    output = []
    for page in layout(doc):
        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="Purchase order A4 page">']
        for command in page.commands:
            kind, *args = command
            if kind == "text":
                x, y, value, size, font, align = args
                family = "Arial, sans-serif" if "Helvetica" in font else "Times New Roman, serif"
                if font in FONTS:
                    family = ("Century Gothic, " if "Helvetica" in font else "Lucida Fax, ") + family
                weight = "bold" if "Bold" in font else "normal"
                italic = "italic" if "Italic" in font else "normal"
                anchor = {"left": "start", "right": "end", "center": "middle"}[align]
                # Fixed textLength makes PDF and browser font metrics agree.
                length = text_width(value, font, size)
                parts.append(f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" font-style="{italic}" text-anchor="{anchor}" textLength="{length}" lengthAdjust="spacingAndGlyphs">{escape(value)}</text>')
            elif kind == "line":
                x1, y1, x2, y2, shade, weight = args
                parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="rgb({shade*100}%,{shade*100}%,{shade*100}%)" stroke-width="{weight}"/>')
            elif kind == "rect":
                x, y, w, h, fill = args
                color = "none" if fill is None else f"rgb({fill*100}%,{fill*100}%,{fill*100}%)"
                parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{color}" stroke="#a6a6a6" stroke-width=".35"/>')
            elif kind == "logo":
                x, y, w, h = args
                encoded = b64encode(LOGO.read_bytes()).decode("ascii")
                parts.append(f'<image x="{x}" y="{y}" width="{w}" height="{h}" href="data:image/png;base64,{encoded}"/>')
        output.append("".join(parts) + "</svg>")
    return output
