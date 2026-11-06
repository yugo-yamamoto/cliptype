# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = [
#   "pywebview>=5",
#   "pillow",
# ]
# ///
"""Clipboard Type Viewer (Windows / pywebview)

Windows のクリップボードに入っている全フォーマット（EMF, CF_DIB, HTML Format など）を
列挙し、Hex ダンプ・テキスト・画像でプレビューする。

起動:  uv run cliptype.py          (Windows 側の uv で実行すること)
       uv run cliptype.py --dump   (GUI なしで一覧を標準出力に出す)
       uv run cliptype.py --debug  (WebView の DevTools を有効化)
"""
import base64
import ctypes
import io
import math
import struct
import sys
import time

if sys.platform != "win32":
    sys.exit("Windows 側の uv で実行してください（WSL からなら: uv.exe run cliptype.py）")

from ctypes import wintypes as w

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
CP_TO_LABEL = {932: "shift_jis", 65001: "utf-8", 936: "gbk", 949: "euc-kr", 950: "big5",
               1250: "windows-1250", 1251: "windows-1251", 1252: "windows-1252"}
ANSI = CP_TO_LABEL.get(kernel32.GetACP(), "windows-1252")
OEM = CP_TO_LABEL.get(kernel32.GetOEMCP(), ANSI)

# 名前で分かるテキスト系フォーマット -> エンコーディング
TEXT_BY_NAME = {
    "HTML Format": "utf-8",
    "Rich Text Format": ANSI,
    "RTF As Text": ANSI,
    "UniformResourceLocator": ANSI,
    "UniformResourceLocatorW": "utf-16le",
    "FileName": ANSI,
    "FileNameW": "utf-16le",
    "CSV": ANSI,
    "XML Spreadsheet": "utf-8",
}


def fmt_name(f):
    if f in STD_FORMATS:
        return STD_FORMATS[f]
    buf = ctypes.create_unicode_buffer(512)
    n = user32.GetClipboardFormatNameW(f, buf, 512)
    if n:
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
    """1 フォーマット分を読む。clipboard は open 済みであること。"""
    item = {"fmt": f, "name": fmt_name(f), "data": b"", "note": "", "info": "", "text_enc": None, "preview_kind": None}
    if f in (2, 9, 0x80, 0x82) or 0x300 <= f <= 0x3FF:
        item["note"] = "GDI ハンドル / データなし（バイト列は取得できない）"
        if f in (2, 0x82):
            item["note"] += "。CF_DIB を参照"
        return item
    h = user32.GetClipboardData(f)
    if not h:
        item["note"] = f"GetClipboardData 失敗 (err={ctypes.get_last_error()})"
        return item
    try:
        if f in (14, 0x8E):  # EMF
            n = gdi32.GetEnhMetaFileBits(h, 0, None)
            buf = ctypes.create_string_buffer(n)
            gdi32.GetEnhMetaFileBits(h, n, buf)
            item["data"] = buf.raw
            item["note"] = "GetEnhMetaFileBits() で取得した EMF バイト列"
            item["preview_kind"] = "emf"
        elif f in (3, 0x83):  # METAFILEPICT -> WMF
            p = kernel32.GlobalLock(h)
            if not p:
                raise OSError("GlobalLock 失敗")
            try:
                mfp = METAFILEPICT.from_address(p)
                mm, x, y, hmf = mfp.mm, mfp.xExt, mfp.yExt, mfp.hMF
            finally:
                kernel32.GlobalUnlock(h)
            n = gdi32.GetMetaFileBitsEx(hmf, 0, None)
            buf = ctypes.create_string_buffer(n)
            gdi32.GetMetaFileBitsEx(hmf, n, buf)
            item["data"] = buf.raw
            item["note"] = "METAFILEPICT 内の WMF バイト列（GetMetaFileBitsEx）"
            item["info"] = f"METAFILEPICT: mm={mm} xExt={x} yExt={y}"
            item["wmf_ext"] = (x, y)
            item["preview_kind"] = "wmf"
        else:
            item["data"] = read_hglobal(h)
    except Exception as e:  # noqa: BLE001
        item["note"] = f"読み取り失敗: {e}"
        return item

    d = item["data"]
    name = item["name"]
    # テキスト判定・付加情報
    if f == 1:
        item["text_enc"] = ANSI
    elif f in (7, 0x81):
        item["text_enc"] = OEM
    elif f == 13:
        item["text_enc"] = "utf-16le"
    elif name in TEXT_BY_NAME:
        item["text_enc"] = TEXT_BY_NAME[name]
    elif f == 16 and len(d) >= 4:
        item["info"] = f"LCID = {struct.unpack_from('<I', d)[0]} (0x{struct.unpack_from('<I', d)[0]:04x})"
    elif f == 15 and len(d) >= 20:
        p_files, _x, _y, _nc, wide = struct.unpack_from("<IiiII", d)
        raw = d[p_files:]
        s = raw.decode("utf-16le" if wide else ANSI, errors="replace")
        files = [x for x in s.split("\0") if x]
        item["info"] = f"DROPFILES (fWide={wide}) {len(files)} 件:\n" + "\n".join(files)
    elif f in (8, 17) and len(d) >= 20:
        hs, wd, ht, _pl, bpp, comp = struct.unpack_from("<IiiHHI", d)
        item["info"] = f"BITMAPINFOHEADER: size={hs} {wd}x{abs(ht)} bpp={bpp} compression={comp}"
        item["preview_kind"] = "dib"
    elif name == "Preferred DropEffect" and len(d) >= 4:
        item["info"] = f"DROPEFFECT = {struct.unpack_from('<I', d)[0]}"
    if f in (14, 0x8E) and len(d) >= 40:
        l, t, r, b = struct.unpack_from("<iiii", d, 24)  # rclFrame (0.01mm)
        item["info"] = f"EMF rclFrame: {(r - l) / 100:.1f} x {(b - t) / 100:.1f} mm"
    return item


