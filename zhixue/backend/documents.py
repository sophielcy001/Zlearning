from dataclasses import asdict, dataclass
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

TEXT_FORMATS = {"txt", "md"}
IMAGE_FORMATS = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG"}
RASTER_FORMATS = {"pdf"} | IMAGE_FORMATS.keys()
FORMATS = TEXT_FORMATS | RASTER_FORMATS | {"docx", "html"}
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 200_000
MAX_PDF_PAGES = 250
MAX_TEXT_LENGTH = 500_000
MAX_SECTIONS = 20_000
MAX_IMAGE_PIXELS = 24_000_000
MAX_OCR_TEXT = 30_000


@dataclass(frozen=True)
class Section:
    number: int
    label: str
    text: str
    text_revision: str | None = None


@dataclass(frozen=True)
class Extracted:
    content: str = ""
    sections: tuple[Section, ...] = ()
    status: str = "ready"
    message: str | None = None
    page_count: int | None = None
    unreadable_pages: tuple[int, ...] = ()


def extract_text(source: bytes) -> Extracted:
    try:
        content = source.decode("utf-8")
    except UnicodeDecodeError:
        return Extracted(status="failed", message="文件无法按 UTF-8 读取，可下载原件后检查编码。")
    lines = content.removeprefix("\ufeff").splitlines()
    sections = tuple(Section(number, f"第 {number} 行", line) for number, line in enumerate(lines[:MAX_SECTIONS], 1))
    if len(lines) > MAX_SECTIONS:
        return Extracted(content, sections, "partial", f"已索引前 {MAX_SECTIONS} 行，可下载原件查看其余内容。")
    if not content.removeprefix("\ufeff").strip():
        return Extracted(content, sections, "empty", "文件中没有可搜索文字。")
    return Extracted(content, sections)


def extract_pdf(source: bytes) -> Extracted:
    import pymupdf

    with pymupdf.open(stream=source, filetype="pdf") as document:
        if not document.is_pdf or document.needs_pass:
            return Extracted(status="failed", message="文件格式异常或 PDF 已加密。可下载原件查看。")
        sections = []
        unreadable = []
        total_chars = 0
        for number in range(1, min(MAX_PDF_PAGES, document.page_count) + 1):
            try:
                body = document[number - 1].get_text("text", sort=True).strip()
            except (RuntimeError, ValueError):
                body = ""
            if total_chars + len(body) > MAX_TEXT_LENGTH:
                break
            total_chars += len(body)
            sections.append(Section(number, f"第 {number} 页", body))
            if not body:
                unreadable.append(number)
        skipped = document.page_count - len(sections)
        has_text = total_chars > 0
        message = "部分页面没有可提取文字，可查看原页。" if unreadable else None
        if skipped:
            limit = f"前 {len(sections)} 页已检查，余下 {skipped} 页超过本次提取范围。"
            message = f"{message or ''}{limit}"
        if not has_text:
            message = f"未提取到可搜索文字。{message or ''}"
        return Extracted(
            content="\n\n".join(section.text for section in sections if section.text), sections=tuple(sections),
            status="partial" if has_text and (unreadable or skipped) else "ready" if has_text else "empty",
            message=message, page_count=document.page_count, unreadable_pages=tuple(unreadable),
        )


def extract_word(source: bytes) -> Extracted:
    from docx import Document
    from docx.text.paragraph import Paragraph

    with ZipFile(BytesIO(source)) as archive:
        members = archive.infolist()
        if len(members) > 3000 or sum(item.file_size for item in members) > 64 * 1024 * 1024:
            return Extracted(status="failed", message="Word 文件解压后过大，可下载原件查看。")

    document = Document(BytesIO(source))
    sections = []
    paragraphs = 0
    tables = 0
    total_chars = 0

    def add(label: str, body: str) -> bool:
        nonlocal total_chars
        total_chars += len(body)
        if len(sections) == MAX_SECTIONS or total_chars > MAX_TEXT_LENGTH:
            return False
        sections.append(Section(len(sections) + 1, label, body))
        return True

    limited = False
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            paragraphs += 1
            limited = not add(f"第 {paragraphs} 段", block.text)
        else:
            tables += 1
            for number, row in enumerate(block.rows, 1):
                if not add(f"表 {tables} · 第 {number} 行", " | ".join(cell.text for cell in row.cells)):
                    limited = True
                    break
        if limited:
            break
    text = "\n\n".join(section.text for section in sections if section.text.strip())
    if not text:
        return Extracted(status="empty", message="未提取到 Word 正文文字，可下载原件查看。")
    message = "已提取正文段落和表格文字。版式、页码、图像及公式请查看原件。"
    if limited:
        message = f"已达到正文提取上限。{message}"
    return Extracted(text, tuple(sections), "partial" if limited else "ready", message)


def open_image(source: bytes, file_format: str):
    from PIL import Image

    image = Image.open(BytesIO(source), formats=[IMAGE_FORMATS[file_format]])
    if image.width * image.height > MAX_IMAGE_PIXELS:
        image.close()
        raise ValueError("图片像素超出读取上限。")
    return image


