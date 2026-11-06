# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pillow",
#   "pyvips[binary]",
# ]
# ///
"""Clipboard Type Viewer (Windows / Tkinter)

Windows のクリップボードに入っている全フォーマット（EMF, CF_DIB, HTML Format など）を
列挙し、Hex ダンプ・テキスト・画像でプレビューする。

起動:  uv run cliptype_tk.py          (Windows 側の uv で実行すること)
       uv run cliptype_tk.py --dump   (GUI なしで一覧を標準出力に出す)
"""
import ctypes
import io
import math
import struct
import sys
import time

if sys.platform != "win32":
    sys.exit("Windows 側の uv で実行してください（WSL からなら: uv.exe run cliptype_tk.py）")

from ctypes import wintypes as w

# ======================================================================
# Win32 クリップボード
# ======================================================================
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


def _sig(f, res, *args):
    f.restype = res
    f.argtypes = list(args)


_sig(user32.OpenClipboard, w.BOOL, w.HWND)
_sig(user32.CloseClipboard, w.BOOL)
_sig(user32.EmptyClipboard, w.BOOL)
_sig(user32.EnumClipboardFormats, w.UINT, w.UINT)
_sig(user32.GetClipboardData, w.HANDLE, w.UINT)
_sig(user32.SetClipboardData, w.HANDLE, w.UINT, w.HANDLE)
_sig(user32.GetClipboardFormatNameW, ctypes.c_int, w.UINT, w.LPWSTR, ctypes.c_int)
_sig(user32.GetClipboardSequenceNumber, w.DWORD)
_sig(kernel32.GlobalSize, ctypes.c_size_t, w.HANDLE)
_sig(kernel32.GlobalLock, ctypes.c_void_p, w.HANDLE)
_sig(kernel32.GlobalUnlock, w.BOOL, w.HANDLE)
_sig(kernel32.GlobalAlloc, w.HANDLE, w.UINT, ctypes.c_size_t)
_sig(kernel32.GlobalFree, w.HANDLE, w.HANDLE)
_sig(kernel32.GetACP, w.UINT)
_sig(kernel32.GetOEMCP, w.UINT)
_sig(gdi32.GetEnhMetaFileBits, w.UINT, w.HANDLE, w.UINT, ctypes.c_void_p)
_sig(gdi32.GetMetaFileBitsEx, w.UINT, w.HANDLE, w.UINT, ctypes.c_void_p)


class METAFILEPICT(ctypes.Structure):
    _fields_ = [("mm", w.LONG), ("xExt", w.LONG), ("yExt", w.LONG), ("hMF", w.HANDLE)]


STD_FORMATS = {
    1: "CF_TEXT", 2: "CF_BITMAP", 3: "CF_METAFILEPICT", 4: "CF_SYLK", 5: "CF_DIF",
    6: "CF_TIFF", 7: "CF_OEMTEXT", 8: "CF_DIB", 9: "CF_PALETTE", 10: "CF_PENDATA",
    11: "CF_RIFF", 12: "CF_WAVE", 13: "CF_UNICODETEXT", 14: "CF_ENHMETAFILE",
    15: "CF_HDROP", 16: "CF_LOCALE", 17: "CF_DIBV5",
    0x80: "CF_OWNERDISPLAY", 0x81: "CF_DSPTEXT", 0x82: "CF_DSPBITMAP",
    0x83: "CF_DSPMETAFILEPICT", 0x8E: "CF_DSPENHMETAFILE",
}
ANSI = f"cp{kernel32.GetACP()}"
OEM = f"cp{kernel32.GetOEMCP()}"
TEXT_BY_NAME = {
    "HTML Format": "utf-8",
    "Rich Text Format": ANSI,
    "RTF As Text": ANSI,
    "UniformResourceLocator": ANSI,
    "UniformResourceLocatorW": "utf-16-le",
    "FileName": ANSI,
    "FileNameW": "utf-16-le",
    "CSV": ANSI,
    "XML Spreadsheet": "utf-8",
}