def read_all():
    open_clipboard()
    try:
        items, f = [], 0
        while True:
            f = user32.EnumClipboardFormats(f)
            if not f:
                break
            items.append(read_item(f))
        return items
    finally:
        user32.CloseClipboard()


def set_clipboard_text(s):
    data = (s + "\0").encode("utf-16le")
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


# ---------------- preview (PNG 変換) ----------------
def dib_to_bmp(d):
    hs = struct.unpack_from("<I", d, 0)[0]
    bpp = struct.unpack_from("<H", d, 14)[0]
    comp = struct.unpack_from("<I", d, 16)[0]
    clr = struct.unpack_from("<I", d, 32)[0] if hs >= 36 else 0
    masks = {3: 12, 6: 16}.get(comp, 0) if hs == 40 else 0
    ncol = clr or ((1 << bpp) if bpp <= 8 else 0)
    off = 14 + hs + masks + ncol * 4
    return b"BM" + struct.pack("<IHHI", 14 + len(d), 0, 0, off) + d


def to_png(im):
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def make_preview(item):
    from PIL import Image

    kind, d = item["preview_kind"], item["data"]
    if kind == "dib":
        bmp = dib_to_bmp(d)
        try:
            im = Image.open(io.BytesIO(bmp))
            im.load()
            return "image/png", to_png(im), "CF_DIB → BMP → PNG (Pillow)"
        except Exception as e:  # noqa: BLE001
            return "image/bmp", bmp, f"Pillow 変換失敗のため BMP のまま表示 ({e})"
    if kind in ("emf", "wmf"):
        src = d
        if kind == "wmf":  # placeable header を付ける
            x, y = item.get("wmf_ext", (0, 0))
            x, y = abs(x) or 10000, abs(y) or 10000  # HIMETRIC (0.01mm)
            k = max(1, math.ceil(max(x, y) / 32000))
            hdr = struct.pack("<IHhhhhHI", 0x9AC6CDD7, 0, 0, 0, x // k, y // k, max(1, 2540 // k), 0)
            words = struct.unpack("<10H", hdr[:20])
            chk = 0
            for wd in words:
                chk ^= wd
            src = hdr + struct.pack("<H", chk) + d
        im = Image.open(io.BytesIO(src))
        mx = max(im.size) or 1
        dpi = max(10, min(96, int(72 * 2000 / mx)))
        im.load(dpi=dpi)
        return "image/png", to_png(im), f"{kind.upper()} → PNG (Pillow / GDI, {dpi}dpi)"
    return None


# ---------------- pywebview API ----------------
class Api:
    def __init__(self):
        self._items = []
        self._previews = {}

    def read(self):
        seq = user32.GetClipboardSequenceNumber()
        try:
            self._items = read_all()
        except Exception as e:  # noqa: BLE001
            return {"error": str(e), "seq": seq, "items": []}
        self._previews = {}
        return {
            "seq": seq,
            "ansi": ANSI,
            "items": [
                {k: v for k, v in it.items() if k not in ("data", "wmf_ext")} | {"size": len(it["data"])}
                for it in self._items
            ],
        }

    def data(self, i):
        return base64.b64encode(self._items[i]["data"]).decode("ascii")

    def preview(self, i):
        if i not in self._previews:
            try:
                r = make_preview(self._items[i])
                self._previews[i] = (
                    {"mime": r[0], "b64": base64.b64encode(r[1]).decode("ascii"), "note": r[2]} if r else None
                )
            except Exception as e:  # noqa: BLE001
                self._previews[i] = {"error": f"{type(e).__name__}: {e}"}
        return self._previews[i]

    def seq(self):
        return user32.GetClipboardSequenceNumber()

    def copy_text(self, text):
        set_clipboard_text(text)
        return user32.GetClipboardSequenceNumber()


HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>Clipboard Type Viewer</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #fff; --fg: #1d2330; --muted: #6b7385; --border: #dde1e8;
    --accent: #2f6fde; --accent-fg: #fff; --code-bg: #f0f2f5; --off: #9aa3b5; --ascii: #2b7a3d; --err: #c0392b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #15181e; --panel: #1d2129; --fg: #e3e7ef; --muted: #9099ab; --border: #313744;
      --accent: #5b8ef0; --accent-fg: #fff; --code-bg: #12151a; --off: #687186; --ascii: #79c48a; --err: #ef7a6c;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg);
    font-family: "游ゴシック", "Yu Gothic", "メイリオ", Meiryo, "BIZ UDゴシック", sans-serif; font-size: 14px; line-height: 1.5; }
  main { max-width: 1200px; margin: 0 auto; padding: 16px 16px 40px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 12px; }
  .bar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 14px; }
  button { font: inherit; border: 1px solid var(--border); background: var(--panel); color: var(--fg);
    padding: 5px 12px; border-radius: 6px; cursor: pointer; }
  button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
  .hint { color: var(--muted); font-size: 13px; }
  .mono { font-family: Consolas, "BIZ UDゴシック", monospace; }
  .summary { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; }
  .summary table { border-collapse: collapse; width: 100%; }
  .summary th, .summary td { text-align: left; padding: 3px 8px; border-bottom: 1px solid var(--border); font-size: 13px; vertical-align: top; }
  .summary th { color: var(--muted); font-weight: normal; }
  .summary tr:last-child td { border-bottom: none; }
  .summary a { color: var(--accent); text-decoration: none; }
  .tag { font-size: 11px; border-radius: 8px; padding: 0 6px; margin-right: 4px; border: 1px solid var(--border); color: var(--muted); }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 14px; overflow: hidden; }
  .card-head { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: baseline; padding: 10px 14px; border-bottom: 1px solid var(--border); }
  .card-head .t { font-family: Consolas, "BIZ UDゴシック", monospace; font-weight: bold; font-size: 15px; word-break: break-all; }
  .badge { font-size: 12px; color: var(--muted); border: 1px solid var(--border); border-radius: 10px; padding: 0 8px; }
  .note { padding: 6px 14px 0; font-size: 13px; color: var(--muted); }
  .info { margin: 8px 14px 0; }
  .tabs { display: flex; gap: 4px; padding: 8px 14px 0; }
  .tabs button { border-radius: 6px 6px 0 0; border-bottom: none; padding: 3px 12px; font-size: 13px; }
  .tabs button.active { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
  .pane { display: none; padding: 10px 14px 14px; }
  .pane.active { display: block; }
  pre { margin: 0; background: var(--code-bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px;
    max-height: 420px; overflow: auto; font-size: 12.5px; font-family: Consolas, "BIZ UDゴシック", monospace; white-space: pre; }
  pre.wrap { white-space: pre-wrap; word-break: break-all; }
  .hex .off { color: var(--off); } .hex .asc { color: var(--ascii); }
  .pane-tools { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; font-size: 13px; }
  select { font: inherit; }
  .imgbox { background: repeating-conic-gradient(#ccc 0% 25%, #fff 0% 50%) 50% / 16px 16px; border: 1px solid var(--border);
    border-radius: 6px; padding: 8px; display: inline-block; max-width: 100%; }
  .imgbox img { max-width: 100%; max-height: 520px; display: block; }
  .empty { color: var(--muted); padding: 30px; text-align: center; background: var(--panel); border: 1px dashed var(--border); border-radius: 8px; }
  .err { color: var(--err); }
  .log-wrap { margin-top: 24px; }
  .log-head { display: flex; gap: 8px; align-items: center; margin-bottom: 6px; }
  .log-head h2 { font-size: 15px; margin: 0; flex: 1; }
  #log { height: 200px; white-space: pre-wrap; word-break: break-all; max-height: none; }
</style>
</head>
<body>
<main>
  <h1>Clipboard Type Viewer</h1>
  <p class="sub">Windows クリップボードの全フォーマット（EnumClipboardFormats）を一覧し、Hex / テキスト / 画像でプレビューします。</p>
  <div class="bar">
    <button class="primary" id="btnRead">再読み込み</button>
    <label class="hint"><input type="checkbox" id="chkWatch" checked> クリップボードの変化を監視して自動更新</label>
    <span class="hint" id="status"></span>
  </div>
  <div id="summary"></div>
  <div id="cards"><div class="empty">読み込み中…</div></div>
  <div class="log-wrap">
    <div class="log-head">
      <h2>イベントログ</h2>
      <button id="btnCopyLog">ログをコピー</button>
      <button id="btnClearLog">クリア</button>
    </div>
    <pre id="log"></pre>
  </div>
</main>
<script>
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const logEl = $('log');
  const HEX_CHUNK = 16 * 1024;
  let api = null, lastSeq = null, busy = false;

  function log(...args) {
    const ts = new Date().toTimeString().slice(0, 8) + '.' + String(new Date().getMilliseconds()).padStart(3, '0');
    const msg = args.map(a => typeof a === 'string' ? a : JSON.stringify(a)).join(' ');
    logEl.textContent += `[${ts}] ${msg}\n`;
    logEl.scrollTop = logEl.scrollHeight;
  }
  const esc = (s) => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmtSize = (n) => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(2) + ' MB';
  function b64ToBytes(b64) {
    if (Uint8Array.fromBase64) return Uint8Array.fromBase64(b64);
    const bin = atob(b64); const u = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    return u;
  }

  async function copyText(text, what) {
    try { lastSeq = await api.copy_text(text); log(`${what} をクリップボードにコピー (${text.length} chars)`); }
    catch (e) { log(`${what} のコピー失敗: ${e}`); }
  }
  $('btnClearLog').onclick = () => { logEl.textContent = ''; };
  $('btnCopyLog').onclick = () => copyText(logEl.textContent, 'ログ');

  function hexDump(bytes, start, end) {
    const lines = [];
    for (let i = start; i < end; i += 16) {
      let hex = '', asc = '';
      for (let j = 0; j < 16; j++) {
        if (i + j < end) {
          const b = bytes[i + j];
          hex += b.toString(16).padStart(2, '0') + ' ';
          asc += (b >= 0x20 && b < 0x7f) ? String.fromCharCode(b) : '.';
        } else hex += '   ';
        if (j === 7) hex += ' ';
      }
      lines.push(`<span class="off">${i.toString(16).padStart(8, '0')}</span>  ${hex} <span class="asc">|${esc(asc)}|</span>`);
    }
    return lines.join('\n');
  }

  function sniffImage(b) {
    const m = (sig, off = 0) => sig.every((v, i) => b[off + i] === v);
    if (m([0x89, 0x50, 0x4e, 0x47])) return 'image/png';
    if (m([0xff, 0xd8, 0xff])) return 'image/jpeg';
    if (m([0x47, 0x49, 0x46, 0x38])) return 'image/gif';
    if (m([0x52, 0x49, 0x46, 0x46]) && m([0x57, 0x45, 0x42, 0x50], 8)) return 'image/webp';
    if (m([0x42, 0x4d])) return 'image/bmp';
    if (m([0x49, 0x49, 0x2a, 0x00]) || m([0x4d, 0x4d, 0x00, 0x2a])) return 'image/tiff';
    if (m([0x66, 0x74, 0x79, 0x70], 4) && (m([0x61, 0x76, 0x69, 0x66], 8) || m([0x61, 0x76, 0x69, 0x73], 8))) return 'image/avif';
    return null;
  }

  // Python からエンコーディング指定がない場合の推定
  function guessText(name, b) {
    if (b.length === 0) return null;
    if (b[0] === 0xef && b[1] === 0xbb && b[2] === 0xbf) return 'utf-8';
    if (b[0] === 0xff && b[1] === 0xfe) return 'utf-16le';
    const sample = b.subarray(0, 8192);
    if (sample.length >= 4 && sample.length % 2 === 0) {
      let z = 0; for (let i = 1; i < sample.length; i += 2) if (sample[i] === 0) z++;
      if (z / (sample.length / 2) > 0.3) return 'utf-16le';
    }
    try {
      const s = new TextDecoder('utf-8', { fatal: true }).decode(sample);
      let ctrl = 0;
      for (const ch of s) { const c = ch.codePointAt(0); if (c < 0x20 && c !== 9 && c !== 10 && c !== 13) ctrl++; }
      if (ctrl / Math.max(1, s.length) < 0.05) return 'utf-8';
    } catch { /* not utf-8 */ }
    if (/svg|xml|json|html|text|csv|rtf/i.test(name)) return 'utf-8';
    return null;
  }

  async function load(reason) {
    if (busy || !api) return;
    busy = true;
    try {
      log(`--- 読み込み (${reason})`);
      const res = await api.read();
      lastSeq = res.seq;
      $('status').textContent = `seq=${res.seq} / ${new Date().toLocaleTimeString()} 更新`;
      if (res.error) { log('エラー: ' + res.error); $('cards').innerHTML = `<div class="empty err">${esc(res.error)}</div>`; $('summary').innerHTML = ''; return; }
      log(`フォーマット数: ${res.items.length} (ANSI=${res.ansi})`);
      render(res.items);
      for (let i = 0; i < res.items.length; i++) await fillCard(res.items[i], i);
      log('読み込み完了');
    } catch (e) {
      log('load 失敗: ' + e);
    } finally { busy = false; }
  }

  function render(items) {
    $('cards').innerHTML = '';
    if (!items.length) {
      $('summary').innerHTML = '';
      $('cards').innerHTML = '<div class="empty">クリップボードは空です。</div>';
      return;
    }
    const rows = items.map((it, i) => `<tr>
      <td>${i + 1}</td><td class="mono">${it.fmt} (0x${it.fmt.toString(16).padStart(4, '0')})</td>
      <td class="mono"><a href="#card-${i}">${esc(it.name)}</a></td><td>${fmtSize(it.size)}</td>
      <td>${it.text_enc ? `<span class="tag">text:${esc(it.text_enc)}</span>` : ''}${it.preview_kind ? `<span class="tag">${esc(it.preview_kind)}</span>` : ''}${esc(it.note || '')}</td></tr>`).join('');
    $('summary').innerHTML = `<div class="summary"><table><thead><tr><th>#</th><th>format id</th><th>name</th><th>size</th><th>備考</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    items.forEach((it, i) => {
      const card = document.createElement('div');
      card.className = 'card'; card.id = 'card-' + i;
      card.innerHTML = `<div class="card-head"><span class="t">${esc(it.name)}</span>
        <span class="badge">id ${it.fmt} (0x${it.fmt.toString(16).padStart(4, '0')})</span>
        <span class="badge">${fmtSize(it.size)} (${it.size.toLocaleString()} bytes)</span></div>
        ${it.note ? `<div class="note">${esc(it.note)}</div>` : ''}
        ${it.info ? `<pre class="info wrap">${esc(it.info)}</pre>` : ''}
        <div class="body hint" style="padding:10px 14px">読み込み中…</div>`;
      $('cards').appendChild(card);
    });
  }

  async function fillCard(it, idx) {
    const card = $('card-' + idx);
    const body = card.querySelector('.body');
    const bytes = it.size ? b64ToBytes(await api.data(idx)) : new Uint8Array(0);
    body.remove();
    if (!it.size) { card.insertAdjacentHTML('beforeend', '<div class="note" style="padding-bottom:10px">(データなし)</div>'); return; }

    const tabs = document.createElement('div'); tabs.className = 'tabs'; card.appendChild(tabs);
    const panes = [], btns = [];
    const addTab = (label, pane) => {
      const b = document.createElement('button'); b.textContent = label;
      b.onclick = () => { btns.forEach(x => x.classList.remove('active')); panes.forEach(p => p.classList.remove('active')); b.classList.add('active'); pane.classList.add('active'); };
      tabs.appendChild(b); card.appendChild(pane); panes.push(pane); btns.push(b);
    };

    // ---- 画像 ----
    let img = null;
    if (it.preview_kind) {
      const pv = await api.preview(idx);
      if (pv && pv.b64) img = { mime: pv.mime, bytes: b64ToBytes(pv.b64), note: pv.note };
      else if (pv && pv.error) img = { error: pv.error };
      log(`preview #${idx + 1} ${it.name}: ${pv ? (pv.error || pv.note) : 'なし'}`);
    } else {
      const sniffed = sniffImage(bytes) || (/svg/i.test(it.name) ? 'image/svg+xml' : null);
      if (sniffed) img = { mime: sniffed, bytes, note: `生データをそのまま表示 (${sniffed})` };
    }
    if (img) {
      const p = document.createElement('div'); p.className = 'pane';
      if (img.error) {
        p.innerHTML = `<div class="err">プレビュー生成に失敗しました: ${esc(img.error)}</div>`;
      } else {
        const url = URL.createObjectURL(new Blob([img.bytes], { type: img.mime }));
        const box = document.createElement('div'); box.className = 'imgbox';
        const el = document.createElement('img');
        const info = document.createElement('div'); info.className = 'hint'; info.style.marginTop = '6px';
        el.onload = () => { info.textContent = `${el.naturalWidth} × ${el.naturalHeight} px — ${img.note}`; };
        el.onerror = () => { box.remove(); info.innerHTML = `<span class="err">この WebView では ${esc(img.mime)} を表示できません。</span>`; log(`画像表示失敗: ${it.name} ${img.mime}`); };
        el.src = url; box.appendChild(el); p.append(box, info);
      }
      addTab('画像', p);
    }

    // ---- テキスト ----
    const enc = it.text_enc || guessText(it.name, bytes);
    if (enc) {
      const p = document.createElement('div'); p.className = 'pane';
      const tools = document.createElement('div'); tools.className = 'pane-tools';
      const sel = document.createElement('select');
      const encs = ['utf-8', 'utf-16le', 'utf-16be', 'shift_jis', 'euc-jp', 'iso-2022-jp', 'windows-1252'];
      if (!encs.includes(enc)) encs.unshift(enc);
      encs.forEach(e => { const o = document.createElement('option'); o.value = o.textContent = e; sel.appendChild(o); });
      sel.value = enc;
      const wrapLbl = document.createElement('label'); wrapLbl.innerHTML = '<input type="checkbox" checked> 折り返し';
      const cbtn = document.createElement('button'); cbtn.textContent = 'テキストをコピー';
      const stat = document.createElement('span'); stat.className = 'hint';
      tools.append('エンコーディング: ', sel, wrapLbl, cbtn, stat);
      const pre = document.createElement('pre'); pre.className = 'wrap';
      const draw = () => {
        try { const s = new TextDecoder(sel.value).decode(bytes).replace(/\0+$/, ''); pre.textContent = s; stat.textContent = `${[...s].length.toLocaleString()} 文字`; }
        catch (e) { pre.textContent = 'デコード失敗: ' + e; }
      };
      sel.onchange = draw;
      wrapLbl.querySelector('input').onchange = (ev) => pre.classList.toggle('wrap', ev.target.checked);
      cbtn.onclick = () => copyText(pre.textContent, `テキスト(${it.name})`);
      draw(); p.append(tools, pre);
      addTab('テキスト', p);
    }

    // ---- Hex ----
    {
      const p = document.createElement('div'); p.className = 'pane';
      const tools = document.createElement('div'); tools.className = 'pane-tools';
      const stat = document.createElement('span'); stat.className = 'hint';
      const more = document.createElement('button'); more.textContent = 'もっと表示';
      const all = document.createElement('button'); all.textContent = 'すべて表示';
      const cbtn = document.createElement('button'); cbtn.textContent = 'Hex をコピー';
      tools.append(stat, more, all, cbtn);
      const pre = document.createElement('pre'); pre.className = 'hex';
      let shown = 0;
      const showUpTo = (n) => {
        const end = Math.min(bytes.length, n);
        if (end > shown) { pre.insertAdjacentHTML('beforeend', (shown ? '\n' : '') + hexDump(bytes, shown, end)); shown = end; }
        stat.textContent = `${shown.toLocaleString()} / ${bytes.length.toLocaleString()} bytes 表示中`;
        more.style.display = all.style.display = shown < bytes.length ? '' : 'none';
      };
      more.onclick = () => showUpTo(shown + HEX_CHUNK);
      all.onclick = () => { if (bytes.length > 4194304 && !confirm('大きいデータです。すべて表示しますか？')) return; showUpTo(bytes.length); };
      cbtn.onclick = () => copyText(pre.textContent, `Hex(${it.name})`);
      showUpTo(HEX_CHUNK); p.append(tools, pre);
      addTab('Hex', p);
    }
    btns[0].click();
  }

  $('btnRead').onclick = () => load('ボタン');

  async function poll() {
    if (api && $('chkWatch').checked && !busy) {
      try { const s = await api.seq(); if (lastSeq !== null && s !== lastSeq) await load(`変化検出 seq=${s}`); }
      catch (e) { log('seq 取得失敗: ' + e); }
    }
    setTimeout(poll, 1000);
  }

  function init() {
    if (api) return;
    api = window.pywebview.api;
    log('起動: UA=' + navigator.userAgent);
    load('起動時').then(poll);
  }
  if (window.pywebview && window.pywebview.api) init();
  else window.addEventListener('pywebviewready', init);
})();
</script>
</body>
</html>
"""


def dump():
    api = Api()
    res = api.read()
    if res.get("error"):
        print("ERROR:", res["error"])
        return
    print(f"seq={res['seq']} ANSI={ANSI} formats={len(res['items'])}")
    for i, it in enumerate(res["items"]):
        print(f"[{i + 1:2}] {it['fmt']:5} 0x{it['fmt']:04x}  {it['name']:<32} {it['size']:>9} B  "
              f"text={it['text_enc'] or '-'}  {it['note']}")
        if it["info"]:
            print("      " + it["info"].replace("\n", "\n      "))
        if it["preview_kind"]:
            pv = api.preview(i)
            print("      preview:", pv.get("error") or f"{pv['mime']} {len(pv['b64'])} b64chars {pv['note']}")


def main():
    if "--dump" in sys.argv:
        sys.stdout.reconfigure(encoding="utf-8")
        dump()
        return
    import webview

    webview.create_window("Clipboard Type Viewer", html=HTML, js_api=Api(), width=1200, height=900)
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
