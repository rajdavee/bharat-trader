"""Plain text out of a simple PDF (the index-change press releases), standard library only.

Good enough for text-based PDFs written by word processors: FlateDecode content streams, literal and hex
strings, Tm/Td/TD/T* positioning, TJ kerning, ToUnicode CMaps for Identity-H fonts. It is not a general
PDF reader: no encryption, no images, no font metrics. Text is returned as lines; pieces of one line that
were positioned apart (table cells) are joined with a tab.
"""

import re
import zlib

_STREAM = re.compile(rb"stream\r?\n")
_WS = b" \t\r\n\x0c\x00"
_DELIM = b"()<>[]{}/%"


def _cmap(body):
    """A ToUnicode CMap stream -> {code int: text}."""
    out = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", body, re.S):
        for a, b in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            out[int(a, 16)] = _utf16(b)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", body, re.S):
        for a, b, rest in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", block):
            lo, hi = int(a, 16), int(b, 16)
            if rest.startswith(b"["):
                for i, h in enumerate(re.findall(rb"<([0-9A-Fa-f]+)>", rest)):
                    out[lo + i] = _utf16(h)
            else:
                base = int(rest[1:-1], 16)
                for i in range(hi - lo + 1):
                    try:
                        out[lo + i] = chr(base + i)
                    except (ValueError, OverflowError):
                        pass
    return out


def _utf16(hexbytes):
    try:
        return bytes.fromhex(hexbytes.decode("ascii")).decode("utf-16-be", "replace")
    except ValueError:
        return ""


def _tokens(s):
    """Content stream -> tokens: ('n', float), ('s', bytes), ('h', bytes), ('a', [...]) or ('op', name)."""
    i, n = 0, len(s)
    stack = [[]]
    while i < n:
        c = s[i:i + 1]
        if c in (b" ", b"\t", b"\r", b"\n", b"\x0c", b"\x00"):
            i += 1
        elif c == b"%":
            j = s.find(b"\n", i)
            i = n if j < 0 else j + 1
        elif c == b"(":
            depth, j, buf = 1, i + 1, bytearray()
            while j < n and depth:
                ch = s[j]
                if ch == 0x5C and j + 1 < n:        # backslash
                    nx = s[j + 1]
                    esc = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
                    if nx in esc:
                        buf.append(esc[nx])
                        j += 2
                    elif 0x30 <= nx <= 0x37:
                        k = j + 1
                        while k < n and k < j + 4 and 0x30 <= s[k] <= 0x37:
                            k += 1
                        buf.append(int(s[j + 1:k], 8) & 0xFF)
                        j = k
                    elif nx in (0x0D, 0x0A):
                        j += 2
                    else:
                        buf.append(nx)
                        j += 2
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                    if not depth:
                        break
                buf.append(ch)
                j += 1
            stack[-1].append(("s", bytes(buf)))
            i = j + 1
        elif c == b"<":
            if s[i + 1:i + 2] == b"<":
                stack[-1].append(("op", "<<"))
                i += 2
                continue
            j = s.find(b">", i)
            j = n if j < 0 else j
            stack[-1].append(("h", re.sub(rb"\s", b"", s[i + 1:j])))
            i = j + 1
        elif c == b">":
            i += 2 if s[i + 1:i + 2] == b">" else 1
        elif c == b"[":
            stack.append([])
            i += 1
        elif c == b"]":
            arr = stack.pop() if len(stack) > 1 else []
            stack[-1].append(("a", arr))
            i += 1
        elif c == b"/":
            j = i + 1
            while j < n and s[j:j + 1] not in _WS and s[j:j + 1] not in _DELIM:
                j += 1
            stack[-1].append(("name", s[i + 1:j]))
            i = j
        else:
            j = i
            while j < n and s[j:j + 1] not in _WS and s[j:j + 1] not in _DELIM:
                j += 1
            word = s[i:j]
            i = j if j > i else i + 1
            try:
                stack[-1].append(("n", float(word)))
            except ValueError:
                stack[-1].append(("op", word.decode("latin-1")))
        if len(stack) == 1 and len(stack[0]) > 64:
            for t in stack[0]:
                yield t
            stack[0] = []
    for t in stack[0]:
        yield t


