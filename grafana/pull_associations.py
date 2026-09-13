"""Fetch the 2026 beneficiaries from zevent.fr into grafana/associations.json and grafana/public/associations/.
Run: uv run --with pillow python grafana/pull_associations.py

zevent.fr is a React app: the list (name, logo, official site, edition) and the descriptions are compiled
into its "associations" JS chunk as JSX calls, which this script parses back into HTML. Logos are saved
under public/associations/ (mounted into Grafana's public/ folder, see compose.yaml) and shrunk to
a LOGO_PX square when Pillow is available; the dashboard shows them from /public/associations/<file>.
"""
import base64
import html
import io
import json
import re
import sys
from pathlib import Path

import httpx

SITE = "https://zevent.fr"
UA = {"User-Agent": "Mozilla/5.0 (zevent-tracker)"}
HERE = Path(__file__).parent
OUT_JSON = HERE / "associations.json"
LOGO_DIR = HERE / "public" / "associations"
LOGO_PX = 320
VOID = {"br", "img", "hr"}


def get(url):
    r = httpx.get(url, headers=UA, follow_redirects=True, timeout=30)
    r.raise_for_status()
    return r


class Jsx:
    """Minimal parser for the minified `(0,a.jsx)(tag,{props})` / `(0,a.jsxs)(...)` calls of the chunk."""

    def __init__(self, s, i):
        self.s, self.i = s, i

    def peek(self):
        return self.s[self.i]

    def eat(self, t):
        assert self.s.startswith(t, self.i), (t, self.s[self.i:self.i + 80])
        self.i += len(t)

    def ws(self):
        while self.s[self.i] in " \n\t":
            self.i += 1

    def string(self):
        q = self.peek()
        self.i += 1
        out = []
        while self.peek() != q:
            c = self.peek()
            if c == "\\":
                n = self.s[self.i + 1]
                if n == "x":
                    out.append(chr(int(self.s[self.i + 2:self.i + 4], 16)))
                    self.i += 4
                elif n == "u":
                    out.append(chr(int(self.s[self.i + 2:self.i + 6], 16)))
                    self.i += 6
                else:
                    out.append({"n": "\n", "t": "\t"}.get(n, n))
                    self.i += 2
                continue
            out.append(c)
            self.i += 1
        self.i += 1
        return "".join(out)

    def value(self):
        self.ws()
        c = self.peek()
        if c in "`'\"":
            return ("str", self.string())
        if self.s.startswith("(0,a.jsx", self.i):
            return self.element()
        if c == "[":
            self.eat("[")
            items = []
            while True:
                self.ws()
                if self.peek() == "]":
                    self.eat("]")
                    return ("list", items)
                items.append(self.value())
                self.ws()
                if self.peek() == ",":
                    self.eat(",")
        if c == "{":
            return ("obj", self.obj())
        m = re.match(r"[\w.$]+", self.s[self.i:])
        self.i += m.end()
        return ("ident", m.group())

    def obj(self):
        self.eat("{")
        d = {}
        while True:
            self.ws()
            if self.peek() == "}":
                self.eat("}")
                return d
            m = re.match(r"[\w$]+|`[^`]*`|\"[^\"]*\"", self.s[self.i:])
            key = m.group().strip('`"')
            self.i += m.end()
            self.ws()
            self.eat(":")
            d[key] = self.value()
            self.ws()
            if self.peek() == ",":
                self.eat(",")

    def element(self):
        m = re.match(r"\(0,a\.jsxs?\)\(", self.s[self.i:])
        self.i += m.end()
        self.ws()
        tag = self.string() if self.peek() == "`" else self.value()[1]
        self.ws()
        self.eat(",")
        props = self.obj()
        self.ws()
        self.eat(")")
        return ("el", tag, props)


def render(v):
    kind = v[0]
    if kind == "str":
        return html.escape(v[1], quote=False)
    if kind == "list":
        return "".join(render(x) for x in v[1])
    if kind == "ident":   # a component of the site (a video, a link widget): nothing we can show
        return ""
    tag, props = v[1], v[2]
    if not isinstance(tag, str) or not re.fullmatch(r"[a-z][a-z0-9]*", tag):
        return render(props["children"]) if "children" in props else ""
    attrs = "".join(f' {("class" if k == "className" else k)}="{html.escape(p[1])}"'
                    for k, p in props.items() if k != "children" and p[0] == "str")
    if tag in VOID:
        return f"<{tag}{attrs}>"
    return f"<{tag}{attrs}>{render(props['children']) if 'children' in props else ''}</{tag}>"


def parse_chunk(src):
    funcs = {m.group(1): Jsx(src, m.end()).value() for m in re.finditer(r"function (\w+)\(\)\{return", src)}
    i = src.find("var D=[")
    entries = re.findall(r"\{name:`([^`]*)`,logo:`([^`]*)`,link:`([^`]*)`,content:(\w+)(?:,edition:(\d+))?\}",
                         src[i:src.find("];", i) + 1])
    assets = dict(re.findall(r"(\w+)=`(/assets/[^`]+|data:image/[^`]+)`", src[i:]))
    return ([{"name": n, "logo": logo, "link": link, "edition": int(ed) if ed else None, "html": render(funcs[fn])}
             for n, logo, link, fn, ed in entries], assets)


def save_logo(name, url):
    """Save the logo `name` (the file name used by the site) from its asset URL or data URI, shrunk to LOGO_PX."""
    if url.startswith("data:"):
        data = base64.b64decode(url.split(";base64,", 1)[1])
    else:
        data = get(SITE + url).content
    path = LOGO_DIR / name
    if path.suffix != ".svg":
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data)).convert("RGBA")
            im.thumbnail((LOGO_PX, LOGO_PX))
            # centred on a square transparent canvas: a wide logo then stays inside a table's image cell
            square = Image.new("RGBA", (LOGO_PX, LOGO_PX), (0, 0, 0, 0))
            square.paste(im, ((LOGO_PX - im.width) // 2, (LOGO_PX - im.height) // 2))
            path = path.with_suffix(".png")
            buf = io.BytesIO()
            square.save(buf, "PNG", optimize=True)
            data = buf.getvalue()
        except ImportError:
            print("Pillow not installed: logos kept at their original size", file=sys.stderr)
    path.write_bytes(data)
    return path.name


def main():
    index = get(SITE + "/associations").text
    bundle = get(SITE + re.search(r'src="(/assets/index-[^"]+\.js)"', index).group(1)).text
    chunk = get(SITE + "/" + re.search(r"assets/associations-[\w-]+\.js", bundle).group()).text
    entries, assets = parse_chunk(chunk)
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    # asset variables are named after the logo file; a data URI is the one whose stem matches no asset URL
    by_stem = {re.sub(r"-[\w-]{8}(\.\w+)$", r"\1", u.rsplit("/", 1)[-1]): u for u in assets.values() if u.startswith("/")}
    data_uris = [u for u in assets.values() if u.startswith("data:")]
    for e in entries:
        url = by_stem.get(e["logo"]) or (data_uris.pop(0) if data_uris else None)
        e["logo"] = save_logo(e["logo"], url) if url else None
        print(f"{e['name']:36} {e['edition'] or '-':>5}  {e['logo']}")
    OUT_JSON.write_text(json.dumps(entries, ensure_ascii=False, indent=1) + "\n")
    print(f"wrote {OUT_JSON} ({len(entries)} entries)")


if __name__ == "__main__":
    main()
