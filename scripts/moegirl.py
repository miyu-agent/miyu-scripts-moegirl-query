#!/usr/bin/env python3
# Display name: Moegirlpedia
# 显示名称：萌娘百科
# Description: Look up anime, manga, games, VTubers and other ACG topics on Moegirlpedia (萌娘百科): find a page by name and read its infobox and text, whole or one section at a time.
# 描述：查萌娘百科：动画、漫画、游戏、虚拟主播等 ACG 条目，读资料卡和正文，长条目可以按小节取。
# Timeout: 60
# Clicall: yes
# Parameters:
# {"type": "object", "properties": {
#   "query": {"type": "string", "description": "Name to look up, in Chinese where possible (e.g. 芙莉莲). Matches page titles by prefix; without action the best hit is opened and the other hits are listed."},
#   "title": {"type": "string", "description": "Exact page title to open. Takes precedence over query."},
#   "action": {"type": "string", "enum": ["auto", "search", "page"], "description": "auto (default): open title, or look up query and open the best hit. search: list matching titles only. page: open title (or query as a title)."},
#   "section": {"type": "string", "description": "Only this section: its number from the section list (e.g. 2.1) or its heading (e.g. 人物经历). Subsections are included."},
#   "limit": {"type": "integer", "description": "Titles to list, 1-20. Default 8."},
#   "max_chars": {"type": "integer", "description": "Most characters of page text to return, 1000-40000. Default 8000; the sections that do not fit are listed so you can ask for them."},
#   "format": {"type": "string", "enum": ["md", "json"], "description": "md (default) for reading; json for code, e.g. piping miyu call into jq or python."}
# }}
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# Anonymous callers only get opensearch and the REST page HTML: api.php's parse and search answer
# "Unauthorized API call", and the mirrors (moegirl.uk) are down.
SITE = {"base": "https://zh.moegirl.org.cn", "page": "https://zh.moegirl.org.cn/", "name": "萌娘百科"}
UA = "miyu-scripts-moegirl-query/0.1 (+https://github.com/SHORiN-KiWATA)"
TIMEOUT = 20
MAX_HTML_BYTES = 6 * 1024 * 1024

class Failed(Exception):
    pass


# ---------------------------------------------------------------- HTML -> Markdown

VOID = {"br", "hr", "img", "meta", "link", "input", "wbr", "source", "col", "area", "base", "param", "track"}
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "math", "head", "title", "img", "figure", "audio", "video"}
SKIP_CLASSES = {"mw-editsection", "toc", "navbox", "reference", "mw-ref", "mw-references-wrap", "references", "noprint",
                "mw-empty-elt", "thumb", "gallery", "printfooter", "catlinks", "mw-cite-backlink", "infoBox", "TabLabelText"}