def fmt_name(f):
    if f in STD_FORMATS:
        return STD_FORMATS[f]
    buf = ctypes.create_unicode_buffer(512)
    if user32.GetClipboardFormatNameW(f, buf, 512):
        return buf.value
    if 0x200 <= f <= 0x2FF:
        return f"CF_PRIVATE+{f - 0x200}"
    if 0x300 <= f <= 0x3FF:
        return f"CF_GDIOBJ+{f - 0x300}"
    return f"#{f}"


def open_clipboard():
    for _ in range(40):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise OSError("OpenClipboard に失敗しました（他のアプリが使用中の可能性）")


def read_hglobal(h):
    size = kernel32.GlobalSize(h)
    if not size:
        raise OSError(f"GlobalSize 失敗 (err={ctypes.get_last_error()})")
    p = kernel32.GlobalLock(h)
    if not p:
        raise OSError(f"GlobalLock 失敗 (err={ctypes.get_last_error()})")
    try:
        return ctypes.string_at(p, size)
    finally:
        kernel32.GlobalUnlock(h)


def read_item(f):
    it = {"fmt": f, "name": fmt_name(f), "data": b"", "note": "", "info": "", "enc": None, "kind": None}
    if f in (2, 9, 0x80, 0x82) or 0x300 <= f <= 0x3FF:
        it["note"] = "GDI ハンドル / データなし（バイト列は取得できない）" + ("。CF_DIB を参照" if f in (2, 0x82) else "")
        return it
    h = user32.GetClipboardData(f)
    if not h:
        it["note"] = f"GetClipboardData 失敗 (err={ctypes.get_last_error()})"
        return it
    try:
        if f in (14, 0x8E):
            n = gdi32.GetEnhMetaFileBits(h, 0, None)
            buf = ctypes.create_string_buffer(n)
            gdi32.GetEnhMetaFileBits(h, n, buf)
            it.update(data=buf.raw, kind="emf", note="GetEnhMetaFileBits() で取得した EMF バイト列")
        elif f in (3, 0x83):
            p = kernel32.GlobalLock(h)
            if not p:
                raise OSError("GlobalLock 失敗")
            try:
                m = METAFILEPICT.from_address(p)
                mm, x, y, hmf = m.mm, m.xExt, m.yExt, m.hMF
            finally:
                kernel32.GlobalUnlock(h)
            n = gdi32.GetMetaFileBitsEx(hmf, 0, None)
            buf = ctypes.create_string_buffer(n)
            gdi32.GetMetaFileBitsEx(hmf, n, buf)
            it.update(data=buf.raw, kind="wmf", wmf_ext=(x, y),
                      note="METAFILEPICT 内の WMF バイト列（GetMetaFileBitsEx）",
                      info=f"METAFILEPICT: mm={mm} xExt={x} yExt={y}")
        else:
            it["data"] = read_hglobal(h)
    except Exception as e:  # noqa: BLE001
        it["note"] = f"読み取り失敗: {e}"
    return it