def extract_image(source: bytes, file_format: str) -> Extracted:
    from PIL import Image

    try:
        with open_image(source, file_format) as image:
            image.load()
    except (OSError, ValueError, Image.DecompressionBombError):
        return Extracted(status="failed", message="图片无法读取或超过 2400 万像素，可下载原件查看。")
    return Extracted(
        status="empty", message="可在本机识别图片文字，校对后即可搜索。", page_count=1, unreadable_pages=(1,),
    )


def render_page(source: bytes, file_format: str, number: int, *, for_ocr: bool = False) -> bytes:
    max_edge = 2800 if for_ocr else 1400
    if file_format == "pdf":
        import pymupdf

        with pymupdf.open(stream=source, filetype="pdf") as document:
            page = document[number - 1]
            scale = min(200 / 72 if for_ocr else 2, max_edge / max(page.rect.width, page.rect.height))
            return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")

    from PIL import Image, ImageOps

    with open_image(source, file_format) as original:
        image = ImageOps.exif_transpose(original)
        image.thumbnail((max_edge, max_edge))
        rgba = image.convert("RGBA")
        white = Image.new("RGBA", rgba.size, "white")
        rendered = BytesIO()
        Image.alpha_composite(white, rgba).convert("RGB").save(rendered, format="PNG")
        return rendered.getvalue()


def recognize_page(source: bytes, file_format: str, number: int) -> dict:
    import pymupdf

    # run_parser starts this worker at the application root. Tesseract on Windows
    # can open this relative path even if the application root contains Unicode.
    tessdata = "backend/tessdata"
    raster = pymupdf.Pixmap(render_page(source, file_format, number, for_ocr=True))
    layer = raster.pdfocr_tobytes(language="chi_sim+eng", tessdata=tessdata)
    with pymupdf.open(stream=layer, filetype="pdf") as document:
        recognized = document[0].get_text("text", sort=True).strip()
    return {"text": recognized[:MAX_OCR_TEXT], "truncated": len(recognized) > MAX_OCR_TEXT}


def extract_html(source: bytes, encoding: str) -> Extracted:
    from .web_page import extract_page

    page = extract_page(source, encoding, MAX_TEXT_LENGTH)
    sections = tuple(
        Section(number, f"网页第 {number} 节" + (f" · {heading}" if heading else ""), body)
        for number, (heading, body) in enumerate(page.sections, 1)
    )
    if not sections:
        return Extracted(status="empty", message="网页快照未提取到正文。可下载快照，或打开来源查看动态内容。")
    messages = []
    if page.replaced:
        messages.append("部分字符无法按网页编码还原，可下载快照检查原件。")
    if page.limited:
        messages.append(f"已索引网页正文的前 {len(sections)} 节共 {sum(len(item.text) for item in sections)} 个字符，余下内容可查看快照。")
    return Extracted(
        content="\n\n".join(section.text for section in sections), sections=sections,
        status="partial" if messages else "ready", message="".join(messages) or None,
    )


def run_parser(source: bytes, *arguments: str) -> bytes:
    # Each binary document is handled in its own process, outside the SQLite write transaction.
    entry = ["--parse-document"] if getattr(sys, "frozen", False) else ["-m", "backend.documents"]
    timeout = {"ocr": 45, "fetch-web": 30}.get(arguments[0], 15)
    result = subprocess.run(
        [sys.executable, *entry, *arguments],
        cwd=Path(__file__).resolve().parents[1], input=source,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return result.stdout


def extract(source: bytes, file_format: str, *, encoding: str | None = None) -> Extracted:
    if file_format in TEXT_FORMATS:
        return extract_text(source)
    label = {"pdf": "PDF", "docx": "Word", "html": "网页"}.get(file_format, "图片")
    arguments = [file_format, encoding or ""] if file_format == "html" else [file_format]
    try:
        value = json.loads(run_parser(source, *arguments))
    except subprocess.TimeoutExpired:
        return Extracted(status="failed", message=f"{label} 文字提取超时，可下载原件查看。")
    except (OSError, ValueError, subprocess.CalledProcessError):
        return Extracted(status="failed", message=f"无法读取 {label} 正文，可下载原件检查。")
    value["sections"] = tuple(Section(**section) for section in value["sections"])
    value["unreadable_pages"] = tuple(value["unreadable_pages"])
    return Extracted(**value)


def main(arguments: list[str]) -> None:
    source = sys.stdin.buffer.read(MAX_DOCUMENT_BYTES + 1)
    if len(source) > MAX_DOCUMENT_BYTES:
        raise SystemExit(1)
    mode = arguments[0]
    if mode == "fetch-web":
        from .web_fetch import write_snapshot

        write_snapshot(source)
        return
    if mode == "page":
        sys.stdout.buffer.write(render_page(source, arguments[1], int(arguments[2])))
        return
    if mode == "ocr":
        value = recognize_page(source, arguments[1], int(arguments[2]))
    elif mode == "pdf":
        value = asdict(extract_pdf(source))
    elif mode in IMAGE_FORMATS:
        value = asdict(extract_image(source, mode))
    elif mode == "html":
        value = asdict(extract_html(source, arguments[1]))
    else:
        value = asdict(extract_word(source))
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main(sys.argv[1:])