QUOTE_CLASSES = set()
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class Markdown(HTMLParser):
    """MediaWiki HTML to compact Markdown: headings, lists, code, tables, notice boxes. Links keep only their text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bufs = [[]]
        self.stack = []      # (tag, kind)
        self.skip = 0
        self.pre = 0
        self.lists = []      # [ordered, counter]
        self.tables = []     # {"rows": [...], "row": [...] | None}
        self.pending_value = False  # an infobox label just closed; the next div is its value
        self.in_value = 0

    # buffer helpers
    def out(self, text):
        self.bufs[-1].append(text)

    def tail(self):
        return "".join(self.bufs[-1][-3:])

    def newline(self, n):
        buf = self.bufs[-1]
        text = "".join(buf).rstrip(" \t")
        if not text:
            buf[:] = []
            return
        have = len(text) - len(text.rstrip("\n"))
        buf[:] = [text + "\n" * max(0, n - have)]

    def capture(self):
        self.bufs.append([])

    def release(self):
        return "".join(self.bufs.pop())

    # parser callbacks
    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            if not self.skip:
                self.void(tag)
            return
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if self.skip or tag in SKIP_TAGS or classes & SKIP_CLASSES or "display:none" in (attrs.get("style") or "").replace(" ", ""):
            self.skip += 1
            self.stack.append((tag, "skip"))
            return
        kind = tag
        if "mw:LanguageVariant" in (attrs.get("typeof") or ""):
            # Parsoid keeps -{...}- text (Japanese names and the like) only in this attribute.
            self.out(variant_text(attrs.get("data-mw-variant")))
            self.skip += 1
            self.stack.append((tag, "skip"))
            return
        style = (attrs.get("style") or "").replace(" ", "").replace("\n", "")
        if tag == "div" and "flex:00" in style:
            kind = "label"
            self.newline(1)
            self.out("- ")
        elif tag == "div" and self.pending_value:
            kind = "value"
            self.pending_value = False
            self.in_value += 1
        elif tag in HEADINGS:
            self.newline(2)
            self.capture()
        elif tag == "pre":
            self.newline(2)
            self.pre += 1
            self.capture()
        elif tag == "code" and not self.pre:
            self.out("`")
        elif tag in ("b", "strong"):
            self.out("**")
        elif tag == "sup":
            self.out(" ")
        elif tag in ("ul", "ol"):
            self.newline(1 if self.lists else 2)
            self.lists.append([tag == "ol", 1])
        elif tag == "li":
            self.newline(1)
            depth = max(1, len(self.lists))
            if self.lists and self.lists[-1][0]:
                marker = f"{self.lists[-1][1]}. "
                self.lists[-1][1] += 1
            else:
                marker = "- "
            self.out("  " * (depth - 1) + marker)
        elif tag == "dt":
            self.newline(1)
            self.out("**")
        elif tag == "dd":
            self.newline(1)
            self.out("  ")
        elif tag == "table":
            self.newline(2)
            self.tables.append({"rows": [], "row": None, "infobox": bool({"infobox", "infobox3", "moe-infobox"} & classes)})
        elif tag == "tr":
            if self.tables:
                self.tables[-1]["row"] = []
        elif tag in ("td", "th"):
            self.capture()
        elif tag == "blockquote" or classes & QUOTE_CLASSES:
            kind = "quote"
            self.newline(2)
            self.capture()
        elif tag in ("p", "section"):
            self.newline(2)
        elif tag in ("div", "dl", "center", "caption", "figcaption"):
            self.newline(1)
        self.stack.append((tag, kind))

    def void(self, tag):
        if tag == "br" and self.in_value and not self.pre:
            self.out(" / ")
        elif tag == "br":
            self.out("\n" if self.pre else " / " if len(self.bufs) > 1 and self.tables else "\n")
        elif tag == "hr":
            self.newline(2)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID or not any(t == tag for t, _ in self.stack):
            return
        while self.stack:
            open_tag, kind = self.stack.pop()
            self.finish(open_tag, kind)
            if open_tag == tag:
                break

    def finish(self, tag, kind):
        if kind == "skip":
            self.skip -= 1
            return
        if self.skip:
            return
        if kind == "label":
            self.out("：")
            self.pending_value = True
            return
        if kind == "value":
            self.in_value -= 1
            self.newline(1)
            return
        if tag in HEADINGS:
            text = " ".join(self.release().split())
            if text:
                self.newline(2)
                self.out("#" * int(tag[1]) + " " + text)
                self.newline(2)
        elif tag == "pre":
            self.pre -= 1
            code = self.release().strip("\n")
            if code.strip():
                fence = "````" if "```" in code else "```"
                self.newline(2)
                self.out(f"{fence}\n{code}\n{fence}")
                self.newline(2)
        elif tag == "code" and not self.pre:
            self.out("`")
        elif tag in ("b", "strong", "dt"):
            self.out("**")
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self.newline(1 if self.lists else 2)
        elif tag in ("td", "th"):
            text = " ".join(self.release().split())
            if self.tables and self.tables[-1]["row"] is not None:
                self.tables[-1]["row"].append((text, tag == "th"))
            elif text:
                self.out(text + " ")
        elif tag == "tr":
            if self.tables and self.tables[-1]["row"]:
                self.tables[-1]["rows"].append(self.tables[-1]["row"])
                self.tables[-1]["row"] = None
        elif tag == "table":
            if self.tables:
                self.out(render_table(self.tables.pop()))
                self.newline(2)
        elif kind == "quote":
            body = self.release().strip()
            if body:
                self.newline(2)
                self.out("\n".join("> " + line if line.strip() else ">" for line in body.split("\n")))
                self.newline(2)
        elif tag in ("p", "section"):
            self.newline(2)
        elif tag in ("div", "dl", "dd", "li", "center", "caption", "figcaption"):
            self.newline(1)

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre:
            self.out(data)
            return
        text = re.sub(r"\s+", " ", data)
        if self.tail().endswith(("\n", " ")) or not "".join(self.bufs[-1]).strip():
            text = text.lstrip(" ")
        if text:
            self.out(text)

    def result(self):
        while self.stack:
            tag, kind = self.stack.pop()
            self.finish(tag, kind)
        while len(self.bufs) > 1:
            text = self.release()
            self.out(text)
        text = "".join(self.bufs[0])
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def variant_text(raw):
    try:
        variant = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return ""
    text = ""
    if isinstance(variant.get("disabled"), dict):
        text = variant["disabled"].get("t") or ""
    elif isinstance(variant.get("twoway"), list) and variant["twoway"]:
        pick = next((v for v in variant["twoway"] if v.get("l") in ("zh-cn", "zh-hans", "zh")), variant["twoway"][0])
        text = pick.get("t") or ""
    elif isinstance(variant.get("oneway"), list) and variant["oneway"]:
        text = variant["oneway"][0].get("t") or ""
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")


def render_table(table):
    rows = [r for r in table["rows"] if any(text for text, _ in r)]
    if not rows:
        return ""
    keyed = all(len(r) == 2 and r[0][1] for r in rows if len(r) > 1)
    if table["infobox"] or (keyed and max(len(r) for r in rows) <= 2):
        lines = []
        for r in rows:
            cells = [text for text, _ in r if text]
            if len(cells) >= 2:
                lines.append(f"- {cells[0]}：{' '.join(cells[1:])}")
            elif cells:
                lines.append(f"- {cells[0]}")
        return "\n".join(lines)
    if len(rows) <= 2 and not any(is_th for r in rows for _, is_th in r):
        # Quote boxes and one-line banners are tables too; a grid adds nothing there.
        return "\n".join(" ".join(text for text, _ in r if text) for r in rows)
    width = max(len(r) for r in rows)
    grid = [[text for text, _ in r] + [""] * (width - len(r)) for r in rows]
    keep = [c for c in range(width) if any(row[c] for row in grid)]  # image columns come out empty
    if len(keep) == 1:
        return "\n".join(row[keep[0]] for row in grid if row[keep[0]])
    width = len(keep)
    lines = []
    for i, row in enumerate(grid):
        cells = [row[c].replace("|", "\\|") for c in keep]
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("|" + " --- |" * width)
    return "\n".join(lines)


def html_to_markdown(html_text):
    parser = Markdown()
    parser.feed(html_text)
    parser.close()
    return parser.result()


# ---------------------------------------------------------------- sections

def split_sections(markdown):
    """Lead plus one entry per heading, numbered like the wiki's table of contents."""
    sections = [{"number": "0", "heading": "", "level": 1, "lines": []}]
    counters = []
    fence = None
    for line in markdown.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("```", "````")):
            marker = stripped[:4] if stripped.startswith("````") else stripped[:3]
            fence = None if fence == marker else (fence or marker)
        match = None if fence or stripped.startswith("```") else re.match(r"^(#{1,6}) (.+)$", line)
        if match:
            level = len(match.group(1))
            depth = max(1, level - 1)
            counters = counters[:depth]
            counters += [0] * (depth - len(counters))
            counters[depth - 1] += 1
            sections.append({"number": ".".join(str(c) for c in counters if c), "heading": match.group(2).strip(),
                             "level": level, "lines": [line]})
        else:
            sections[-1]["lines"].append(line)
    for s in sections:
        s["text"] = "\n".join(s.pop("lines")).strip()
    if not sections[0]["text"]:
        sections.pop(0)
    return sections


