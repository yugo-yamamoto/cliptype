#!/usr/bin/env rust-script
//! Clipboard Type Viewer (Windows / Rust + GPUI)
//!
//! Windows のクリップボードに入っている全フォーマット（EMF, CF_DIB, HTML Format など）を
//! 列挙し、Hex ダンプ・テキスト・画像でプレビューする。
//!
//! 起動:  rust-script cliptype_gpui.rs          (Windows 側の rust-script で実行すること)
//!        rust-script cliptype_gpui.rs --dump   (GUI なしで一覧を標準出力に出す)
//!
//! 必要なもの: Rust (MSVC), Visual Studio Build Tools (C++), Windows SDK (fxc.exe)
//!
//! ```cargo
//! [package]
//! edition = "2024"
//!
//! [dependencies]
//! gpui = "0.2.2"
//! resvg = "0.45"   # GPUI 内部と同じ版（SVG を自前で PNG 化するため）
//! tiny-skia = "0.11"
//! ```

use std::ffi::c_void;
use std::sync::Arc;
use std::time::Duration;

use gpui::{
    App, Application, Bounds, ClipboardItem, Context, Image, ImageFormat, ObjectFit, ScrollStrategy,
    SharedString, TitlebarOptions, UniformListScrollHandle, Window, WindowBounds, WindowOptions,
    div, img, prelude::*, px, rgb, size, uniform_list,
};

// ======================================================================
// Win32 API（必要な分だけ自前で宣言する）
// ======================================================================
type Handle = isize;

