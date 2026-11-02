"""Generate sample GST bills for trying the scanner.

    python samples/make_samples.py

Writes JPG / PNG / PDF bills and a ZIP of all of them into samples/, plus
expected.json with the correct figures so you can compare what the AI read.
All companies, GSTINs and figures are fictional. Every bill is marked SAMPLE.
"""
import io
import json
import math
import os
import random
import zipfile

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = os.path.dirname(os.path.abspath(__file__))
W, H = 1240, 1754                     # A4 at 150 dpi

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "C:/Windows/Fonts", "/Library/Fonts"]


def font(size, bold=False):
    names = (["DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"] if bold
             else ["DejaVuSans.ttf", "arial.ttf", "Arial.ttf"])
    for d in FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default()


F = {k: font(s, b) for k, (s, b) in {
    "title": (34, True), "h": (22, True), "b": (18, True), "t": (18, False), "s": (15, False), "xs": (13, False)}.items()}

# ── amount in words (Indian system) ─────────────────────────────────────────
ONES = "Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen " \
       "Sixteen Seventeen Eighteen Nineteen".split()
TENS = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()


def _words(n):
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        return ONES[n // 100] + " Hundred" + ("" if n % 100 == 0 else " " + _words(n % 100))
    for div, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        if n >= div:
            return _words(n // div) + f" {name}" + ("" if n % div == 0 else " " + _words(n % div))


def rupees_in_words(x):
    r = int(round(x))
    return f"Rupees {_words(r)} Only"


def inr(x):
    """1234567.5 -> '12,34,567.50'"""
    neg = x < 0
    s = f"{abs(x):.2f}"
    whole, dec = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if neg else "") + whole + "." + dec


# ── invoice maths ──────────────────────────────────────────────────────────
def compute(inv):
    inter = inv["seller"]["gstin"][:2] != inv["buyer"]["gstin"][:2] if inv["buyer"].get("gstin") else False
    rows, by_rate = [], {}
    for it in inv["items"]:
        taxable = round(it["qty"] * it["rate"] * (1 - it.get("disc", 0) / 100), 2)
        rows.append({**it, "taxable": taxable})
        by_rate.setdefault(it["gst"], 0)
        by_rate[it["gst"]] += taxable
    for ch in inv.get("charges", []):          # freight etc. taxed at its own rate
        by_rate.setdefault(ch["gst"], 0)
        by_rate[ch["gst"]] += ch["amount"]
    breakup = []
    for rate, taxable in sorted(by_rate.items()):
        tax = round(taxable * rate / 100, 2)
        if inter:
            breakup.append({"rate": rate, "taxable": round(taxable, 2), "cgst": 0, "sgst": 0, "igst": tax})
        else:
            half = round(tax / 2, 2)
            breakup.append({"rate": rate, "taxable": round(taxable, 2), "cgst": half, "sgst": half, "igst": 0})
    taxable_total = round(sum(b["taxable"] for b in breakup), 2)
    cgst = round(sum(b["cgst"] for b in breakup), 2)
    sgst = round(sum(b["sgst"] for b in breakup), 2)
    igst = round(sum(b["igst"] for b in breakup), 2)
    raw = round(taxable_total + cgst + sgst + igst, 2)
    total = float(round(raw))
    return {"rows": rows, "breakup": breakup, "inter": inter, "taxable_total": taxable_total,
            "cgst": cgst, "sgst": sgst, "igst": igst, "round_off": round(total - raw, 2), "grand_total": total}


# ── drawing ────────────────────────────────────────────────────────────────
def text(d, xy, s, f="t", fill=(20, 20, 20), anchor="la"):
    d.text(xy, str(s), font=F[f], fill=fill, anchor=anchor)


def box(d, x0, y0, x1, y1, w=2):
    d.rectangle([x0, y0, x1, y1], outline=(40, 40, 40), width=w)


def watermark(img):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((img.width // 2, img.height // 2), "SAMPLE", font=font(200, True),
                               fill=(200, 30, 30, 28), anchor="mm")
    layer = layer.rotate(30, center=(img.width // 2, img.height // 2))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


COLS = [("Sr", 50, "l"), ("Description of Goods / Services", 380, "l"), ("HSN/SAC", 110, "l"),
        ("Qty", 80, "r"), ("Unit", 70, "l"), ("Rate", 120, "r"), ("Disc%", 70, "r"),
        ("GST%", 70, "r"), ("Taxable Value", 170, "r")]


def draw_header(d, inv, page_no, pages):
    s, b = inv["seller"], inv["buyer"]
    m = 60
    text(d, (W // 2, 50), s["name"], "title", fill=inv.get("color", (20, 50, 120)), anchor="ma")
    text(d, (W // 2, 96), s["address"], "s", anchor="ma")
    text(d, (W // 2, 120), f"GSTIN: {s['gstin']}   |   State: {s['state']} ({s['gstin'][:2]})   |   Ph: {s['phone']}",
         "s", anchor="ma")
    d.line([m, 150, W - m, 150], fill=(40, 40, 40), width=3)
    text(d, (W // 2, 165), inv.get("heading", "TAX INVOICE"), "h", anchor="ma")
    if pages > 1:
        text(d, (W - m, 170), f"Page {page_no} of {pages}", "xs", anchor="ra")
    y = 205
    box(d, m, y, W // 2, y + 170)
    box(d, W // 2, y, W - m, y + 170)
    text(d, (m + 12, y + 10), "Bill To:", "b")
    text(d, (m + 12, y + 38), b["name"], "b")
    yy = y + 64
    for line in b["address"].split("\n"):
        text(d, (m + 12, yy), line, "s")
        yy += 22
    if b.get("gstin"):
        text(d, (m + 12, yy), f"GSTIN: {b['gstin']}", "s")
        yy += 22
    text(d, (m + 12, yy), f"State: {b['state']}", "s")
    x = W // 2 + 12
    for i, (k, v) in enumerate([("Invoice No.", inv["number"]), ("Invoice Date", inv["date"]),
                                ("Place of Supply", inv["pos"]), ("Payment Terms", inv.get("terms", "30 days")),
                                ("Vehicle / Ref.", inv.get("ref", "-"))]):
        text(d, (x, y + 12 + i * 30), k, "s", fill=(90, 90, 90))
        text(d, (x + 190, y + 12 + i * 30), f": {v}", "b" if i < 2 else "t")
    return y + 190


def draw_items(d, rows, y, start_sr):
    m = 60
    x = m
    box(d, m, y, W - m, y + 36)
    d.rectangle([m + 2, y + 2, W - m - 2, y + 34], fill=(232, 236, 244))
    for name, w, al in COLS:
        text(d, (x + (w - 10 if al == "r" else 8), y + 9), name, "s" if len(name) < 20 else "s",
             anchor="ra" if al == "r" else "la")
        x += w
    y += 36
    for i, r in enumerate(rows):
        vals = [start_sr + i, r["desc"], r["hsn"], f"{r['qty']:g}", r["unit"], inr(r["rate"]),
                f"{r.get('disc', 0):g}" if r.get("disc") else "-", f"{r['gst']:g}", inr(r["taxable"])]
        x = m
        for (name, w, al), v in zip(COLS, vals):
            text(d, (x + (w - 10 if al == "r" else 8), y + 9), v, "t", anchor="ra" if al == "r" else "la")
            x += w
        y += 34
        d.line([m, y, W - m, y], fill=(210, 210, 210), width=1)
    box(d, m, y - 34 * len(rows) - 36, W - m, y)
    return y


def draw_totals(d, inv, c, y):
    m = 60
    y += 30
    for ch in inv.get("charges", []):
        text(d, (W - m - 230, y), f"{ch['name']} (GST {ch['gst']:g}%)", "t", anchor="ra")
        text(d, (W - m - 10, y), inr(ch["amount"]), "t", anchor="ra")
        y += 30
    # GST summary table on the left
    ty = y
    hdr = ["GST%", "Taxable", "IGST"] if c["inter"] else ["GST%", "Taxable", "CGST", "SGST"]
    widths = [90, 160, 140] if c["inter"] else [90, 160, 130, 130]
    x = m
    for h_, w in zip(hdr, widths):
        text(d, (x + w - 10, ty), h_, "b", anchor="ra")
        x += w
    ty += 30
    for bk in c["breakup"]:
        vals = [f"{bk['rate']:g}%", inr(bk["taxable"])] + ([inr(bk["igst"])] if c["inter"]
                                                            else [inr(bk["cgst"]), inr(bk["sgst"])])
        x = m
        for v, w in zip(vals, widths):
            text(d, (x + w - 10, ty), v, "t", anchor="ra")
            x += w
        ty += 28
    box(d, m - 6, y - 8, m + sum(widths) + 6, ty + 4, 1)

    # Totals on the right
    lines = [("Taxable Amount", c["taxable_total"])]
    if c["inter"]:
        lines.append(("IGST", c["igst"]))
    else:
        lines += [("CGST", c["cgst"]), ("SGST", c["sgst"])]
    lines.append(("Round Off", c["round_off"]))
    ry = y
    for k, v in lines:
        text(d, (W - m - 230, ry), k, "t", anchor="ra")
        text(d, (W - m - 10, ry), inr(v), "t", anchor="ra")
        ry += 30
    d.line([W - m - 420, ry, W - m, ry], fill=(40, 40, 40), width=2)
    text(d, (W - m - 230, ry + 10), "GRAND TOTAL", "h", anchor="ra")
    text(d, (W - m - 10, ry + 10), "₹ " + inr(c["grand_total"]), "h", anchor="ra")
    y = max(ty, ry + 50) + 24
    text(d, (m, y), "Amount in words: " + rupees_in_words(c["grand_total"]), "b")
    y += 50
    if inv.get("bank"):
        text(d, (m, y), "Bank: " + inv["bank"], "s")
        y += 24
    text(d, (m, y), "Declaration: We declare that this invoice shows the actual price of the goods described.", "xs",
         fill=(90, 90, 90))
    text(d, (W - m, y + 60), f"For {inv['seller']['name']}", "b", anchor="ra")
    text(d, (W - m, y + 140), "Authorised Signatory", "s", anchor="ra")
    text(d, (W // 2, H - 40), "This is a computer generated SAMPLE invoice - fictional data for testing.", "xs",
         fill=(150, 150, 150), anchor="ma")


def render(inv, rows_per_page=18):
    c = compute(inv)
    chunks = [c["rows"][i:i + rows_per_page] for i in range(0, len(c["rows"]), rows_per_page)]
    pages = []
    for p, chunk in enumerate(chunks, 1):
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        y = draw_header(d, inv, p, len(chunks))
        y = draw_items(d, chunk, y, (p - 1) * rows_per_page + 1)
        if p == len(chunks):
            draw_totals(d, inv, c, y)
        else:
            text(d, (W - 70, y + 16), "Continued on next page…", "s", anchor="ra")
        pages.append(watermark(img))
    return pages, c


def photocopy(img, seed=11):
    """Grey, low-contrast, speckled - like a photocopy that was scanned again."""
    rnd = random.Random(seed)
    g = img.convert("L").point(lambda v: int(70 + v * 0.62))
    g = g.rotate(0.8, resample=Image.BICUBIC, fillcolor=228)
    px = g.load()
    for _ in range(60000):
        x, y = rnd.randrange(g.width), rnd.randrange(g.height)
        px[x, y] = max(0, px[x, y] - rnd.randint(40, 120))
    return g.filter(ImageFilter.GaussianBlur(0.6)).convert("RGB")


def phone_photo(img, angle=-2.2, seed=7):
    """Make a clean page look like a photo taken on a phone: tilt, shadow, noise, blur."""
    rnd = random.Random(seed)
    bg = Image.new("RGB", (img.width + 160, img.height + 160), (96, 84, 70))       # wooden desk
    paper = img.copy()
    # uneven lighting
    shade = Image.new("L", paper.size, 0)
    sd = ImageDraw.Draw(shade)
    for i in range(0, paper.width, 8):
        v = int(40 * (1 - math.cos(i / paper.width * math.pi)) / 2)
        sd.line([i, 0, i, paper.height], fill=v, width=8)
    paper = Image.composite(Image.new("RGB", paper.size, (60, 55, 50)), paper, shade.point(lambda v: v // 2))
    bg.paste(paper, (80, 80))
    out = bg.rotate(angle, resample=Image.BICUBIC, fillcolor=(80, 70, 58))
    px = out.load()
    for _ in range(40000):
        x, y = rnd.randrange(out.width), rnd.randrange(out.height)
        r, g, b = px[x, y]
        n = rnd.randint(-18, 18)
        px[x, y] = (max(0, min(255, r + n)), max(0, min(255, g + n)), max(0, min(255, b + n)))
    return out.filter(ImageFilter.GaussianBlur(0.7))


# ── the sample bills ───────────────────────────────────────────────────────
DEMO = {"name": "Demo Traders", "address": "12, Ring Road Industrial Estate\nAhmedabad - 380015",
        "gstin": "24AAAAA0000A1Z5", "state": "Gujarat"}

BILLS = [
    {
        "file": "1_purchase_intra_state_18pct.jpg",
        "what": "Purchase, same state (CGST + SGST), single rate 18%, round-off. Party already in Tally (by GSTIN).",
        "seller": {"name": "Sharma Steel Pvt Ltd", "address": "Plot 44, GIDC Vatva, Ahmedabad - 382445, Gujarat",
                   "gstin": "24AAACS1234F1Z5", "state": "Gujarat", "phone": "079-2583 1100"},
        "buyer": DEMO, "number": "SS/1042", "date": "05/04/2026", "pos": "24-Gujarat", "ref": "GJ01 AB 4521",
        "items": [
            {"desc": "MS Plate 10mm", "hsn": "7208", "qty": 12, "unit": "Nos", "rate": 4250.00, "gst": 18},
            {"desc": "MS Angle 50x50x5", "hsn": "7216", "qty": 40, "unit": "Mtr", "rate": 312.50, "gst": 18},
            {"desc": "MS Flat 40x6", "hsn": "7211", "qty": 25.5, "unit": "Kg", "rate": 68.75, "gst": 18, "disc": 2},
        ],
        "bank": "Bank of Baroda, A/c 1234 5678 9012, IFSC BARB0VATVAX",
    },
    {
        "file": "2_purchase_interstate_igst_12_18.png",
        "what": "Purchase from another state (IGST), two GST rates 12% and 18%. New party (not in Tally).",
        "color": (110, 30, 30),
        "seller": {"name": "Kaveri Electricals & Co.", "address": "B-7, MIDC Bhosari, Pune - 411026, Maharashtra",
                   "gstin": "27AAKFK5521M1Z3", "state": "Maharashtra", "phone": "+91 20 2712 4455"},
        "buyer": DEMO, "number": "KEC/26-27/0387", "date": "18-04-2026", "pos": "24-Gujarat", "terms": "45 days",
        "items": [
            {"desc": "LED Panel Light 18W", "hsn": "9405", "qty": 50, "unit": "Pcs", "rate": 385.00, "gst": 12},
            {"desc": "Copper Wire 1.5 sq mm (90m)", "hsn": "8544", "qty": 20, "unit": "Coil", "rate": 1420.00, "gst": 18},
            {"desc": "MCB 32A Double Pole", "hsn": "8536", "qty": 30, "unit": "Pcs", "rate": 465.00, "gst": 18, "disc": 5},
            {"desc": "PVC Conduit Pipe 25mm", "hsn": "3917", "qty": 100, "unit": "Mtr", "rate": 38.40, "gst": 18},
        ],
    },
    {
        "file": "3_purchase_two_pages_5_18pct_freight.pdf",
        "what": "Two-page scanned PDF, 22 items, GST 5% and 18%, freight charged on the bill.",
        "color": (20, 100, 60),
        "seller": {"name": "Annapurna Packaging Industries", "address": "Survey 118, Changodar, Ahmedabad - 382213, Gujarat",
                   "gstin": "24AAHFA7788K1ZQ", "state": "Gujarat", "phone": "98250 44321"},
        "buyer": DEMO, "number": "API-2261", "date": "02.05.2026", "pos": "24-Gujarat",
        "items": [
            {"desc": f"Corrugated Box {s} (5 ply)", "hsn": "4819", "qty": q, "unit": "Nos", "rate": r, "gst": 18}
            for s, q, r in [("12x10x8", 500, 18.60), ("14x12x10", 400, 24.25), ("16x12x12", 300, 29.80),
                            ("18x14x12", 250, 34.50), ("20x16x14", 200, 41.75), ("24x18x16", 150, 55.20),
                            ("10x8x6", 600, 12.40), ("8x6x4", 800, 8.75)]
        ] + [
            {"desc": f"Kraft Paper Roll {g} GSM", "hsn": "4804", "qty": q, "unit": "Kg", "rate": r, "gst": 18}
            for g, q, r in [(80, 220, 46.50), (100, 180, 48.00), (120, 150, 51.25), (150, 120, 54.80)]
        ] + [
            {"desc": f"Jute Bag {s}", "hsn": "6305", "qty": q, "unit": "Pcs", "rate": r, "gst": 5}
            for s, q, r in [("Small", 300, 22.00), ("Medium", 250, 31.50), ("Large", 200, 42.00), ("XL", 120, 55.00)]
        ] + [
            {"desc": f"BOPP Tape {w} x 65m", "hsn": "3919", "qty": q, "unit": "Roll", "rate": r, "gst": 18}
            for w, q, r in [("48mm", 240, 38.00), ("72mm", 120, 56.50), ("24mm", 300, 21.00),
                            ("Brown 48mm", 180, 39.50), ("Printed 48mm", 100, 64.00), ("Clear 36mm", 150, 29.75)]
        ],
        "charges": [{"name": "Freight Charges", "amount": 1500.00, "gst": 18}],
    },
    {
        "file": "4_sales_invoice_by_demo_traders.jpg",
        "what": "SALES bill issued BY Demo Traders. Set your GSTIN 24AAAAA0000A1Z5 in Settings to see it become a Sales voucher.",
        "color": (60, 40, 120),
        "seller": {**DEMO, "address": "12, Ring Road Industrial Estate, Ahmedabad - 380015, Gujarat", "phone": "079-4000 1234"},
        "buyer": {"name": "Patel Hardware Stores", "address": "Station Road, Anand - 388001",
                  "gstin": "24ABQPP9087L1Z2", "state": "Gujarat"},
        "number": "DT/S/0915", "date": "21/05/2026", "pos": "24-Gujarat",
        "items": [
            {"desc": "MS Plate 10mm", "hsn": "7208", "qty": 4, "unit": "Nos", "rate": 4900.00, "gst": 18},
            {"desc": "MS Angle 50x50x5", "hsn": "7216", "qty": 15, "unit": "Mtr", "rate": 365.00, "gst": 18},
        ],
    },
    {
        "file": "5_courier_bill_phone_photo.jpg",
        "what": "Service bill (SAC), photographed on a phone: tilted, shadowed, slightly blurred.",
        "color": (180, 90, 0),
        "heading": "TAX INVOICE (SERVICES)",
        "seller": {"name": "SwiftLine Couriers", "address": "4th Floor, Sakar-II, Ellisbridge, Ahmedabad - 380006, Gujarat",
                   "gstin": "24AAJCS3344B1ZK", "state": "Gujarat", "phone": "1800 200 3344"},
        "buyer": DEMO, "number": "SLC/AMD/77812", "date": "30-May-2026", "pos": "24-Gujarat", "terms": "Immediate",
        "items": [
            {"desc": "Courier charges - Domestic (May 2026)", "hsn": "996812", "qty": 1, "unit": "Job", "rate": 2845.00, "gst": 18},
            {"desc": "Fuel surcharge", "hsn": "996812", "qty": 1, "unit": "Job", "rate": 341.40, "gst": 18},
        ],
        "photo": True,
    },
    {
        "file": "6_textile_yarn_purchase_5pct.jpg",
        "what": "Textile: cotton and polyester yarn, GST 5%, same state, quantities in Kg.",
        "color": (0, 90, 110),
        "seller": {"name": "Surat Yarn Mills LLP", "address": "Plot 21-24, Sachin GIDC, Surat - 394230, Gujarat",
                   "gstin": "24AAPFS4471K1ZB", "state": "Gujarat", "phone": "0261-239 7788"},
        "buyer": DEMO, "number": "SYM/26-27/1184", "date": "08/06/2026", "pos": "24-Gujarat",
        "ref": "LR 55120 / GJ05 CT 7711",
        "items": [
            {"desc": "Cotton Yarn 30s Combed", "hsn": "5205", "qty": 1250, "unit": "Kg", "rate": 268.00, "gst": 5},
            {"desc": "Cotton Yarn 40s Combed", "hsn": "5205", "qty": 800, "unit": "Kg", "rate": 294.50, "gst": 5},
            {"desc": "Polyester Spun Yarn 2/30", "hsn": "5509", "qty": 600, "unit": "Kg", "rate": 162.75, "gst": 5,
             "disc": 1.5},
        ],
        "bank": "ICICI Bank, A/c 0045 0550 1184, IFSC ICIC0000045",
    },
    {
        "file": "7_job_work_dyeing_bill_5pct.png",
        "what": "Textile job work (dyeing & processing) service bill, SAC 998821, GST 5%.",
        "color": (120, 20, 90),
        "heading": "TAX INVOICE (JOB WORK)",
        "seller": {"name": "Rangoli Dyeing & Processing House", "address": "Pandesara GIDC, Surat - 394221, Gujarat",
                   "gstin": "24AAKFR9032P1Z4", "state": "Gujarat", "phone": "98790 11223"},
        "buyer": DEMO, "number": "RDP/0412", "date": "15-06-2026", "pos": "24-Gujarat", "terms": "15 days",
        "ref": "Challan 3381 dt 10-06-2026",
        "items": [
            {"desc": "Dyeing charges - Cotton fabric (reactive)", "hsn": "998821", "qty": 4200, "unit": "Mtr",
             "rate": 14.50, "gst": 5},
            {"desc": "Finishing & calendaring charges", "hsn": "998821", "qty": 4200, "unit": "Mtr", "rate": 3.25,
             "gst": 5},
            {"desc": "Shrinkage control (sanforising)", "hsn": "998821", "qty": 1800, "unit": "Mtr", "rate": 2.10,
             "gst": 5},
        ],
    },
    {
        "file": "8_credit_note_igst.jpg",
        "what": "CREDIT NOTE from a supplier (goods returned), IGST 18%, against bill KEC/26-27/0387.",
        "color": (110, 30, 30),
        "heading": "CREDIT NOTE",
        "seller": {"name": "Kaveri Electricals & Co.", "address": "B-7, MIDC Bhosari, Pune - 411026, Maharashtra",
                   "gstin": "27AAKFK5521M1Z3", "state": "Maharashtra", "phone": "+91 20 2712 4455"},
        "buyer": DEMO, "number": "KEC/CN/0042", "date": "02-05-2026", "pos": "24-Gujarat", "terms": "-",
        "ref": "Against Inv KEC/26-27/0387",
        "items": [
            {"desc": "MCB 32A DP - returned (defective)", "hsn": "8536", "qty": 6, "unit": "Pcs",
             "rate": 465.00, "gst": 18, "disc": 5},
        ],
    },
    {
        "file": "9_dyes_chemicals_photocopy.jpg",
        "what": "Dyes & chemicals purchase, GST 18%, grey low-contrast photocopy with speckles.",
        "color": (40, 40, 40),
        "seller": {"name": "Ankleshwar Dyestuff Corporation", "address": "Plot 3307, GIDC Ankleshwar - 393002, Gujarat",
                   "gstin": "24AABCA6620E1ZS", "state": "Gujarat", "phone": "02646-221 900"},
        "buyer": DEMO, "number": "ADC/2026/0977", "date": "22.06.2026", "pos": "24-Gujarat",
        "items": [
            {"desc": "Reactive Red ME4BL", "hsn": "3204", "qty": 150, "unit": "Kg", "rate": 412.00, "gst": 18},
            {"desc": "Reactive Yellow ME4GL", "hsn": "3204", "qty": 120, "unit": "Kg", "rate": 389.00, "gst": 18},
            {"desc": "Soda Ash Light", "hsn": "2836", "qty": 2000, "unit": "Kg", "rate": 31.40, "gst": 18},
            {"desc": "Wetting Agent (non-ionic)", "hsn": "3402", "qty": 250, "unit": "Kg", "rate": 118.00, "gst": 18},
        ],
        "photocopy": True,
    },
]


def make_excel_samples():
    """Two Excel files: a bill register (one row per bill / GST rate) and ledger-line vouchers."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        print("openpyxl not installed - skipping Excel samples")
        return []

    def book(path, heads, rows, widths):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(heads)
        for r in rows:
            ws.append(r)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="2457D6")
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        wb.save(path)
        return path

    bills = book(os.path.join(OUT, "10_excel_bill_register.xlsx"),
                 ["Date", "Voucher Type", "Party", "Party GSTIN", "Bill No", "Taxable", "GST %", "CGST", "SGST",
                  "IGST", "Round Off", "Total", "Narration"],
                 [["01-07-2026", "Purchase", "Surat Yarn Mills LLP", "24AAPFS4471K1ZB", "SYM/26-27/1302", 184500, 5,
                   "", "", "", "", "", "Cotton yarn 30s"],
                  ["03-07-2026", "Purchase", "Rangoli Dyeing & Processing House", "24AAKFR9032P1Z4", "RDP/0451",
                   52600, 5, "", "", "", "", "", "Dyeing job work July"],
                  ["05-07-2026", "Purchase", "Kaveri Electricals & Co.", "27AAKFK5521M1Z3", "KEC/26-27/0455",
                   12400, 12, "", "", "", "", "", "LED fittings"],
                  ["05-07-2026", "Purchase", "Kaveri Electricals & Co.", "27AAKFK5521M1Z3", "KEC/26-27/0455",
                   23850, 18, "", "", "", "", "", "Wires and MCBs (same bill, 18%)"],
                  ["09-07-2026", "Purchase", "Ankleshwar Dyestuff Corporation", "24AABCA6620E1ZS", "ADC/2026/1033",
                   96750, 18, 8707.50, 8707.50, "", 0, 114165, "Reactive dyes"],
                  ["12-07-2026", "Sales", "Patel Hardware Stores", "24ABQPP9087L1Z2", "DT/S/1022", 41300, 18,
                   "", "", "", "", "", "Steel sale"]],
                 [12, 13, 34, 18, 18, 12, 8, 10, 10, 10, 10, 12, 30])
    ledger = book(os.path.join(OUT, "11_excel_payments_receipts_journal.xlsx"),
                  ["Voucher No", "Date", "Voucher Type", "Ledger", "Debit", "Credit", "Bill Ref", "Narration"],
                  [["PMT-101", "10-07-2026", "Payment", "Surat Yarn Mills LLP", 193725, "", "SYM/26-27/1302",
                    "NEFT UTR 6123344556"],
                   ["PMT-101", "10-07-2026", "Payment", "HDFC Bank", "", 193725, "", ""],
                   ["RCT-55", "15-07-2026", "Receipt", "HDFC Bank", 48734, "", "", "From Patel Hardware"],
                   ["RCT-55", "15-07-2026", "Receipt", "Patel Hardware Stores", "", 48734, "DT/S/1022", ""],
                   ["JV-12", "31-07-2026", "Journal", "Electricity Charges", 38600, "", "", "July power bill provision"],
                   ["JV-12", "31-07-2026", "Journal", "Outstanding Expenses", "", 38600, "", ""],
                   ["JV-13", "31-07-2026", "Journal", "Depreciation", 21500, "", "", "Monthly depreciation"],
                   ["JV-13", "31-07-2026", "Journal", "Plant & Machinery", "", 21500, "", ""]],
                  [12, 12, 13, 30, 12, 12, 18, 32])
    print("10_excel_bill_register.xlsx, 11_excel_payments_receipts_journal.xlsx")
    return [bills, ledger]


def main():
    expected = {}
    written = []
    for inv in BILLS:
        pages, c = render(inv)
        path = os.path.join(OUT, inv["file"])
        if inv.get("photocopy"):
            pages = [photocopy(p) for p in pages]
        if inv.get("photo"):
            pages = [phone_photo(p) for p in pages]
        if path.endswith(".pdf"):
            pages[0].save(path, "PDF", resolution=150, save_all=True, append_images=pages[1:])
        elif path.endswith(".png"):
            pages[0].save(path, "PNG", optimize=True)
        else:
            pages[0].save(path, "JPEG", quality=88)
        written.append(path)
        expected[inv["file"]] = {
            "what": inv["what"], "supplier": inv["seller"]["name"], "supplier_gstin": inv["seller"]["gstin"],
            "buyer": inv["buyer"]["name"], "invoice_number": inv["number"], "invoice_date_printed": inv["date"],
            "pages": len(pages), "taxable_total": c["taxable_total"], "cgst": c["cgst"], "sgst": c["sgst"],
            "igst": c["igst"], "round_off": c["round_off"], "grand_total": c["grand_total"],
            "gst_breakup": c["breakup"],
        }
        print(f"{inv['file']:45s} {len(pages)}p  total {inr(c['grand_total']):>12s}")
    written += make_excel_samples()
    zpath = os.path.join(OUT, "all_samples.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in written:
            if not p.endswith(".xlsx"):
                z.write(p, "bills/" + os.path.basename(p))
    with open(os.path.join(OUT, "expected.json"), "w", encoding="utf-8") as f:
        json.dump(expected, f, indent=2, ensure_ascii=False)
    print("all_samples.zip, expected.json")


if __name__ == "__main__":
    main()