def pick_section(sections, wanted):
    key = wanted.strip().lower()
    index = next((i for i, s in enumerate(sections) if s["number"] == key), None)
    if index is None:
        index = next((i for i, s in enumerate(sections) if s["heading"].lower() == key), None)
    if index is None:
        index = next((i for i, s in enumerate(sections) if key and key in s["heading"].lower()), None)
    if index is None:
        return None
    first = sections[index]
    picked = [first]
    for s in sections[index + 1:]:
        if first["number"] == "0" or s["level"] <= first["level"]:
            break
        picked.append(s)
    return picked


# ---------------------------------------------------------------- HTTP

def get(url, accept):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.geturl(), resp.read(MAX_HTML_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise Failed("missingtitle") from exc
        raise Failed(f"萌娘百科返回 HTTP {exc.code}，稍后再试。") from exc
    except urllib.error.URLError as exc:
        raise Failed(f"连不上萌娘百科：{exc.reason}。检查网络。") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise Failed(f"萌娘百科 {TIMEOUT} 秒没有回应，稍后再试。") from exc


def page_url(site, title):
    return site["page"] + urllib.parse.quote(title.replace(" ", "_"))


def search(query, limit):
    params = urllib.parse.urlencode({"action": "opensearch", "search": query, "limit": limit,
                                     "namespace": 0, "format": "json"})
    _, body = get(f"{SITE['base']}/api.php?{params}", "application/json")
    try:
        data = json.loads(body)
        titles = data[1]
    except (json.JSONDecodeError, IndexError, TypeError) as exc:
        raise Failed("萌娘百科的搜索返回看不懂（可能在维护或要验证），稍后再试。") from exc
    return [{"title": t, "url": page_url(SITE, t)} for t in titles]


def fetch_page(title):
    quoted = urllib.parse.quote(title.replace(" ", "_"), safe="")
    try:
        final, body = get(f"{SITE['base']}/rest.php/v1/page/{quoted}/html", "text/html")
    except Failed as exc:
        if str(exc) == "missingtitle":
            raise Failed(f"萌娘百科上没有标题为「{title}」的条目。用 action=search 找准确标题（按开头匹配）。") from exc
        raise
    if "<html" not in body[:2000].lower() or "MoeAuth" in body[:5000]:
        raise Failed("萌娘百科要求登录或验证，匿名读不了这一页。")
    match = re.search(r"/rest\.php/v1/page/([^/?]+)/html", final)
    landed = urllib.parse.unquote(match.group(1)).replace("_", " ") if match else title
    return {
        "title": landed,
        "redirected_from": title if landed != title.replace("_", " ") else None,
        "markdown": html_to_markdown(body),
    }


# ---------------------------------------------------------------- output

def build_page(site, page, section, max_chars, candidates):
    sections = split_sections(page["markdown"])
    if section:
        chosen = pick_section(sections, section)
        if chosen is None:
            names = "、".join(f"{s['number']} {s['heading']}" for s in sections if s["heading"] and s["number"].count(".") < 2)
            raise Failed(f"「{page['title']}」里没有小节「{section}」。有这些：{names}")
    else:
        chosen = sections
    shown, used, truncated = [], 0, False
    for s in chosen:
        if used + len(s["text"]) <= max_chars:
            shown.append(s)
            used += len(s["text"]) + 2
            continue
        room = max_chars - used
        if not shown and room > 0:
            shown.append(dict(s, text=s["text"][:room].rstrip() + "\n…（本节截断）"))
        truncated = True
        break
    shown_numbers = {s["number"] for s in shown}
    rest = []
    for s in chosen:
        if s["number"] in shown_numbers:
            continue
        # Deeper sections fold into their x.y parent: asking for the parent brings them along.
        if rest and s["number"].count(".") > 1 and s["number"].startswith(rest[-1]["number"] + "."):
            rest[-1]["chars"] += len(s["text"])
            continue
        rest.append({"number": s["number"], "heading": s["heading"], "chars": len(s["text"])})
    return {
        "site": site["name"],
        "title": page["title"],
        "url": page_url(site, page["title"]),
        "redirected_from": page["redirected_from"],
        "section": section or None,
        "content": "\n\n".join(s["text"] for s in shown),
        "truncated": truncated,
        "not_shown": rest,
        "sections": [{"number": s["number"], "heading": s["heading"]} for s in sections if s["heading"]],
        "other_hits": candidates,
    }


def render_page(result):
    lines = [f"# {result['title']}", f"来源：{result['url']}"]
    if result["redirected_from"]:
        lines.append(f"（从「{result['redirected_from']}」重定向过来）")
    lines += ["", result["content"]]
    if result["not_shown"]:
        lines += ["", f"---", f"超出 max_chars，下面这些小节没给出（字数含子节），用 section 取（编号或标题）："]
        lines += [f"- {s['number']} {s['heading'] or '（导言）'}（{s['chars']} 字）" for s in result["not_shown"]]
    if result["other_hits"]:
        lines += ["", "其他搜索结果：" + "、".join(h["title"] for h in result["other_hits"])]
    return "\n".join(lines)


def render_search(site, query, hits):
    if not hits:
        return f"{site['name']} 上没有以「{query}」开头的条目。换个写法：中文译名、原名，或更短的开头。"
    lines = [f"{site['name']} 搜索「{query}」：{len(hits)} 条", ""]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. **{h['title']}**" + (f" — {h['snippet']}" if h.get("snippet") else ""))
    lines += ["", "读页面：action=page，title 写上面的标题。"]
    return "\n".join(lines)


def int_arg(args, key, default, low, high):
    value = args.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise Failed(f"{key} 要是整数。")
    return max(low, min(high, value))


def run(args):
    if not isinstance(args, dict):
        raise Failed('参数要是一个 JSON 对象，例：{"query": "芙莉莲"}')
    action = args.get("action") or "auto"
    if action not in ("auto", "search", "page"):
        raise Failed("action 只能是 auto、search、page。")
    fmt = args.get("format") or "md"
    if fmt not in ("md", "json"):
        raise Failed("format 只能是 md 或 json。")
    query = str(args.get("query") or "").strip()
    title = str(args.get("title") or "").strip()
    section = str(args.get("section") or "").strip()
    limit = int_arg(args, "limit", 8, 1, 20)
    max_chars = int_arg(args, "max_chars", 8000, 1000, 40000)
    if not query and not title:
        raise Failed('要给 query（条目名）或 title（准确标题），例：{"query": "芙莉莲"}')

    if action == "search":
        hits = search(query or title, limit)
        return {"query": query or title, "hits": hits} if fmt == "json" else render_search(SITE, query or title, hits)

    candidates = []
    if action == "page" or title:
        target = title or query
    else:
        hits = search(query, limit)
        # opensearch only matches title prefixes; a name it misses may still be a page or a redirect.
        target = hits[0]["title"] if hits else query
        candidates = [{"title": h["title"], "url": h["url"]} for h in hits[1:]]
    result = build_page(SITE, fetch_page(target), section, max_chars, candidates)
    return result if fmt == "json" else render_page(result)


def main():
    try:
        args = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"错误：参数不是 JSON：{exc}")
        sys.exit(1)
    try:
        result = run(args)
    except Failed as exc:
        print(f"错误：{exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - the caller wants a reason, not a traceback
        print(f"错误：{type(exc).__name__}: {exc}")
        sys.exit(1)
    print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