def _decode(tok, cmap):
    kind, raw = tok
    if kind == "h":
        try:
            b = bytes.fromhex(raw.decode("ascii") + ("0" if len(raw) % 2 else ""))
        except ValueError:
            return ""
        if cmap and len(b) % 2 == 0 and len(b) >= 2:
            return "".join(cmap.get((b[k] << 8) | b[k + 1], "") for k in range(0, len(b), 2))
        return b.decode("latin-1")
    if cmap and raw and len(raw) % 2 == 0 and raw[0] == 0 and len(raw) >= 2:
        return "".join(cmap.get((raw[k] << 8) | raw[k + 1], "") for k in range(0, len(raw), 2))
    return raw.decode("cp1252", "replace")


def _page_lines(body, cmap):
    """One page's content -> [(baseline y, line text)] in drawing order. Pieces on one baseline are joined with nothing, a space or a tab
    (a table cell), judged from the gap between where the last piece should end and where this one starts
    (glyph widths are estimated at half the font size: there are no font metrics here)."""
    lines, cur, cur_y = [], [], None
    ops = []
    x = y = lx = ly = 0.0
    leading, size, scale = 0.0, 10.0, 1.0
    end_x = None

    def show(text):
        nonlocal cur, cur_y, end_x
        if not text:
            return
        em = max(1.0, size * scale)
        if cur_y is None or abs(y - cur_y) > 0.3 * em:
            if cur:
                lines.append((cur_y, "".join(cur)))
            cur, cur_y = [], y
        elif end_x is not None and cur:
            gap = x - end_x
            if gap > 1.6 * em:
                cur.append("\t")
            elif gap > 0.2 * em and not cur[-1].endswith(" ") and not text.startswith(" "):
                cur.append(" ")
        cur.append(text)
        end_x = x + 0.5 * em * len(text)

    for tok in _tokens(body):
        if tok[0] != "op":
            ops.append(tok)
            continue
        op = tok[1]
        nums = [v for k, v in ops if k == "n"]
        if op == "BT":
            x = y = lx = ly = 0.0
            scale = 1.0
        elif op == "Tf" and nums:
            size = abs(nums[-1]) or size
        elif op == "Tm" and len(nums) >= 6:
            scale = abs(nums[-6]) or abs(nums[-3]) or 1.0
            lx, ly = nums[-2], nums[-1]
            x, y = lx, ly
        elif op in ("Td", "TD") and len(nums) >= 2:
            if op == "TD":
                leading = -nums[-1]
            lx, ly = lx + nums[-2] * scale, ly + nums[-1] * scale
            x, y = lx, ly
        elif op == "TL" and nums:
            leading = nums[-1]
        elif op in ("T*", "'", '"'):
            ly -= (leading or size * 1.2) * scale
            x, y = lx, ly
            if op != "T*":
                strs = [t for t in ops if t[0] in ("s", "h")]
                if strs:
                    show(_decode(strs[-1], cmap))
        elif op == "Tj":
            strs = [t for t in ops if t[0] in ("s", "h")]
            if strs:
                show(_decode(strs[-1], cmap))
                x = end_x
        elif op == "TJ":
            arrs = [v for k, v in ops if k == "a"]
            if arrs:
                parts = []
                for t in arrs[-1]:
                    if t[0] in ("s", "h"):
                        parts.append(_decode(t, cmap))
                    elif t[0] == "n" and t[1] < -180:
                        parts.append(" ")
                show("".join(parts))
                x = end_x
        ops = []
    if cur:
        lines.append((cur_y, "".join(cur)))
    return lines


_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
_REF = re.compile(rb"(\d+)\s+\d+\s+R\b")