def sniff_image(b):
    if b[:4] == b"\x89PNG":
        return "png"
    if b[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if b[:4] == b"GIF8":
        return "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    if b[:2] == b"BM" and len(b) > 26:
        return "bmp"
    if b[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if b[:4] == b"\x00\x00\x01\x00" and len(b) > 22:
        return "ico"
    return None


def guess_text(name, b):
    if not b:
        return None
    if b[:3] == b"\xef\xbb\xbf":
        return "utf-8"
    if b[:2] == b"\xff\xfe":
        return "utf-16-le"
    s = b[:8192]
    if len(s) >= 4 and len(s) % 2 == 0 and s[1::2].count(0) > len(s) / 2 * 0.3:
        return "utf-16-le"
    for k in range(4):  # 末尾でマルチバイトが切れている場合を考慮
        try:
            t = s[: len(s) - k].decode("utf-8")
        except UnicodeDecodeError:
            if len(s) == len(b):
                break
            continue
        ctrl = sum(1 for c in t if ord(c) < 0x20 and c not in "\t\r\n")
        if ctrl < max(1, len(t)) * 0.05:
            return "utf-8"
        break
    if any(k in name.lower() for k in ("svg", "xml", "json", "html", "text", "rtf")):
        return "utf-8"
    return None


def add_info(it, s):
    it["info"] = f"{it['info']}\n{s}" if it["info"] else s


def analyze(it):
    f, d, name = it["fmt"], it["data"], it["name"]
    if f == 1:
        it["enc"] = ANSI
    elif f in (7, 0x81):
        it["enc"] = OEM
    elif f == 13:
        it["enc"] = "utf-16-le"
    elif name in TEXT_BY_NAME:
        it["enc"] = TEXT_BY_NAME[name]

    if f == 16 and len(d) >= 4:
        lcid = struct.unpack_from("<I", d)[0]
        add_info(it, f"LCID = {lcid} (0x{lcid:04x})")
    elif f == 15 and len(d) >= 20:
        p_files, _x, _y, _nc, wide = struct.unpack_from("<IiiII", d)
        s = d[p_files:].decode("utf-16-le" if wide else ANSI, errors="replace")
        files = [x for x in s.split("\0") if x]
        add_info(it, f"DROPFILES (fWide={wide}) {len(files)} 件:\n" + "\n".join(files))
    elif f in (8, 17) and len(d) >= 20:
        hs, wd, ht, _pl, bpp, comp = struct.unpack_from("<IiiHHI", d)
        add_info(it, f"BITMAPINFOHEADER: size={hs} {wd}x{abs(ht)} bpp={bpp} compression={comp}")
        it["kind"] = "dib"
    elif name == "Preferred DropEffect" and len(d) >= 4:
        add_info(it, f"DROPEFFECT = {struct.unpack_from('<I', d)[0]}")
    if it["kind"] == "emf" and len(d) >= 40:
        l, t, r, b = struct.unpack_from("<iiii", d, 24)
        add_info(it, f"EMF rclFrame: {(r - l) / 100:.1f} x {(b - t) / 100:.1f} mm")
    if not it["kind"]:
        sig = sniff_image(d)
        if sig:
            it["kind"] = "raw"
            add_info(it, f"signature: {sig}")
    if not it["enc"] and not it["kind"] and f != 16:
        it["enc"] = guess_text(name, d)
    # SVG はテキストとしても画像としても見せる
    if not it["kind"] and it["enc"] and looks_like_svg(name, d, it["enc"]):
        it["kind"] = "svg"


def looks_like_svg(name, d, enc):
    if "svg" in name.lower():
        return True
    try:
        head = d[:4096].decode(enc, errors="ignore").lstrip("﻿ \t\r\n").lower()
    except LookupError:
        return False
    return head.startswith("<svg") or (head.startswith("<?xml") and "<svg" in head)


def read_all():
    open_clipboard()
    try:
        items, f = [], 0
        while True:
            f = user32.EnumClipboardFormats(f)
            if not f:
                break
            items.append(read_item(f))
    finally:
        user32.CloseClipboard()
    for it in items:
        analyze(it)
    return items


def get_seq():
    return user32.GetClipboardSequenceNumber()


def set_clipboard_text(s):
    data = (s + "\0").encode("utf-16-le")
    h = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    open_clipboard()
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(13, h):
            kernel32.GlobalFree(h)
            raise OSError("SetClipboardData 失敗")
    finally:
        user32.CloseClipboard()


# ======================================================================
# 画像化（Pillow）
# ======================================================================
def dib_to_bmp(d):
    hs = struct.unpack_from("<I", d, 0)[0]
    bpp = struct.unpack_from("<H", d, 14)[0]
    comp = struct.unpack_from("<I", d, 16)[0]
    clr = struct.unpack_from("<I", d, 32)[0] if hs >= 36 else 0
    masks = {3: 12, 6: 16}.get(comp, 0) if hs == 40 else 0
    ncol = clr or ((1 << bpp) if bpp <= 8 else 0)
    off = 14 + hs + masks + ncol * 4
    return b"BM" + struct.pack("<IHHI", 14 + len(d), 0, 0, off) + d


def make_image(it):
    """PIL.Image と説明文を返す（失敗時は例外）"""
    from PIL import Image

    kind, d = it["kind"], it["data"]
    if kind == "dib":
        im = Image.open(io.BytesIO(dib_to_bmp(d)))
        im.load()
        return im, "CF_DIB → BMP (Pillow)"
    if kind == "raw":
        im = Image.open(io.BytesIO(d))
        im.load()
        return im, f"生データ ({im.format})"
    if kind in ("emf", "wmf"):
        src = d
        if kind == "wmf":  # placeable header を付ける
            x, y = it.get("wmf_ext", (0, 0))
            x, y = abs(x) or 10000, abs(y) or 10000
            k = max(1, math.ceil(max(x, y) / 32000))
            hdr = struct.pack("<IHhhhhHI", 0x9AC6CDD7, 0, 0, 0, x // k, y // k, max(1, 2540 // k), 0)
            chk = 0
            for wd in struct.unpack("<10H", hdr[:20]):
                chk ^= wd
            src = hdr + struct.pack("<H", chk) + d
        im = Image.open(io.BytesIO(src))
        mx = max(im.size) or 1
        dpi = max(10, min(96, int(72 * 2000 / mx)))
        im.load(dpi=dpi)
        return im, f"{kind.upper()} → ラスタ化 (Pillow / GDI, {dpi}dpi)"
    if kind == "svg":
        import pyvips

        svg = d.decode(it["enc"] or "utf-8", errors="replace").rstrip("\0").lstrip("﻿").encode("utf-8")
        im = pyvips.Image.svgload_buffer(svg)
        mx = max(im.width, im.height) or 1
        scale = 1.0
        if mx > 2000:  # 巨大な SVG は縮小して描画
            scale = 2000 / mx
            im = pyvips.Image.svgload_buffer(svg, scale=scale)
        pil = Image.open(io.BytesIO(im.write_to_buffer(".png")))
        pil.load()
        return pil, f"SVG → ラスタ化 (pyvips {pyvips.version(0)}.{pyvips.version(1)}.{pyvips.version(2)}, scale={scale:.2f})"
    raise ValueError("画像化できないフォーマット")


def fmt_size(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"


def hex_dump(d, end):
    lines = []
    for i in range(0, min(len(d), end), 16):
        chunk = d[i: min(i + 16, end)]
        hx = " ".join(f"{c:02x}" for c in chunk[:8])
        if len(chunk) > 8:
            hx += "  " + " ".join(f"{c:02x}" for c in chunk[8:])
        asc = "".join(chr(c) if 0x20 <= c < 0x7F else "." for c in chunk)
        lines.append(f"{i:08x}  {hx:<49} |{asc}|")
    return "\n".join(lines)


# ======================================================================
# GUI (Tkinter)
# ======================================================================
HEX_LIMIT = 64 * 1024
ENCODINGS = ["utf-8", "utf-16-le", "utf-16-be", "cp932", "euc_jp", "iso2022_jp", "cp1252", "cp437"]


class App:
    def __init__(self):
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
        self.root = root = tk.Tk()
        root.title("Clipboard Type Viewer (Tkinter)")
        # 高 DPI 対応: ピクセル指定のサイズは DPI 倍率を掛ける（tk scaling は 96dpi で 1.333）
        self.s = s = float(root.tk.call("tk", "scaling")) / (96 / 72)
        root.geometry(f"{int(1200 * s)}x{int(860 * s)}")

        fams = set(tkfont.families())
        ui = next((f for f in ("Yu Gothic UI", "Meiryo UI", "Meiryo") if f in fams), "TkDefaultFont")
        mono = next((f for f in ("BIZ UDGothic", "BIZ UDゴシック", "MS Gothic") if f in fams), "TkFixedFont")
        self.ui_font = tkfont.Font(family=ui, size=10)
        self.mono_font = tkfont.Font(family=mono, size=10)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=ui, size=10)
        style = ttk.Style()
        style.configure("Treeview", rowheight=int(self.ui_font.metrics("linespace") * 1.35))

        self.items, self.cur, self.last_seq = [], None, 0
        self.pil, self.photo, self._fit_job = None, None, None

        # --- 上部バー ---
        top = ttk.Frame(root, padding=4)
        top.pack(side="top", fill="x")
        ttk.Button(top, text="再読み込み", command=lambda: self.reload("ボタン")).pack(side="left")
        self.watch = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="クリップボードの変化を監視して自動更新", variable=self.watch).pack(side="left", padx=10)
        self.status = ttk.Label(top, foreground="gray")
        self.status.pack(side="left")

        # --- 下部ログ ---
        logf = ttk.Frame(root, padding=(4, 0, 4, 4))
        logf.pack(side="bottom", fill="x")
        lb = ttk.Frame(logf)
        lb.pack(fill="x")
        ttk.Label(lb, text="イベントログ").pack(side="left")
        ttk.Button(lb, text="ログをコピー", command=lambda: self.copy(self.text_of(self.log_txt), "ログ")).pack(side="left", padx=4)
        ttk.Button(lb, text="クリア", command=self.clear_log).pack(side="left")
        self.log_txt = self.make_text(logf, wrap="char", height=8)

        # --- 中央 ---
        pw = ttk.PanedWindow(root, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=4)
        left = ttk.Frame(pw)
        cols = ("no", "id", "name", "size", "kind")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c, t, wd in zip(cols, ("#", "ID", "Name", "Size", "種別"), (40, 110, 220, 80, 130)):
            self.tree.heading(c, text=t, anchor="w")
            self.tree.column(c, width=int(wd * s), anchor="w", stretch=(c == "name"))
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        pw.add(left, weight=2)

        self.nb = ttk.Notebook(pw)
        pw.add(self.nb, weight=3)

        # 画像タブ
        self.tab_img = ttk.Frame(self.nb)
        self.img_lbl = ttk.Label(self.tab_img, padding=4)
        self.img_lbl.pack(fill="x")
        self.canvas = tk.Canvas(self.tab_img, background="#e6e6e6", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.schedule_fit())
        self.nb.add(self.tab_img, text="画像")

        # テキストタブ
        self.tab_txt = ttk.Frame(self.nb)
        tb = ttk.Frame(self.tab_txt, padding=4)
        tb.pack(fill="x")
        ttk.Label(tb, text="エンコーディング:").pack(side="left")
        self.enc = tk.StringVar()
        self.enc_cb = ttk.Combobox(tb, textvariable=self.enc, values=ENCODINGS, width=14, state="readonly")
        self.enc_cb.pack(side="left", padx=4)
        self.enc_cb.bind("<<ComboboxSelected>>", lambda e: self.show_text())
        self.wrap = tk.BooleanVar(value=True)
        ttk.Checkbutton(tb, text="折り返し", variable=self.wrap,
                        command=lambda: self.txt.configure(wrap="char" if self.wrap.get() else "none")).pack(side="left", padx=8)
        ttk.Button(tb, text="テキストをコピー", command=lambda: self.copy(self.text_of(self.txt), "テキスト")).pack(side="left")
        self.txt_lbl = ttk.Label(tb, foreground="gray")
        self.txt_lbl.pack(side="left", padx=8)
        self.txt = self.make_text(self.tab_txt, wrap="char")
        self.nb.add(self.tab_txt, text="テキスト")

        # Hex タブ
        self.tab_hex = ttk.Frame(self.nb)
        hb = ttk.Frame(self.tab_hex, padding=4)
        hb.pack(fill="x")
        self.hex_lbl = ttk.Label(hb)
        self.hex_lbl.pack(side="left")
        self.hex_all = ttk.Button(hb, text="すべて表示", command=self.show_hex_all)
        self.hex_all.pack(side="left", padx=4)
        ttk.Button(hb, text="Hex をコピー", command=lambda: self.copy(self.text_of(self.hex), "Hex")).pack(side="left")
        self.hex = self.make_text(self.tab_hex, wrap="none")
        self.nb.add(self.tab_hex, text="Hex")

        # 情報タブ
        self.tab_info = ttk.Frame(self.nb)
        self.info = self.make_text(self.tab_info, wrap="char")
        self.nb.add(self.tab_info, text="情報")

        self.log(f"起動: Python {sys.version.split()[0]} / Tk {tk.TkVersion} / ACP {ANSI} / font {ui}, {mono}")
        root.after(100, lambda: (self.reload("起動時"), self.poll()))

    # ---------- 部品 ----------
    def make_text(self, parent, wrap, height=None):
        tk, ttk = self.tk, self.ttk
        fr = ttk.Frame(parent)
        fr.pack(fill="both", expand=True)
        t = tk.Text(fr, wrap=wrap, font=self.mono_font, undo=False, height=height or 10)
        ys = ttk.Scrollbar(fr, orient="vertical", command=t.yview)
        xs = ttk.Scrollbar(fr, orient="horizontal", command=t.xview)
        t.configure(yscrollcommand=ys.set, xscrollcommand=xs.set, state="disabled")
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        t.pack(fill="both", expand=True)
        return t

    @staticmethod
    def set_text(t, s):
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", s)
        t.configure(state="disabled")

    @staticmethod
    def text_of(t):
        return t.get("1.0", "end-1c")

    def log(self, msg):
        t = self.log_txt
        t.configure(state="normal")
        t.insert("end", f"[{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d}] {msg}\n")
        t.see("end")
        t.configure(state="disabled")

    def clear_log(self):
        self.set_text(self.log_txt, "")

    def copy(self, s, what):
        if not s:
            self.log(f"{what}: 空なのでコピーしません")
            return
        try:
            set_clipboard_text(s)
            self.last_seq = get_seq()  # 自分のコピーでは自動更新しない
            self.log(f"{what} をクリップボードにコピー ({len(s)} chars)")
        except Exception as e:  # noqa: BLE001
            self.log(f"{what} のコピー失敗: {e}")

    # ---------- 読み込み ----------
    def reload(self, reason):
        self.log(f"--- 読み込み ({reason})")
        seq = get_seq()
        try:
            items = read_all()
        except Exception as e:  # noqa: BLE001
            self.log(f"読み取り失敗: {e}")
            return
        self.last_seq = seq
        prev = self.cur["fmt"] if self.cur else None
        self.items, self.cur = items, None
        self.tree.delete(*self.tree.get_children())
        sel = 0
        for i, it in enumerate(items):
            kind = " ".join(x for x in (it["kind"], f"text:{it['enc']}" if it["enc"] else None,
                                        None if it["data"] else "データなし") if x)
            self.tree.insert("", "end", iid=str(i), values=(i + 1, f"{it['fmt']} (0x{it['fmt']:04x})",
                                                             it["name"], fmt_size(len(it["data"])), kind))
            if it["fmt"] == prev:
                sel = i
        self.status.configure(text=f"seq={seq} / {len(items)} フォーマット / {time.strftime('%H:%M:%S')} 更新")
        self.log(f"フォーマット数: {len(items)} : {', '.join(it['name'] for it in items)}")
        if items:
            self.tree.selection_set(str(sel))
            self.tree.see(str(sel))
        else:
            self.set_text(self.info, "クリップボードは空です。")
            for t in (self.txt, self.hex):
                self.set_text(t, "")
            self.set_image(None, "")
            self.nb.select(self.tab_info)

    def poll(self):
        try:
            if self.watch.get():
                s = get_seq()
                if s != self.last_seq:
                    self.reload(f"変化検出 seq={s}")
        except Exception as e:  # noqa: BLE001
            self.log(f"poll エラー: {e}")
        self.root.after(1000, self.poll)

    # ---------- 表示 ----------
    def on_select(self, _e=None):
        sel = self.tree.selection()
        if sel:
            try:
                self.show_item(int(sel[0]))
            except Exception as e:  # noqa: BLE001
                self.log(f"表示エラー: {type(e).__name__}: {e}")

    def show_item(self, i):
        it = self.items[i]
        self.cur = it
        info = [
            f"Name : {it['name']}",
            f"ID   : {it['fmt']} (0x{it['fmt']:04x})",
            f"Size : {len(it['data']):,} bytes",
            f"種別 : {it['kind'] or '-'} / テキスト: {it['enc'] or '-'}",
        ]
        if it["note"]:
            info.append(f"備考 : {it['note']}")
        if it["info"]:
            info += ["", it["info"]]
        self.set_text(self.info, "\n".join(info))

        has_img = False
        if it["kind"]:
            try:
                im, note = make_image(it)
                self.set_image(im, f"{im.width} × {im.height} px — {note}")
                has_img = True
                self.log(f"画像プレビュー OK: {it['name']} {im.width}x{im.height}")
            except Exception as e:  # noqa: BLE001
                self.set_image(None, f"画像プレビュー失敗: {type(e).__name__}: {e}")
                self.log(f"画像プレビュー失敗: {it['name']} {e}")
        else:
            self.set_image(None, "(画像としてプレビューできないフォーマット)")

        if it["enc"]:
            enc = it["enc"]
            if enc not in self.enc_cb["values"]:
                self.enc_cb["values"] = (*self.enc_cb["values"], enc)
            self.enc.set(enc)
            self.show_text()
        else:
            self.enc.set("")
            self.set_text(self.txt, "(テキストとして表示できないフォーマットと判定。エンコーディングを選ぶと強制的にデコードします)")
            self.txt_lbl.configure(text="")

        self.show_hex(HEX_LIMIT)
        self.nb.select(self.tab_img if has_img else self.tab_txt if it["enc"]
                       else self.tab_hex if it["data"] else self.tab_info)

    def show_text(self):
        it, enc = self.cur, self.enc.get()
        if not it or not enc:
            return
        s = it["data"].decode(enc, errors="replace").rstrip("\0").replace("\0", "␀")
        self.set_text(self.txt, s)
        self.txt_lbl.configure(text=f"{len(s):,} 文字")

    def show_hex(self, limit):
        d = self.cur["data"]
        self.set_text(self.hex, hex_dump(d, limit))
        shown = min(len(d), limit)
        self.hex_lbl.configure(text=f"{shown:,} / {len(d):,} bytes 表示中")
        if shown < len(d):
            self.hex_all.pack(side="left", padx=4, after=self.hex_lbl)
        else:
            self.hex_all.pack_forget()

    def show_hex_all(self):
        from tkinter import messagebox

        if len(self.cur["data"]) > 4 * 1024 * 1024 and not messagebox.askyesno("Hex", "大きいデータです。すべて表示しますか？"):
            return
        self.show_hex(len(self.cur["data"]))

    def set_image(self, im, label):
        self.pil = im
        self.img_lbl.configure(text=label)
        self.fit_image()

    def schedule_fit(self):
        if self._fit_job:
            self.root.after_cancel(self._fit_job)
        self._fit_job = self.root.after(80, self.fit_image)

    def fit_image(self):
        from PIL import Image, ImageTk

        self._fit_job = None
        self.canvas.delete("all")
        self.photo = None
        if self.pil is None:
            return
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        im = self.pil
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        scale = min(1.0, cw / im.width, ch / im.height)
        if scale < 1.0:
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(im)
        self.canvas.create_image(cw // 2, ch // 2, image=self.photo, anchor="center")

    def run(self):
        self.root.mainloop()


def dump():
    sys.stdout.reconfigure(encoding="utf-8")
    items = read_all()
    print(f"seq={get_seq()} ANSI={ANSI} formats={len(items)}")
    for i, it in enumerate(items):
        print(f"[{i + 1:2}] {it['fmt']:5} 0x{it['fmt']:04x}  {it['name']:<32} {len(it['data']):>9} B  "
              f"enc={it['enc'] or '-'} kind={it['kind'] or '-'} {it['note']}")
        if it["info"]:
            print("      " + it["info"].replace("\n", "\n      "))
        if it["kind"]:
            try:
                im, note = make_image(it)
                print(f"      image: {im.width}x{im.height} {note}")
            except Exception as e:  # noqa: BLE001
                print(f"      image: 失敗 {e}")


if __name__ == "__main__":
    if "--dump" in sys.argv:
        dump()
    else:
        App().run()