#[repr(C)]
#[derive(Clone, Copy)]
struct MetafilePict {
    mm: i32,
    x_ext: i32,
    y_ext: i32,
    h_mf: Handle,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct BitmapInfoHeader {
    size: u32,
    width: i32,
    height: i32,
    planes: u16,
    bit_count: u16,
    compression: u32,
    size_image: u32,
    x_ppm: i32,
    y_ppm: i32,
    clr_used: u32,
    clr_important: u32,
}

#[repr(C)]
struct Rect {
    left: i32,
    top: i32,
    right: i32,
    bottom: i32,
}

#[repr(C)]
#[derive(Default)]
struct SystemTime {
    year: u16,
    month: u16,
    dow: u16,
    day: u16,
    hour: u16,
    minute: u16,
    second: u16,
    ms: u16,
}

#[link(name = "user32")]
unsafe extern "system" {
    fn OpenClipboard(hwnd: Handle) -> i32;
    fn CloseClipboard() -> i32;
    fn EnumClipboardFormats(f: u32) -> u32;
    fn GetClipboardData(f: u32) -> Handle;
    fn GetClipboardFormatNameW(f: u32, buf: *mut u16, n: i32) -> i32;
    fn GetClipboardSequenceNumber() -> u32;
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn GlobalSize(h: Handle) -> usize;
    fn GlobalLock(h: Handle) -> *mut c_void;
    fn GlobalUnlock(h: Handle) -> i32;
    fn GetLastError() -> u32;
    fn GetACP() -> u32;
    fn GetOEMCP() -> u32;
    fn MultiByteToWideChar(cp: u32, flags: u32, s: *const u8, n: i32, out: *mut u16, outn: i32) -> i32;
    fn GetLocalTime(t: *mut SystemTime);
}

#[link(name = "gdi32")]
unsafe extern "system" {
    fn GetEnhMetaFileBits(h: Handle, n: u32, buf: *mut u8) -> u32;
    fn GetEnhMetaFileHeader(h: Handle, n: u32, buf: *mut u8) -> u32;
    fn GetMetaFileBitsEx(h: Handle, n: u32, buf: *mut u8) -> u32;
    fn SetEnhMetaFileBits(n: u32, buf: *const u8) -> Handle;
    fn SetWinMetaFileBits(n: u32, buf: *const u8, hdc: Handle, mfp: *const MetafilePict) -> Handle;
    fn DeleteEnhMetaFile(h: Handle) -> i32;
    fn PlayEnhMetaFile(hdc: Handle, h: Handle, r: *const Rect) -> i32;
    fn CreateCompatibleDC(hdc: Handle) -> Handle;
    fn DeleteDC(hdc: Handle) -> i32;
    fn CreateDIBSection(hdc: Handle, bmi: *const BitmapInfoHeader, usage: u32, bits: *mut *mut u8, sec: Handle, off: u32) -> Handle;
    fn SelectObject(hdc: Handle, h: Handle) -> Handle;
    fn DeleteObject(h: Handle) -> i32;
    fn SetStretchBltMode(hdc: Handle, mode: i32) -> i32;
    fn StretchDIBits(hdc: Handle, xd: i32, yd: i32, wd: i32, hd: i32, xs: i32, ys: i32, ws: i32, hs: i32,
                     bits: *const u8, bmi: *const u8, usage: u32, rop: u32) -> i32;
    fn GdiFlush() -> i32;
}

fn last_error() -> u32 {
    unsafe { GetLastError() }
}

fn now() -> String {
    let mut t = SystemTime::default();
    unsafe { GetLocalTime(&mut t) };
    format!("{:02}:{:02}:{:02}.{:03}", t.hour, t.minute, t.second, t.ms)
}

// ======================================================================
// クリップボードの読み取り
// ======================================================================
#[derive(Clone, Copy, PartialEq, Debug)]
enum Enc {
    Ansi,
    Oem,
    Utf8,
    Utf16,
}

impl Enc {
    fn label(self) -> String {
        match self {
            Enc::Ansi => format!("cp{} (ANSI)", unsafe { GetACP() }),
            Enc::Oem => format!("cp{} (OEM)", unsafe { GetOEMCP() }),
            Enc::Utf8 => "utf-8".into(),
            Enc::Utf16 => "utf-16-le".into(),
        }
    }
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Kind {
    Dib,
    Raw(ImageFormat),
    Emf,
    Wmf,
    Svg,
}

struct Item {
    fmt: u32,
    name: String,
    data: Vec<u8>,
    note: String,
    info: String,
    enc: Option<Enc>,
    kind: Option<Kind>,
    wmf: Option<(i32, i32, i32)>, // METAFILEPICT の mm, xExt, yExt
}

fn std_name(f: u32) -> Option<&'static str> {
    Some(match f {
        1 => "CF_TEXT", 2 => "CF_BITMAP", 3 => "CF_METAFILEPICT", 4 => "CF_SYLK", 5 => "CF_DIF",
        6 => "CF_TIFF", 7 => "CF_OEMTEXT", 8 => "CF_DIB", 9 => "CF_PALETTE", 10 => "CF_PENDATA",
        11 => "CF_RIFF", 12 => "CF_WAVE", 13 => "CF_UNICODETEXT", 14 => "CF_ENHMETAFILE",
        15 => "CF_HDROP", 16 => "CF_LOCALE", 17 => "CF_DIBV5",
        0x80 => "CF_OWNERDISPLAY", 0x81 => "CF_DSPTEXT", 0x82 => "CF_DSPBITMAP",
        0x83 => "CF_DSPMETAFILEPICT", 0x8E => "CF_DSPENHMETAFILE",
        _ => return None,
    })
}

fn fmt_name(f: u32) -> String {
    if let Some(s) = std_name(f) {
        return s.into();
    }
    let mut buf = [0u16; 512];
    let n = unsafe { GetClipboardFormatNameW(f, buf.as_mut_ptr(), 512) };
    if n > 0 {
        return String::from_utf16_lossy(&buf[..n as usize]);
    }
    match f {
        0x200..=0x2FF => format!("CF_PRIVATE+{}", f - 0x200),
        0x300..=0x3FF => format!("CF_GDIOBJ+{}", f - 0x300),
        _ => format!("#{f}"),
    }
}

fn text_enc_by_name(name: &str) -> Option<Enc> {
    Some(match name {
        "HTML Format" | "XML Spreadsheet" => Enc::Utf8,
        "Rich Text Format" | "RTF As Text" | "UniformResourceLocator" | "FileName" | "CSV" => Enc::Ansi,
        "UniformResourceLocatorW" | "FileNameW" => Enc::Utf16,
        _ => return None,
    })
}

/// 他のアプリが開いていることがあるので少し粘る
fn open_clipboard() -> Result<(), String> {
    for _ in 0..40 {
        if unsafe { OpenClipboard(0) } != 0 {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    Err("OpenClipboard に失敗しました（他のアプリが使用中の可能性）".into())
}

fn read_hglobal(h: Handle) -> Result<Vec<u8>, String> {
    unsafe {
        let n = GlobalSize(h);
        if n == 0 {
            return Err(format!("GlobalSize 失敗 (err={})", last_error()));
        }
        let p = GlobalLock(h) as *const u8;
        if p.is_null() {
            return Err(format!("GlobalLock 失敗 (err={})", last_error()));
        }
        let v = std::slice::from_raw_parts(p, n).to_vec();
        GlobalUnlock(h);
        Ok(v)
    }
}

fn read_item(f: u32) -> Item {
    let mut it = Item {
        fmt: f, name: fmt_name(f), data: vec![], note: String::new(), info: String::new(),
        enc: None, kind: None, wmf: None,
    };
    if matches!(f, 2 | 9 | 0x80 | 0x82) || (0x300..=0x3FF).contains(&f) {
        it.note = "GDI ハンドル / データなし（バイト列は取得できない）".into();
        if matches!(f, 2 | 0x82) {
            it.note += "。CF_DIB を参照";
        }
        return it;
    }
    let h = unsafe { GetClipboardData(f) };
    if h == 0 {
        it.note = format!("GetClipboardData 失敗 (err={})", last_error());
        return it;
    }
    unsafe {
        if matches!(f, 14 | 0x8E) {
            let n = GetEnhMetaFileBits(h, 0, std::ptr::null_mut());
            let mut buf = vec![0u8; n as usize];
            GetEnhMetaFileBits(h, n, buf.as_mut_ptr());
            it.data = buf;
            it.kind = Some(Kind::Emf);
            it.note = "GetEnhMetaFileBits() で取得した EMF バイト列".into();
        } else if matches!(f, 3 | 0x83) {
            let p = GlobalLock(h) as *const MetafilePict;
            if p.is_null() {
                it.note = "GlobalLock 失敗".into();
                return it;
            }
            let m = *p;
            GlobalUnlock(h);
            let n = GetMetaFileBitsEx(m.h_mf, 0, std::ptr::null_mut());
            let mut buf = vec![0u8; n as usize];
            GetMetaFileBitsEx(m.h_mf, n, buf.as_mut_ptr());
            it.data = buf;
            it.kind = Some(Kind::Wmf);
            it.wmf = Some((m.mm, m.x_ext, m.y_ext));
            it.note = "METAFILEPICT 内の WMF バイト列（GetMetaFileBitsEx）".into();
            it.info = format!("METAFILEPICT: mm={} xExt={} yExt={}", m.mm, m.x_ext, m.y_ext);
        } else {
            match read_hglobal(h) {
                Ok(v) => it.data = v,
                Err(e) => it.note = format!("読み取り失敗: {e}"),
            }
        }
    }
    it
}

fn u16_at(d: &[u8], o: usize) -> u16 {
    u16::from_le_bytes([d[o], d[o + 1]])
}
fn u32_at(d: &[u8], o: usize) -> u32 {
    u32::from_le_bytes([d[o], d[o + 1], d[o + 2], d[o + 3]])
}
fn i32_at(d: &[u8], o: usize) -> i32 {
    u32_at(d, o) as i32
}

fn sniff_image(b: &[u8]) -> Option<ImageFormat> {
    if b.starts_with(b"\x89PNG") {
        Some(ImageFormat::Png)
    } else if b.starts_with(b"\xff\xd8\xff") {
        Some(ImageFormat::Jpeg)
    } else if b.starts_with(b"GIF8") {
        Some(ImageFormat::Gif)
    } else if b.len() >= 12 && &b[..4] == b"RIFF" && &b[8..12] == b"WEBP" {
        Some(ImageFormat::Webp)
    } else if b.starts_with(b"BM") && b.len() > 26 {
        Some(ImageFormat::Bmp)
    } else if b.starts_with(b"II*\x00") || b.starts_with(b"MM\x00*") {
        Some(ImageFormat::Tiff)
    } else {
        None
    }
}

fn guess_text(name: &str, b: &[u8]) -> Option<Enc> {
    if b.is_empty() {
        return None;
    }
    if b.starts_with(b"\xef\xbb\xbf") {
        return Some(Enc::Utf8);
    }
    if b.starts_with(b"\xff\xfe") {
        return Some(Enc::Utf16);
    }
    let s = &b[..b.len().min(8192)];
    if s.len() >= 4 && s.len() % 2 == 0 {
        let zeros = s.iter().skip(1).step_by(2).filter(|&&c| c == 0).count();
        if zeros as f64 > s.len() as f64 / 2.0 * 0.3 {
            return Some(Enc::Utf16);
        }
    }
    for k in 0..4 {
        // 末尾でマルチバイトが切れている場合を考慮
        if k > s.len() {
            break;
        }
        match std::str::from_utf8(&s[..s.len() - k]) {
            Ok(t) => {
                let total = t.chars().count().max(1);
                let ctrl = t.chars().filter(|&c| (c as u32) < 0x20 && !"\t\r\n".contains(c)).count();
                if (ctrl as f64) < total as f64 * 0.05 {
                    return Some(Enc::Utf8);
                }
                break;
            }
            Err(_) if s.len() == b.len() => break,
            Err(_) => continue,
        }
    }
    let lower = name.to_lowercase();
    if ["svg", "xml", "json", "html", "text", "rtf"].iter().any(|k| lower.contains(k)) {
        return Some(Enc::Utf8);
    }
    None
}

fn decode(enc: Enc, b: &[u8]) -> String {
    let s = match enc {
        Enc::Utf8 => String::from_utf8_lossy(b.strip_prefix(b"\xef\xbb\xbf").unwrap_or(b)).into_owned(),
        Enc::Utf16 => {
            let w: Vec<u16> = b.chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
            let w = w.strip_prefix(&[0xFEFF]).unwrap_or(&w);
            String::from_utf16_lossy(w)
        }
        Enc::Ansi | Enc::Oem => {
            let cp = if enc == Enc::Ansi { 0 } else { 1 }; // CP_ACP / CP_OEMCP
            if b.is_empty() {
                return String::new();
            }
            unsafe {
                let n = MultiByteToWideChar(cp, 0, b.as_ptr(), b.len() as i32, std::ptr::null_mut(), 0);
                let mut w = vec![0u16; n.max(0) as usize];
                MultiByteToWideChar(cp, 0, b.as_ptr(), b.len() as i32, w.as_mut_ptr(), n);
                String::from_utf16_lossy(&w)
            }
        }
    };
    s.trim_end_matches('\0').to_string()
}

fn looks_like_svg(name: &str, d: &[u8], enc: Enc) -> bool {
    if name.to_lowercase().contains("svg") {
        return true;
    }
    let head = decode(enc, &d[..d.len().min(4096) & !1]);
    let head = head.trim_start_matches(['\u{feff}', ' ', '\t', '\r', '\n']).to_lowercase();
    head.starts_with("<svg") || (head.starts_with("<?xml") && head.contains("<svg"))
}

fn add_info(it: &mut Item, s: String) {
    if !it.info.is_empty() {
        it.info.push('\n');
    }
    it.info += &s;
}

fn analyze(it: &mut Item) {
    let (f, name) = (it.fmt, it.name.clone());
    it.enc = match f {
        1 => Some(Enc::Ansi),
        7 | 0x81 => Some(Enc::Oem),
        13 => Some(Enc::Utf16),
        _ => text_enc_by_name(&name),
    };
    let d = &it.data;
    if f == 16 && d.len() >= 4 {
        let lcid = u32_at(d, 0);
        add_info(it, format!("LCID = {lcid} (0x{lcid:04x})"));
    } else if f == 15 && d.len() >= 20 {
        let p_files = u32_at(d, 0) as usize;
        let wide = u32_at(d, 16) != 0;
        let s = decode(if wide { Enc::Utf16 } else { Enc::Ansi }, d.get(p_files..).unwrap_or(&[]));
        let files: Vec<&str> = s.split('\0').filter(|x| !x.is_empty()).collect();
        let msg = format!("DROPFILES (fWide={}) {} 件:\n{}", wide as u8, files.len(), files.join("\n"));
        add_info(it, msg);
    } else if matches!(f, 8 | 17) && d.len() >= 20 {
        let msg = format!(
            "BITMAPINFOHEADER: size={} {}x{} bpp={} compression={}",
            u32_at(d, 0), i32_at(d, 4), i32_at(d, 8).abs(), u16_at(d, 14), u32_at(d, 16)
        );
        add_info(it, msg);
        it.kind = Some(Kind::Dib);
    } else if name == "Preferred DropEffect" && d.len() >= 4 {
        let v = u32_at(d, 0);
        add_info(it, format!("DROPEFFECT = {v}"));
    }
    let d = &it.data;
    if it.kind == Some(Kind::Emf) && d.len() >= 40 {
        let (l, t, r, b) = (i32_at(d, 24), i32_at(d, 28), i32_at(d, 32), i32_at(d, 36));
        add_info(it, format!("EMF rclFrame: {:.1} x {:.1} mm", (r - l) as f64 / 100.0, (b - t) as f64 / 100.0));
    }
    if it.kind.is_none() {
        if let Some(sig) = sniff_image(&it.data) {
            it.kind = Some(Kind::Raw(sig));
            add_info(it, format!("signature: {}", sig.mime_type()));
        }
    }
    if it.enc.is_none() && it.kind.is_none() && f != 16 {
        it.enc = guess_text(&name, &it.data);
    }
    // SVG はテキストとしても画像としても見せる
    if it.kind.is_none() {
        if let Some(e) = it.enc {
            if looks_like_svg(&name, &it.data, e) {
                it.kind = Some(Kind::Svg);
            }
        }
    }
}

fn read_all() -> Result<Vec<Item>, String> {
    open_clipboard()?;
    let mut items = vec![];
    let mut f = 0;
    loop {
        f = unsafe { EnumClipboardFormats(f) };
        if f == 0 {
            break;
        }
        items.push(read_item(f));
    }
    unsafe { CloseClipboard() };
    for it in items.iter_mut() {
        analyze(it);
    }
    Ok(items)
}

// ======================================================================
// 画像化（GDI で 32bpp に描いて BMP にし、GPUI の img に渡す）
// ======================================================================
const MAX_SIDE: i32 = 2000;

/// 32bpp のメモリ DC を作って描画関数を呼び、結果を BMP バイト列にして返す
fn render_gdi(w: i32, h: i32, draw: impl FnOnce(Handle) -> bool) -> Result<Vec<u8>, String> {
    let hdr = BitmapInfoHeader { size: 40, width: w, height: -h, planes: 1, bit_count: 32, ..Default::default() };
    unsafe {
        let dc = CreateCompatibleDC(0);
        let mut bits: *mut u8 = std::ptr::null_mut();
        let bmp = CreateDIBSection(dc, &hdr, 0, &mut bits, 0, 0);
        if bmp == 0 || bits.is_null() {
            DeleteDC(dc);
            return Err(format!("CreateDIBSection 失敗 ({w}x{h})"));
        }
        let old = SelectObject(dc, bmp);
        let n = (w * h * 4) as usize;
        std::ptr::write_bytes(bits, 0xFF, n); // 白で塗る
        SetStretchBltMode(dc, 4); // HALFTONE
        let ok = draw(dc);
        GdiFlush();
        let mut px = std::slice::from_raw_parts(bits, n).to_vec();
        SelectObject(dc, old);
        DeleteObject(bmp);
        DeleteDC(dc);
        if !ok {
            return Err(format!("GDI 描画失敗 (err={})", last_error()));
        }
        for a in px.iter_mut().skip(3).step_by(4) {
            *a = 0xFF; // GDI はアルファを壊すので不透明にする
        }
        let mut out = Vec::with_capacity(54 + n);
        out.extend_from_slice(b"BM");
        out.extend_from_slice(&((54 + n) as u32).to_le_bytes());
        out.extend_from_slice(&[0, 0, 0, 0]);
        out.extend_from_slice(&54u32.to_le_bytes());
        out.extend_from_slice(&40u32.to_le_bytes());
        out.extend_from_slice(&w.to_le_bytes());
        out.extend_from_slice(&(-h).to_le_bytes());
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&32u16.to_le_bytes());
        out.extend_from_slice(&[0u8; 24]);
        out.extend_from_slice(&px);
        Ok(out)
    }
}

fn fit(w: f64, h: f64) -> (i32, i32) {
    let k = (MAX_SIDE as f64 / w.max(h)).min(1.0);
    (((w * k) as i32).max(1), ((h * k) as i32).max(1))
}

fn render_emf(hemf: Handle) -> Result<(Vec<u8>, String), String> {
    let mut bits = [0u8; 108]; // ENHMETAHEADER
    let n = unsafe { GetEnhMetaFileHeader(hemf, 108, bits.as_mut_ptr()) };
    let (mut w, mut h) = (800.0, 600.0);
    if n >= 40 {
        // rclFrame は 0.01mm 単位。96dpi 換算の 2 倍（高 DPI 向け）で描く
        let fw = (i32_at(&bits, 32) - i32_at(&bits, 24)) as f64 / 2540.0 * 96.0 * 2.0;
        let fh = (i32_at(&bits, 36) - i32_at(&bits, 28)) as f64 / 2540.0 * 96.0 * 2.0;
        if fw > 1.0 && fh > 1.0 {
            (w, h) = (fw, fh);
        }
    }
    let (w, h) = fit(w, h);
    let r = Rect { left: 0, top: 0, right: w, bottom: h };
    let bmp = render_gdi(w, h, |dc| unsafe { PlayEnhMetaFile(dc, hemf, &r) } != 0);
    unsafe { DeleteEnhMetaFile(hemf) };
    Ok((bmp?, format!("PlayEnhMetaFile → {w}x{h}px")))
}

fn render_svg(src: &[u8]) -> Result<(Vec<u8>, u32, u32), String> {
    use resvg::usvg;
    static FONTS: std::sync::LazyLock<Arc<usvg::fontdb::Database>> = std::sync::LazyLock::new(|| {
        let mut db = usvg::fontdb::Database::new();
        db.load_system_fonts();
        Arc::new(db)
    });
    let opt = usvg::Options { fontdb: FONTS.clone(), ..Default::default() };
    let tree = usvg::Tree::from_data(src, &opt).map_err(|e| format!("SVG 解析失敗: {e}"))?;
    let sz = tree.size();
    // 高 DPI 向けに 2 倍で描く（長辺は MAX_SIDE まで）
    let k = (2.0f32).min(MAX_SIDE as f32 / sz.width().max(sz.height()));
    let (w, h) = (((sz.width() * k) as u32).max(1), ((sz.height() * k) as u32).max(1));
    let mut pm = tiny_skia::Pixmap::new(w, h).ok_or("Pixmap 作成失敗")?;
    resvg::render(&tree, tiny_skia::Transform::from_scale(k, k), &mut pm.as_mut());
    let png = pm.encode_png().map_err(|e| format!("PNG 化失敗: {e}"))?;
    Ok((png, w, h))
}

fn make_image(it: &Item) -> Result<(Arc<Image>, String), String> {
    let d = &it.data;
    let bmp = |v: Vec<u8>| Arc::new(Image::from_bytes(ImageFormat::Bmp, v));
    match it.kind {
        Some(Kind::Raw(f)) => Ok((Arc::new(Image::from_bytes(f, d.clone())), format!("生データ ({})", f.mime_type()))),
        Some(Kind::Svg) => {
            let src = match it.enc {
                Some(Enc::Utf8) | None => d.clone(),
                Some(e) => decode(e, d).into_bytes(),
            };
            // GPUI 0.2.2 の ImageFormat::Svg は RGBA→BGRA 変換が抜けていて色が化けるので、
            // resvg で PNG にしてから渡す
            let (png, w, h) = render_svg(&src)?;
            Ok((Arc::new(Image::from_bytes(ImageFormat::Png, png)), format!("SVG → resvg → {w}x{h}px")))
        }
        Some(Kind::Emf) => {
            let h = unsafe { SetEnhMetaFileBits(d.len() as u32, d.as_ptr()) };
            if h == 0 {
                return Err("SetEnhMetaFileBits 失敗".into());
            }
            let (v, msg) = render_emf(h)?;
            Ok((bmp(v), format!("EMF: {msg}")))
        }
        Some(Kind::Wmf) => {
            let (mm, x, y) = it.wmf.unwrap_or((8, 0, 0));
            let mfp = MetafilePict { mm, x_ext: x, y_ext: y, h_mf: 0 };
            let h = unsafe { SetWinMetaFileBits(d.len() as u32, d.as_ptr(), 0, &mfp) };
            if h == 0 {
                return Err(format!("SetWinMetaFileBits 失敗 (err={})", last_error()));
            }
            let (v, msg) = render_emf(h)?;
            Ok((bmp(v), format!("WMF → EMF (SetWinMetaFileBits): {msg}")))
        }
        Some(Kind::Dib) => {
            if d.len() < 40 {
                return Err("DIB が短すぎます".into());
            }
            let hs = u32_at(d, 0) as usize;
            let (w, h) = (i32_at(d, 4), i32_at(d, 8).abs());
            let bpp = u16_at(d, 14) as u32;
            let comp = u32_at(d, 16);
            if comp == 4 || comp == 5 {
                // BI_JPEG / BI_PNG: ピクセル部分がそのまま画像ファイル
                let f = if comp == 4 { ImageFormat::Jpeg } else { ImageFormat::Png };
                return Ok((Arc::new(Image::from_bytes(f, d[hs..].to_vec())), "DIB 内の JPEG/PNG".into()));
            }
            let clr = if hs >= 36 { u32_at(d, 32) } else { 0 };
            let masks = if hs == 40 { match comp { 3 => 12, 6 => 16, _ => 0 } } else { 0 };
            let ncol = if clr > 0 { clr } else if bpp <= 8 { 1 << bpp } else { 0 };
            let off = hs + masks + ncol as usize * 4;
            if off >= d.len() || w <= 0 || h <= 0 {
                return Err("DIB ヘッダーが不正です".into());
            }
            let (tw, th) = fit(w as f64, h as f64);
            let v = render_gdi(tw, th, |dc| unsafe {
                StretchDIBits(dc, 0, 0, tw, th, 0, 0, w, h, d[off..].as_ptr(), d.as_ptr(), 0, 0x00CC0020) != 0
            })?;
            Ok((bmp(v), format!("CF_DIB → StretchDIBits ({w}x{h} → {tw}x{th})")))
        }
        None => Err("画像ではありません".into()),
    }
}

// ======================================================================
// 表示用の文字列
// ======================================================================
fn kind_label(it: &Item) -> String {
    let mut v = vec![];
    match it.kind {
        Some(Kind::Dib) => v.push("画像(DIB)".to_string()),
        Some(Kind::Raw(f)) => v.push(format!("画像({})", f.mime_type().trim_start_matches("image/"))),
        Some(Kind::Emf) => v.push("画像(EMF)".into()),
        Some(Kind::Wmf) => v.push("画像(WMF)".into()),
        Some(Kind::Svg) => v.push("画像(SVG)".into()),
        None => {}
    }
    if it.enc.is_some() {
        v.push("テキスト".into());
    }
    if v.is_empty() && it.data.is_empty() {
        v.push("-".into());
    }
    v.join(" / ")
}

fn hex_line(d: &[u8], row: usize) -> String {
    let start = row * 16;
    let chunk = &d[start..(start + 16).min(d.len())];
    let mut s = format!("{start:08x}  ");
    for i in 0..16 {
        match chunk.get(i) {
            Some(b) => s += &format!("{b:02x} "),
            None => s += "   ",
        }
        if i == 7 {
            s.push(' ');
        }
    }
    s.push(' ');
    s.extend(chunk.iter().map(|&b| if (0x20..0x7f).contains(&b) { b as char } else { '.' }));
    s
}

/// 長い行は uniform_list に載せるために折り返す
fn split_lines(text: &str) -> Vec<SharedString> {
    const W: usize = 160;
    let mut out = vec![];
    for line in text.replace("\r\n", "\n").replace('\r', "\n").split('\n') {
        let line = line.replace('\t', "    ");
        let chars: Vec<char> = line.chars().collect();
        if chars.is_empty() {
            out.push(SharedString::from(""));
        }
        for c in chars.chunks(W) {
            out.push(c.iter().collect::<String>().into());
        }
    }
    out
}

fn mono(d: gpui::Div) -> gpui::Div {
    d.font_family(FONT_MONO).text_size(px(13.)).whitespace_nowrap()
}

fn human(n: usize) -> String {
    if n < 1024 { format!("{n} B") } else if n < 1 << 20 { format!("{:.1} KB", n as f64 / 1024.0) } else { format!("{:.1} MB", n as f64 / 1048576.0) }
}

// ======================================================================
// GPUI のビュー
// ======================================================================
#[derive(Clone, Copy, PartialEq)]
enum Tab {
    Image,
    Text,
    Hex,
    Info,
}

struct Viewer {
    items: Vec<Item>,
    sel: usize,
    tab: Tab,
    seq: u32,
    epoch: usize, // 再読込のたびに増やす（リストのスクロール位置をリセットするため）
    text: Vec<SharedString>,
    image: Option<Result<(Arc<Image>, String), String>>,
    log: Vec<SharedString>,
    log_scroll: UniformListScrollHandle,
    _poll: Option<gpui::Task<()>>,
}

const FONT_UI: &str = "Yu Gothic UI";
const FONT_MONO: &str = "BIZ UDGothic";

impl Viewer {
    fn new(cx: &mut Context<Self>) -> Self {
        let mut v = Viewer {
            items: vec![], sel: 0, tab: Tab::Info, seq: 0, epoch: 0, text: vec![], image: None,
            log: vec![], log_scroll: UniformListScrollHandle::new(), _poll: None,
        };
        v.log("起動しました（GPUI）");
        v.reload();
        // 1 秒ごとにシーケンス番号を見て、変わったら読み直す
        v._poll = Some(cx.spawn(async move |this, cx| {
            loop {
                cx.background_executor().timer(Duration::from_secs(1)).await;
                let alive = this.update(cx, |v, cx| {
                    let s = unsafe { GetClipboardSequenceNumber() };
                    if s != v.seq {
                        v.log(format!("クリップボードが変更されました (seq={s})"));
                        v.reload();
                        cx.notify();
                    }
                });
                if alive.is_err() {
                    break;
                }
            }
        }));
        v
    }

    fn log(&mut self, s: impl Into<String>) {
        self.log.push(format!("[{}] {}", now(), s.into()).into());
        self.log_scroll.scroll_to_item(self.log.len() - 1, ScrollStrategy::Top);
    }

    fn reload(&mut self) {
        self.seq = unsafe { GetClipboardSequenceNumber() };
        let prev = self.items.get(self.sel).map(|i| i.fmt);
        match read_all() {
            Ok(items) => {
                self.items = items;
                self.epoch += 1;
                self.log(format!("{} 個のフォーマットを読み込みました (seq={})", self.items.len(), self.seq));
                let sel = prev
                    .and_then(|f| self.items.iter().position(|i| i.fmt == f))
                    .or_else(|| self.items.iter().position(|i| i.kind.is_some()))
                    .or_else(|| self.items.iter().position(|i| i.enc.is_some()))
                    .unwrap_or(0);
                self.select(sel);
            }
            Err(e) => self.log(e),
        }
    }

    fn select(&mut self, i: usize) {
        self.sel = i;
        self.text.clear();
        self.image = None;
        let Some(it) = self.items.get(i) else { return };
        if let Some(e) = it.enc {
            self.text = split_lines(&decode(e, &it.data));
        }
        if it.kind.is_some() {
            let r = make_image(it);
            match &r {
                Ok((_, m)) => self.log(format!("{}: {m}", it.name)),
                Err(e) => self.log(format!("{}: 画像化失敗 {e}", it.name)),
            }
            self.image = Some(r);
        }
        let name = self.items[i].name.clone();
        let (has_img, has_txt) = (self.image.is_some(), !self.text.is_empty());
        if !(self.tab == Tab::Image && has_img || self.tab == Tab::Text && has_txt || self.tab == Tab::Hex) {
            self.tab = if has_img { Tab::Image } else if has_txt { Tab::Text } else { Tab::Info };
        }
        self.log(format!("選択: {name}"));
    }

    fn button(&self, id: &'static str, label: &'static str) -> gpui::Stateful<gpui::Div> {
        div()
            .id(id)
            .px_3()
            .py_1()
            .rounded_md()
            .border_1()
            .border_color(rgb(0xc8c8c8))
            .bg(rgb(0xffffff))
            .hover(|s| s.bg(rgb(0xe8f0fe)))
            .cursor_pointer()
            .child(label)
    }

    fn render_tabs(&self, cx: &mut Context<Self>) -> impl IntoElement {
        let tabs = [(Tab::Image, "画像"), (Tab::Text, "テキスト"), (Tab::Hex, "Hex"), (Tab::Info, "情報")];
        div().flex().gap_1().px_2().pt_2().border_b_1().border_color(rgb(0xd0d0d0)).children(
            tabs.into_iter().map(|(t, label)| {
                let active = self.tab == t;
                div()
                    .id(label)
                    .px_4()
                    .py_1()
                    .rounded_t_md()
                    .border_1()
                    .border_color(rgb(0xd0d0d0))
                    .when(active, |d| d.bg(rgb(0xffffff)).font_weight(gpui::FontWeight::BOLD))
                    .when(!active, |d| d.bg(rgb(0xeeeeee)).text_color(rgb(0x666666)))
                    .cursor_pointer()
                    .on_click(cx.listener(move |v, _, _, cx| {
                        v.tab = t;
                        cx.notify();
                    }))
                    .child(label)
            }),
        )
    }

    fn render_body(&self, cx: &mut Context<Self>) -> gpui::AnyElement {
        let Some(it) = self.items.get(self.sel) else {
            return div().p_4().child("クリップボードは空です").into_any_element();
        };
        match self.tab {
            Tab::Image => match &self.image {
                Some(Ok((image, msg))) => div()
                    .size_full()
                    .flex()
                    .flex_col()
                    .child(div().px_2().py_1().text_color(rgb(0x666666)).child(msg.clone()))
                    .child(
                        div().flex_1().min_h_0().overflow_hidden().bg(rgb(0xf4f4f4)).child(
                            img(image.clone()).size_full().object_fit(ObjectFit::ScaleDown),
                        ),
                    )
                    .into_any_element(),
                Some(Err(e)) => div().p_4().text_color(rgb(0xc00000)).child(e.clone()).into_any_element(),
                None => div().p_4().child("画像としてプレビューできません").into_any_element(),
            },
            Tab::Text => {
                if self.text.is_empty() {
                    return div().p_4().child("テキストとしてプレビューできません").into_any_element();
                }
                let head = format!("{} / {} 行", it.enc.map(|e| e.label()).unwrap_or_default(), self.text.len());
                div()
                    .size_full()
                    .flex()
                    .flex_col()
                    .child(div().px_2().py_1().text_color(rgb(0x666666)).child(head))
                    .child(
                        uniform_list(
                            ("text", self.epoch * 1000 + self.sel),
                            self.text.len(),
                            cx.processor(|v, range: std::ops::Range<usize>, _, _| {
                                range.map(|i| mono(div().px_2()).child(v.text[i].clone())).collect()
                            }),
                        )
                        .flex_1(),
                    )
                    .into_any_element()
            }
            Tab::Hex => {
                let rows = it.data.len().div_ceil(16);
                if rows == 0 {
                    return div().p_4().child("データがありません").into_any_element();
                }
                uniform_list(
                    ("hex", self.epoch * 1000 + self.sel),
                    rows,
                    cx.processor(|v, range: std::ops::Range<usize>, _, _| {
                        let d = &v.items[v.sel].data;
                        range.map(|r| mono(div().px_2()).child(hex_line(d, r))).collect()
                    }),
                )
                .size_full()
                .into_any_element()
            }
            Tab::Info => {
                let mut rows = vec![
                    ("フォーマット ID", format!("{} (0x{:04X})", it.fmt, it.fmt)),
                    ("名前", it.name.clone()),
                    ("サイズ", format!("{} ({} bytes)", human(it.data.len()), it.data.len())),
                    ("種類", kind_label(it)),
                    ("文字コード", it.enc.map(|e| e.label()).unwrap_or("-".into())),
                ];
                if !it.note.is_empty() {
                    rows.push(("備考", it.note.clone()));
                }
                if !it.info.is_empty() {
                    rows.push(("詳細", it.info.clone()));
                }
                div()
                    .id("info")
                    .size_full()
                    .overflow_y_scroll()
                    .p_3()
                    .flex()
                    .flex_col()
                    .gap_2()
                    .children(rows.into_iter().map(|(k, v)| {
                        div()
                            .flex()
                            .gap_3()
                            .child(div().w(px(120.)).flex_none().text_color(rgb(0x666666)).child(k))
                            .child(div().flex_1().children(v.lines().map(|l| div().child(l.to_string())).collect::<Vec<_>>()))
                    }))
                    .into_any_element()
            }
        }
    }
}

impl Render for Viewer {
    fn render(&mut self, _w: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let header_cell = |w: f32, s: &'static str| div().w(px(w)).flex_none().child(s);
        div()
            .size_full()
            .flex()
            .flex_col()
            .bg(rgb(0xfafafa))
            .text_color(rgb(0x202020))
            .font_family(FONT_UI)
            .text_size(px(14.))
            // ツールバー
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap_2()
                    .px_3()
                    .py_2()
                    .border_b_1()
                    .border_color(rgb(0xd0d0d0))
                    .child(div().font_weight(gpui::FontWeight::BOLD).child("Clipboard Type Viewer"))
                    .child(div().text_color(rgb(0x666666)).child(format!("{} 個 / seq={}", self.items.len(), self.seq)))
                    .child(div().flex_1())
                    .child(self.button("reload", "再読込").on_click(cx.listener(|v, _, _, cx| {
                        v.log("手動で再読込");
                        v.reload();
                        cx.notify();
                    }))),
            )
            // 本体: 左に一覧、右にプレビュー
            .child(
                div()
                    .flex_1()
                    .min_h_0()
                    .flex()
                    .child(
                        div()
                            .w(px(470.))
                            .flex_none()
                            .flex()
                            .flex_col()
                            .border_r_1()
                            .border_color(rgb(0xd0d0d0))
                            .bg(rgb(0xffffff))
                            .child(
                                div()
                                    .flex()
                                    .px_2()
                                    .py_1()
                                    .bg(rgb(0xeeeeee))
                                    .text_color(rgb(0x555555))
                                    .child(header_cell(60., "ID"))
                                    .child(div().flex_1().child("名前"))
                                    .child(header_cell(80., "サイズ"))
                                    .child(header_cell(130., "種類")),
                            )
                            .child(
                                uniform_list(
                                    ("formats", self.epoch),
                                    self.items.len(),
                                    cx.processor(|v, range: std::ops::Range<usize>, _, cx| {
                                        range
                                            .map(|i| {
                                                let it = &v.items[i];
                                                let selected = i == v.sel;
                                                div()
                                                    .id(i)
                                                    .w_full()
                                                    .flex()
                                                    .px_2()
                                                    .py_0p5()
                                                    .cursor_pointer()
                                                    .when(selected, |d| d.bg(rgb(0xcce0ff)))
                                                    .when(!selected, |d| d.hover(|s| s.bg(rgb(0xeef4ff))))
                                                    .when(it.kind.is_none() && it.enc.is_none(), |d| d.text_color(rgb(0x808080)))
                                                    .on_click(cx.listener(move |v, _, _, cx| {
                                                        v.select(i);
                                                        cx.notify();
                                                    }))
                                                    .child(div().w(px(60.)).flex_none().child(it.fmt.to_string()))
                                                    .child(div().flex_1().overflow_hidden().whitespace_nowrap().child(it.name.clone()))
                                                    .child(div().w(px(80.)).flex_none().child(human(it.data.len())))
                                                    .child(div().w(px(130.)).flex_none().whitespace_nowrap().child(kind_label(it)))
                                            })
                                            .collect()
                                    }),
                                )
                                .flex_1(),
                            ),
                    )
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .flex()
                            .flex_col()
                            .child(self.render_tabs(cx))
                            .child(div().flex_1().min_h_0().bg(rgb(0xffffff)).child(self.render_body(cx))),
                    ),
            )
            // ログ
            .child(
                div()
                    .h(px(150.))
                    .flex_none()
                    .flex()
                    .flex_col()
                    .border_t_1()
                    .border_color(rgb(0xd0d0d0))
                    .child(
                        div()
                            .flex()
                            .items_center()
                            .gap_2()
                            .px_3()
                            .py_1()
                            .child(div().text_color(rgb(0x555555)).child("ログ"))
                            .child(div().flex_1())
                            .child(self.button("copylog", "ログをコピー").on_click(cx.listener(|v, _, _, cx| {
                                let text = v.log.iter().map(|s| s.as_ref()).collect::<Vec<&str>>().join("\n");
                                cx.write_to_clipboard(ClipboardItem::new_string(text));
                                v.log("ログをクリップボードにコピーしました");
                                cx.notify();
                            }))),
                    )
                    .child(
                        uniform_list(
                            "log",
                            self.log.len(),
                            cx.processor(|v, range: std::ops::Range<usize>, _, _| {
                                range
                                    .map(|i| {
                                        div()
                                            .px_3()
                                            .font_family(FONT_MONO)
                                            .text_size(px(12.))
                                            .whitespace_nowrap()
                                            .child(v.log[i].clone())
                                    })
                                    .collect()
                            }),
                        )
                        .track_scroll(self.log_scroll.clone())
                        .flex_1()
                        .bg(rgb(0xffffff)),
                    ),
            )
    }
}

// ======================================================================
// main
// ======================================================================
fn dump() {
    match read_all() {
        Ok(items) => {
            for it in &items {
                println!("{:>6}  {:<32} {:>10}  {}", it.fmt, it.name, human(it.data.len()), kind_label(it));
                if !it.note.is_empty() {
                    println!("        note: {}", it.note);
                }
                for l in it.info.lines() {
                    println!("        {l}");
                }
            }
        }
        Err(e) => eprintln!("{e}"),
    }
}

fn main() {
    if std::env::args().any(|a| a == "--dump") {
        dump();
        return;
    }
    Application::new().run(|cx: &mut App| {
        let bounds = Bounds::centered(None, size(px(1200.), px(800.)), cx);
        cx.open_window(
            WindowOptions {
                window_bounds: Some(WindowBounds::Windowed(bounds)),
                titlebar: Some(TitlebarOptions { title: Some("Clipboard Type Viewer (GPUI)".into()), ..Default::default() }),
                ..Default::default()
            },
            |_, cx| cx.new(Viewer::new),
        )
        .unwrap();
        cx.on_window_closed(|cx| cx.quit()).detach();
        cx.activate(true);
    });
}