def _objects(data):
    """{object number: (dict bytes, stream bytes or None)}, objects inside object streams included."""
    objs = {}
    marks = list(_OBJ.finditer(data))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(data)
        chunk = data[m.end():end]
        k = chunk.find(b"endobj")
        body = chunk if k < 0 else chunk[:k]
        sm = _STREAM.search(body)
        if sm:
            head = body[:sm.start()]
            raw = body[sm.end():]
            stream = None
            if b"FlateDecode" in head:
                try:
                    stream = zlib.decompressobj().decompress(raw)
                except zlib.error:
                    stream = None
            elif b"/Filter" not in head:
                stream = raw[:raw.rfind(b"endstream")] if b"endstream" in raw else raw
            objs[int(m.group(1))] = (head, stream)
        else:
            objs[int(m.group(1))] = (body, None)
    for num, (head, stream) in list(objs.items()):
        if stream is not None and re.search(rb"/Type\s*/ObjStm", head):
            first = re.search(rb"/First\s+(\d+)", head)
            if not first:
                continue
            first = int(first.group(1))
            nums = [int(x) for x in re.findall(rb"\d+", stream[:first])]
            pairs = list(zip(nums[0::2], nums[1::2]))
            for j, (n, off) in enumerate(pairs):
                stop = pairs[j + 1][1] if j + 1 < len(pairs) else len(stream) - first
                objs.setdefault(n, (stream[first + off:first + stop], None))
    return objs


def _pages(objs):
    """Page dicts in reading order, from the catalog's page tree (None when there is no usable tree)."""
    root = None
    for num, (head, _) in objs.items():
        if re.search(rb"/Type\s*/Catalog", head):
            m = re.search(rb"/Pages\s+(\d+)\s+\d+\s+R", head)
            root = int(m.group(1)) if m else None
            break
    if root is None:
        return None
    out, seen = [], set()

    def walk(n):
        if n in seen or n not in objs:
            return
        seen.add(n)
        head = objs[n][0]
        if re.search(rb"/Type\s*/Pages\b", head):
            kids = re.search(rb"/Kids\s*\[([^\]]*)\]", head)
            for k in _REF.findall(kids.group(1) if kids else b""):
                walk(int(k))
        elif re.search(rb"/Type\s*/Page\b", head):
            out.append(head)

    walk(root)
    return out or None


def _contents(objs, page):
    m = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", page)
    if not m:
        return b""
    refs = [int(x) for x in _REF.findall(m.group(1))]
    bodies = []
    for r in refs:
        head, stream = objs.get(r, (b"", None))
        if stream is None:
            arr = re.match(rb"\s*\[([^\]]*)\]", head)      # an indirect array of streams
            if arr:
                bodies.extend(objs.get(int(x), (b"", None))[1] or b"" for x in _REF.findall(arr.group(1)))
            continue
        bodies.append(stream)
    return b"\n".join(bodies)


def _tidy(text):
    line = "\t".join(" ".join(p.replace("\xa0", " ").split()) for p in text.split("\t"))
    return re.sub(r"\t+", "\t", line).strip("\t ")


def pdf_lines(data):
    """The text lines of a PDF (bytes): page after page, each page top to bottom (by baseline, so text
    boxes drawn out of order still read in place), whitespace tidied; a line "\f" between pages."""
    objs = _objects(data)
    cmap = {}
    for head, stream in objs.values():
        if stream and b"begincmap" in stream:
            cmap.update(_cmap(stream))
    pages = _pages(objs)
    if pages:
        bodies = [_contents(objs, p) for p in pages]
    else:       # no page tree we can read: every text-like stream in file order, as one page
        bodies = [b"\n".join(st for hd, st in objs.values() if st and b"BT" in st and b"begincmap" not in st
                             and not re.search(rb"/(Type|Subtype|Length1|Length2|N)\b", hd))]
    out = []
    for n, body in enumerate(bodies):
        if n:
            out.append("\f")
        items = _page_lines(body, cmap)
        order = sorted(range(len(items)), key=lambda i: (-round(items[i][0], 0), i))
        for i in order:
            line = _tidy(items[i][1])
            if line:
                out.append(line)
    return out
