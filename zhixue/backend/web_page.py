from codecs import BOM_UTF16_BE, BOM_UTF16_LE, BOM_UTF32_BE, BOM_UTF32_LE, BOM_UTF8, lookup
from dataclasses import dataclass
import re


MAX_WEB_SECTIONS = 1000
CHARSET = re.compile(rb"<meta\b[^>]*?\bcharset\s*=\s*[\"']?([A-Za-z0-9._-]+)", re.IGNORECASE)
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCKS = {"address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption", "hr",
          "li", "main", "p", "section", "table", "tr"}
INVISIBLE = (
    "nav | footer | script | style | noscript | template | form | button | input | iframe | "
    "object | embed | svg | *[@hidden or @aria-hidden='true' or @role='navigation']"
)


@dataclass(frozen=True)
class WebText:
    title: str
    encoding: str
    sections: tuple[tuple[str, str], ...]
    limited: bool = False
    replaced: bool = False


def decode_page(source: bytes, declared: str) -> tuple[str, str, bool]:
    if source.startswith(BOM_UTF8):
        encoding = "utf-8-sig"
    elif source.startswith((BOM_UTF32_LE, BOM_UTF32_BE)):
        encoding = "utf-32"
    elif source.startswith((BOM_UTF16_LE, BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        found = CHARSET.search(source[:4096]) if not declared else None
        label = declared or (found.group(1).decode("ascii") if found else "utf-8")
        try:
            encoding = lookup(label).name
            if encoding in {"gb2312", "gbk"}:
                encoding = "gb18030"
            elif encoding == "iso8859-1":
                encoding = "cp1252"
        except LookupError:
            encoding = "utf-8"
    try:
        return source.decode(encoding), encoding, False
    except UnicodeError:
        return source.decode(encoding, errors="replace"), encoding, True
    except LookupError:
        return source.decode("utf-8", errors="replace"), "utf-8", True


def html_tree(text: str):
    from lxml import html

    if not text.strip():
        return None
    return html.document_fromstring(text.encode("utf-8"), parser=html.HTMLParser(
        encoding="utf-8", no_network=True, recover=True,
    ))


def page_title(source: bytes, declared: str) -> tuple[str, str]:
    from lxml.etree import ParserError

    text, encoding, _ = decode_page(source, declared)
    try:
        document = html_tree(text)
    except (ParserError, ValueError):
        document = None
    title = document.findtext("./head/title", "") if document is not None else ""
    return " ".join(title.split())[:175], encoding


def extract_page(source: bytes, declared: str, max_chars: int) -> WebText:
    from lxml.etree import ParserError

    text, encoding, replaced = decode_page(source, declared)
    try:
        document = html_tree(text)
    except (ParserError, ValueError):
        document = None
    if document is None:
        return WebText("", encoding, (), replaced=replaced)

    title = " ".join(document.findtext("./head/title", "").split())
    matches = document.xpath("//main | //*[@role='main']")
    if not matches:
        matches = document.xpath("//article")
    body = matches[0] if len(matches) == 1 else document.find("body")
    if body is None or body.get("hidden") is not None or body.get("aria-hidden") == "true":
        return WebText(title, encoding, (), replaced=replaced)
    for item in body.xpath(" | ".join(f".//{query}" for query in INVISIBLE.split(" | "))):
        if item.getparent() is not None:
            item.drop_tree()

    sections = []
    buffer: list[str] = []
    heading = ""
    remaining = max_chars
    limited = False

    def flush():
        nonlocal remaining, limited
        body_text = "\n".join(line.rstrip() for line in "".join(buffer).strip().splitlines())
        body_text = re.sub(r"\n{3,}", "\n\n", body_text)
        buffer.clear()
        if not body_text.strip():
            return
        if remaining <= 0 or len(sections) == MAX_WEB_SECTIONS:
            limited = True
            return
        if len(body_text) > remaining:
            body_text = body_text[:remaining].rstrip()
            limited = True
        sections.append((heading, body_text))
        remaining -= len(body_text)

    def add_space(text: str | None):
        if text:
            buffer.append(re.sub(r"\s+", " ", text))

    def walk(node):
        nonlocal heading
        tag = node.tag
        if not isinstance(tag, str) or limited:
            return
        if tag in HEADINGS:
            name = " ".join(node.text_content().split())
            if name:
                flush()
                heading = name[:80]
                buffer.append(name + "\n")
            return
        if tag == "pre":
            buffer.extend(("\n", node.text_content().strip("\n"), "\n"))
            return
        if tag in BLOCKS:
            buffer.append("\n")
        add_space(node.text)
        for child in node:
            walk(child)
            if limited:
                return
            add_space(child.tail)
        if tag in BLOCKS:
            buffer.append("\n")
        elif tag in {"td", "th"}:
            buffer.append("\t")

    walk(body)
    if not limited:
        flush()
    return WebText(title, encoding, tuple(sections), limited=limited, replaced=replaced)
