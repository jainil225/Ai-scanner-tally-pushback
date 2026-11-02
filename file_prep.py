"""Turn whatever the customer uploads into a list of JPG pages for the AI.

Accepted: JPG / JPEG / PNG / WEBP / BMP / TIFF images, PDF files (every page is
rendered to a JPG), and ZIP files containing any of these (nested folders are
fine; nested ZIPs are opened one level deep).

Every page becomes one `Page` (name + JPG bytes). One bill can span several
pages; scanner.py groups pages that came from the same source file into one
document, so a 3-page PDF invoice is read by the AI as one bill.
"""
import io
import os
import zipfile
from dataclasses import dataclass, field

try:
    import pymupdf as fitz            # PyMuPDF >= 1.24
except ImportError:                    # pragma: no cover
    import fitz                        # older PyMuPDF

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
PDF_EXT = {".pdf"}
ZIP_EXT = {".zip"}

MAX_PAGES_PER_DOC = 10        # a bill longer than this is almost certainly a mistake
MAX_ZIP_MEMBERS = 500
MAX_ZIP_TOTAL = 300 * 1024 * 1024   # unpacked bytes; guards against zip bombs
RENDER_DPI = 170              # enough for small print on A4 without huge files
MAX_SIDE = 2200               # px; larger images are scaled down
JPEG_QUALITY = 85


class PrepError(ValueError):
    pass


@dataclass
class Document:
    """One bill: one image, or all pages of one PDF."""
    name: str
    pages: list = field(default_factory=list)   # list of JPG bytes


def _to_jpg(img_bytes):
    """Normalise any raster image to an RGB JPG no larger than MAX_SIDE."""
    pix = fitz.Pixmap(img_bytes)
    if pix.alpha or pix.n - (1 if pix.alpha else 0) != 3:
        pix = fitz.Pixmap(fitz.csRGB, pix)       # CMYK / gray / alpha -> RGB
        if pix.alpha:
            pix = fitz.Pixmap(pix, 0)
    longest = max(pix.width, pix.height)
    if longest > MAX_SIDE:
        doc = fitz.open()
        page = doc.new_page(width=pix.width, height=pix.height)
        page.insert_image(page.rect, pixmap=pix)
        scale = MAX_SIDE / longest
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
    return pix.tobytes("jpg", jpg_quality=JPEG_QUALITY)


def image_to_document(name, data):
    try:
        return Document(name, [_to_jpg(data)])
    except Exception as e:
        raise PrepError(f"{name}: not a readable image ({e})")


def pdf_to_document(name, data):
    """Render each PDF page to JPG (the 'PDF into JPG' step)."""
    try:
        pdf = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise PrepError(f"{name}: not a readable PDF ({e})")
    if pdf.needs_pass:
        raise PrepError(f"{name}: PDF is password protected")
    if pdf.page_count == 0:
        raise PrepError(f"{name}: PDF has no pages")
    pages = []
    zoom = RENDER_DPI / 72.0
    for i, page in enumerate(pdf):
        if i >= MAX_PAGES_PER_DOC:
            break
        r = page.rect
        z = min(zoom, MAX_SIDE / max(r.width, r.height, 1))
        pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
        pages.append(pix.tobytes("jpg", jpg_quality=JPEG_QUALITY))
    return Document(name, pages)


def zip_to_documents(name, data, depth=0):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PrepError(f"{name}: not a valid ZIP file")
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_ZIP_MEMBERS:
        raise PrepError(f"{name}: too many files in ZIP (max {MAX_ZIP_MEMBERS})")
    if sum(i.file_size for i in infos) > MAX_ZIP_TOTAL:
        raise PrepError(f"{name}: ZIP is too large when unpacked")
    docs, skipped = [], []
    for info in sorted(infos, key=lambda i: i.filename.lower()):
        base = os.path.basename(info.filename)
        # macOS resource forks and hidden files
        if not base or base.startswith(".") or "__MACOSX" in info.filename:
            continue
        ext = os.path.splitext(base)[1].lower()
        if ext in ZIP_EXT and depth >= 1:
            skipped.append(f"{info.filename} (nested ZIP)")
            continue
        if ext not in IMAGE_EXT | PDF_EXT | ZIP_EXT:
            skipped.append(info.filename)
            continue
        member = zf.read(info)
        try:
            d, s = load_file(f"{name}/{info.filename}", member, depth + 1)
            docs.extend(d)
            skipped.extend(s)
        except PrepError as e:
            skipped.append(str(e))
    return docs, skipped


def load_file(name, data, depth=0):
    """-> (documents, skipped_messages)."""
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return [image_to_document(name, data)], []
    if ext in PDF_EXT:
        return [pdf_to_document(name, data)], []
    if ext in ZIP_EXT:
        return zip_to_documents(name, data, depth)
    # Unknown extension: sniff the content
    if data[:4] == b"%PDF":
        return [pdf_to_document(name, data)], []
    if data[:2] == b"PK":
        return zip_to_documents(name, data, depth)
    try:
        return [image_to_document(name, data)], []
    except PrepError:
        raise PrepError(f"{name}: unsupported file type (use JPG, PNG, PDF or ZIP)")
