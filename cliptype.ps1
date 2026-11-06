<#
  Clipboard Type Viewer (PowerShell + WinForms)

  Windows のクリップボードに入っている全フォーマット（EMF, CF_DIB, HTML Format など）を
  列挙し、Hex ダンプ・テキスト・画像でプレビューする。

  起動:  powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File cliptype.ps1
         ... -File cliptype.ps1 -Dump         (GUI なしで一覧を標準出力に出す)
         ... -File cliptype.ps1 -HideConsole  (コンソール窓を隠す。ショートカット用)
             ※ -WindowStyle Hidden はフォームまで表示されなくなるので使わないこと

  ※ Windows PowerShell 5.1 (powershell.exe) 用。ファイルは UTF-8 (BOM 付き) で保存すること。
#>
param([switch]$Dump, [switch]$HideConsole)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

public class ClipItem {
    public uint Id;
    public string Name = "";
    public byte[] Data = new byte[0];
    public string Note = "";
    public string Info = "";
    public int CodePage;      // テキスト表示する場合のコードページ（0 = テキストではない）
    public string Kind = "";  // emf / wmf / dib / raw（画像プレビュー可能なもの）
    public int MfMM, MfX, MfY;
}

public static class ClipNative {
    [DllImport("user32.dll", SetLastError = true)] static extern bool OpenClipboard(IntPtr h);
    [DllImport("user32.dll", SetLastError = true)] static extern bool CloseClipboard();
    [DllImport("user32.dll", SetLastError = true)] static extern uint EnumClipboardFormats(uint f);
    [DllImport("user32.dll", SetLastError = true)] static extern IntPtr GetClipboardData(uint f);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClipboardFormatName(uint f, StringBuilder sb, int max);
    [DllImport("user32.dll")] static extern uint GetClipboardSequenceNumber();
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("kernel32.dll", SetLastError = true)] static extern UIntPtr GlobalSize(IntPtr h);
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr GlobalLock(IntPtr h);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool GlobalUnlock(IntPtr h);
    [DllImport("kernel32.dll")] static extern uint GetACP();
    [DllImport("kernel32.dll")] static extern uint GetOEMCP();
    [DllImport("gdi32.dll")] static extern uint GetEnhMetaFileBits(IntPtr h, uint n, byte[] buf);
    [DllImport("gdi32.dll")] static extern uint GetMetaFileBitsEx(IntPtr h, uint n, byte[] buf);
    [DllImport("gdi32.dll")] static extern IntPtr SetWinMetaFileBits(uint n, byte[] buf, IntPtr hdc, ref METAFILEPICT mfp);

    [StructLayout(LayoutKind.Sequential)]
    public struct METAFILEPICT { public int mm, xExt, yExt; public IntPtr hMF; }

    public static int Acp { get { return (int)GetACP(); } }
    public static int Oemcp { get { return (int)GetOEMCP(); } }
    public static uint GetSeq() { return GetClipboardSequenceNumber(); }
    public static void DpiAware() { SetProcessDPIAware(); }

    [DllImport("kernel32.dll")] static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
    public static void HideConsole() { IntPtr h = GetConsoleWindow(); if (h != IntPtr.Zero) ShowWindow(h, 0); }

    static readonly Dictionary<uint, string> Std = new Dictionary<uint, string> {
        {1,"CF_TEXT"},{2,"CF_BITMAP"},{3,"CF_METAFILEPICT"},{4,"CF_SYLK"},{5,"CF_DIF"},{6,"CF_TIFF"},
        {7,"CF_OEMTEXT"},{8,"CF_DIB"},{9,"CF_PALETTE"},{10,"CF_PENDATA"},{11,"CF_RIFF"},{12,"CF_WAVE"},
        {13,"CF_UNICODETEXT"},{14,"CF_ENHMETAFILE"},{15,"CF_HDROP"},{16,"CF_LOCALE"},{17,"CF_DIBV5"},
        {0x80,"CF_OWNERDISPLAY"},{0x81,"CF_DSPTEXT"},{0x82,"CF_DSPBITMAP"},{0x83,"CF_DSPMETAFILEPICT"},{0x8E,"CF_DSPENHMETAFILE"}
    };

    public static string FormatName(uint f) {
        string s;
        if (Std.TryGetValue(f, out s)) return s;
        var sb = new StringBuilder(512);
        if (GetClipboardFormatName(f, sb, 512) > 0) return sb.ToString();
        if (f >= 0x200 && f <= 0x2FF) return "CF_PRIVATE+" + (f - 0x200);
        if (f >= 0x300 && f <= 0x3FF) return "CF_GDIOBJ+" + (f - 0x300);
        return "#" + f;
    }

    public static ClipItem[] ReadAll() {
        bool opened = false;
        for (int i = 0; i < 40; i++) {
            if (OpenClipboard(IntPtr.Zero)) { opened = true; break; }
            System.Threading.Thread.Sleep(50);
        }
        if (!opened) throw new Exception("OpenClipboard に失敗しました（他のアプリが使用中の可能性）");
        var list = new List<ClipItem>();
        try {
            uint f = 0;
            while ((f = EnumClipboardFormats(f)) != 0) list.Add(ReadItem(f));
        } finally {
            CloseClipboard();
        }
        foreach (var it in list) Analyze(it);
        return list.ToArray();
    }

    static ClipItem ReadItem(uint f) {
        var it = new ClipItem();
        it.Id = f;
        it.Name = FormatName(f);
        if (f == 2 || f == 9 || f == 0x80 || f == 0x82 || (f >= 0x300 && f <= 0x3FF)) {
            it.Note = "GDI ハンドル / データなし（バイト列は取得できない）" + ((f == 2 || f == 0x82) ? "。CF_DIB を参照" : "");
            return it;
        }
        IntPtr h = GetClipboardData(f);
        if (h == IntPtr.Zero) {
            it.Note = "GetClipboardData 失敗 (err=" + Marshal.GetLastWin32Error() + ")";
            return it;
        }
        try {
            if (f == 14 || f == 0x8E) {
                uint n = GetEnhMetaFileBits(h, 0, null);
                var b = new byte[n];
                GetEnhMetaFileBits(h, n, b);
                it.Data = b; it.Kind = "emf";
                it.Note = "GetEnhMetaFileBits() で取得した EMF バイト列";
            } else if (f == 3 || f == 0x83) {
                IntPtr p = GlobalLock(h);
                if (p == IntPtr.Zero) throw new Exception("GlobalLock 失敗");
                METAFILEPICT m;
                try { m = (METAFILEPICT)Marshal.PtrToStructure(p, typeof(METAFILEPICT)); }
                finally { GlobalUnlock(h); }
                uint n = GetMetaFileBitsEx(m.hMF, 0, null);
                var b = new byte[n];
                GetMetaFileBitsEx(m.hMF, n, b);
                it.Data = b; it.Kind = "wmf";
                it.MfMM = m.mm; it.MfX = m.xExt; it.MfY = m.yExt;
                it.Note = "METAFILEPICT 内の WMF バイト列（GetMetaFileBitsEx）";
                it.Info = "METAFILEPICT: mm=" + m.mm + " xExt=" + m.xExt + " yExt=" + m.yExt;
            } else {
                ulong size = (ulong)GlobalSize(h);
                if (size == 0) throw new Exception("GlobalSize 失敗 (err=" + Marshal.GetLastWin32Error() + ")");
                IntPtr p = GlobalLock(h);
                if (p == IntPtr.Zero) throw new Exception("GlobalLock 失敗");
                try {
                    var b = new byte[size];
                    Marshal.Copy(p, b, 0, (int)size);
                    it.Data = b;
                } finally { GlobalUnlock(h); }
            }
        } catch (Exception e) {
            it.Note = "読み取り失敗: " + e.Message;
        }
        return it;
    }

    static readonly Dictionary<string, int> TextByName = new Dictionary<string, int>();
    static ClipNative() {
        int acp = (int)GetACP();
        TextByName["HTML Format"] = 65001;
        TextByName["Rich Text Format"] = acp;
        TextByName["RTF As Text"] = acp;
        TextByName["UniformResourceLocator"] = acp;
        TextByName["UniformResourceLocatorW"] = 1200;
        TextByName["FileName"] = acp;
        TextByName["FileNameW"] = 1200;
        TextByName["CSV"] = acp;
        TextByName["XML Spreadsheet"] = 65001;
    }

    static void AddInfo(ClipItem it, string s) { it.Info = it.Info.Length > 0 ? it.Info + "\r\n" + s : s; }

    static void Analyze(ClipItem it) {
        byte[] d = it.Data;
        uint f = it.Id;
        int tcp;
        if (f == 1) it.CodePage = (int)GetACP();
        else if (f == 7 || f == 0x81) it.CodePage = (int)GetOEMCP();
        else if (f == 13) it.CodePage = 1200;
        else if (TextByName.TryGetValue(it.Name, out tcp)) it.CodePage = tcp;

        if (f == 16 && d.Length >= 4) {
            int lcid = BitConverter.ToInt32(d, 0);
            AddInfo(it, "LCID = " + lcid + " (0x" + lcid.ToString("x4") + ")");
        } else if (f == 15 && d.Length >= 20) {
            int pFiles = BitConverter.ToInt32(d, 0);
            int wide = BitConverter.ToInt32(d, 16);
            if (pFiles >= 0 && pFiles <= d.Length) {
                string s = wide != 0 ? Encoding.Unicode.GetString(d, pFiles, d.Length - pFiles)
                                     : Encoding.GetEncoding((int)GetACP()).GetString(d, pFiles, d.Length - pFiles);
                var files = s.Split(new[] { '\0' }, StringSplitOptions.RemoveEmptyEntries);
                AddInfo(it, "DROPFILES (fWide=" + wide + ") " + files.Length + " 件:\r\n" + string.Join("\r\n", files));
            }
        } else if ((f == 8 || f == 17) && d.Length >= 20) {
            AddInfo(it, "BITMAPINFOHEADER: size=" + BitConverter.ToInt32(d, 0) + " " + BitConverter.ToInt32(d, 4) + "x" +
                Math.Abs(BitConverter.ToInt32(d, 8)) + " bpp=" + BitConverter.ToUInt16(d, 14) + " compression=" + BitConverter.ToInt32(d, 16));
            it.Kind = "dib";
        } else if (it.Name == "Preferred DropEffect" && d.Length >= 4) {
            AddInfo(it, "DROPEFFECT = " + BitConverter.ToInt32(d, 0));
        }
        if (it.Kind == "emf" && d.Length >= 40) {
            int l = BitConverter.ToInt32(d, 24), t = BitConverter.ToInt32(d, 28), r = BitConverter.ToInt32(d, 32), b = BitConverter.ToInt32(d, 36);
            AddInfo(it, string.Format("EMF rclFrame: {0:0.0} x {1:0.0} mm", (r - l) / 100.0, (b - t) / 100.0));
        }
        if (it.Kind == "") {
            string sig = Sniff(d);
            if (sig != null) { it.Kind = "raw"; AddInfo(it, "signature: " + sig); }
        }
        if (it.CodePage == 0 && it.Kind == "" && f != 16) it.CodePage = GuessText(it.Name, d);
    }

    static bool M(byte[] b, int off, params byte[] sig) {
        if (b.Length < off + sig.Length) return false;
        for (int i = 0; i < sig.Length; i++) if (b[off + i] != sig[i]) return false;
        return true;
    }

    public static string Sniff(byte[] b) {
        if (M(b, 0, 0x89, 0x50, 0x4e, 0x47)) return "png";
        if (M(b, 0, 0xff, 0xd8, 0xff)) return "jpeg";
        if (M(b, 0, 0x47, 0x49, 0x46, 0x38)) return "gif";
        if (M(b, 0, 0x42, 0x4d) && b.Length > 26) return "bmp";
        if (M(b, 0, 0x49, 0x49, 0x2a, 0x00) || M(b, 0, 0x4d, 0x4d, 0x00, 0x2a)) return "tiff";
        if (M(b, 0, 0x00, 0x00, 0x01, 0x00) && b.Length > 22) return "ico";
        return null;
    }

    static int GuessText(string name, byte[] b) {
        if (b.Length == 0) return 0;
        if (M(b, 0, 0xef, 0xbb, 0xbf)) return 65001;
        if (M(b, 0, 0xff, 0xfe)) return 1200;
        int n = Math.Min(b.Length, 8192);
        if (n >= 4 && n % 2 == 0) {
            int z = 0;
            for (int i = 1; i < n; i += 2) if (b[i] == 0) z++;
            if (z > n / 2 * 0.3) return 1200;
        }
        var strict = new UTF8Encoding(false, true);
        for (int k = 0; k < 4 && n - k > 0; k++) {
            try {
                string s = strict.GetString(b, 0, n - k);
                int ctrl = 0;
                foreach (char c in s) if (c < 0x20 && c != '\t' && c != '\n' && c != '\r') ctrl++;
                if (ctrl < Math.Max(1, s.Length) * 0.05) return 65001;
                break;
            } catch (DecoderFallbackException) { if (n == b.Length) break; }
        }
        string ln = name.ToLowerInvariant();
        if (ln.Contains("svg") || ln.Contains("xml") || ln.Contains("json") || ln.Contains("html") || ln.Contains("text") || ln.Contains("rtf")) return 65001;
        return 0;
    }

    public static string Decode(byte[] d, int cp) {
        string s = Encoding.GetEncoding(cp).GetString(d).TrimEnd('\0');
        return s.Replace("\r\n", "\n").Replace("\r", "\n").Replace("\n", "\r\n");
    }

    public static string HexDump(byte[] d, int max) {
        int end = Math.Min(d.Length, max);
        var sb = new StringBuilder((end / 16 + 1) * 80);
        const string hx = "0123456789abcdef";
        for (int i = 0; i < end; i += 16) {
            sb.Append(i.ToString("x8")).Append("  ");
            var asc = new StringBuilder(16);
            for (int j = 0; j < 16; j++) {
                if (i + j < end) {
                    byte c = d[i + j];
                    sb.Append(hx[c >> 4]).Append(hx[c & 15]).Append(' ');
                    asc.Append(c >= 0x20 && c < 0x7f ? (char)c : '.');
                } else sb.Append("   ");
                if (j == 7) sb.Append(' ');
            }
            sb.Append(" |").Append(asc).Append("|\r\n");
        }
        return sb.ToString();
    }

    static byte[] DibToBmp(byte[] d) {
        int hs = BitConverter.ToInt32(d, 0);
        int bpp = BitConverter.ToUInt16(d, 14);
        int comp = BitConverter.ToInt32(d, 16);
        int clr = hs >= 36 ? BitConverter.ToInt32(d, 32) : 0;
        int masks = hs == 40 ? (comp == 3 ? 12 : comp == 6 ? 16 : 0) : 0;
        int ncol = clr != 0 ? clr : (bpp <= 8 ? (1 << bpp) : 0);
        int off = 14 + hs + masks + ncol * 4;
        var bmp = new byte[14 + d.Length];
        bmp[0] = 0x42; bmp[1] = 0x4d;
        BitConverter.GetBytes(bmp.Length).CopyTo(bmp, 2);
        BitConverter.GetBytes(off).CopyTo(bmp, 10);
        d.CopyTo(bmp, 14);
        return bmp;
    }

    // 画像プレビュー用の Image を作る（失敗時は例外）
    public static Image MakeImage(ClipItem it) {
        switch (it.Kind) {
            case "emf":
                return new Metafile(new MemoryStream(it.Data));
            case "wmf": {
                var m = new METAFILEPICT();
                m.mm = it.MfMM; m.xExt = it.MfX; m.yExt = it.MfY;
                IntPtr h = SetWinMetaFileBits((uint)it.Data.Length, it.Data, IntPtr.Zero, ref m);
                if (h == IntPtr.Zero) throw new Exception("SetWinMetaFileBits 失敗");
                return new Metafile(h, true);
            }
            case "dib":
                return Image.FromStream(new MemoryStream(DibToBmp(it.Data)));
            case "raw":
                return Image.FromStream(new MemoryStream(it.Data));
        }
        return null;
    }
}
'@

# ---------------- Dump モード ----------------
if ($Dump) {
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    $items = [ClipNative]::ReadAll()
    "seq={0} ACP={1} formats={2}" -f [ClipNative]::GetSeq(), [ClipNative]::Acp, $items.Count
    $n = 0
    foreach ($it in $items) {
        $n++
        "[{0,2}] {1,5} 0x{1:x4}  {2,-32} {3,9} B  cp={4} kind={5} {6}" -f $n, $it.Id, $it.Name, $it.Data.Length, $it.CodePage, $it.Kind, $it.Note
        if ($it.Info) { "      " + ($it.Info -replace "`r`n", "`n      ") }
        if ($it.Kind) {
            try { $img = [ClipNative]::MakeImage($it); "      image: {0}x{1} {2}" -f $img.Width, $img.Height, $img.GetType().Name; $img.Dispose() }
            catch { "      image: 失敗 " + $_.Exception.InnerException.Message + $_.Exception.Message }
        }
    }
    return
}

# ---------------- GUI ----------------
if ($HideConsole) { [ClipNative]::HideConsole() }
[ClipNative]::DpiAware()
[Windows.Forms.Application]::EnableVisualStyles()

function Pick-Font([string[]]$names, [float]$size) {
    foreach ($n in $names) {
        $f = New-Object Drawing.Font($n, $size)
        if ($f.Name -eq $n) { return $f }
        $f.Dispose()
    }
    return New-Object Drawing.Font('MS Gothic', $size)
}
$uiFont   = Pick-Font @('Yu Gothic UI', 'Meiryo UI', 'Meiryo') 9.5
$monoFont = Pick-Font @('BIZ UDGothic', 'BIZ UDゴシック', 'MS Gothic') 10

$form = New-Object Windows.Forms.Form
$form.Text = 'Clipboard Type Viewer (PowerShell)'
$form.Font = $uiFont
$g = $form.CreateGraphics(); $S = $g.DpiX / 96; $g.Dispose()
$form.Size = New-Object Drawing.Size([int](1200 * $S), [int](860 * $S))
$form.StartPosition = 'CenterScreen'

function New-Bar {
    $p = New-Object Windows.Forms.FlowLayoutPanel
    $p.Dock = 'Top'; $p.AutoSize = $true; $p.WrapContents = $true; $p.Padding = New-Object Windows.Forms.Padding(4)
    return $p
}
function New-Btn($text) { $b = New-Object Windows.Forms.Button; $b.Text = $text; $b.AutoSize = $true; return $b }
function New-Lbl($text) {
    $l = New-Object Windows.Forms.Label; $l.Text = $text; $l.AutoSize = $true
    $l.Margin = New-Object Windows.Forms.Padding(3, 8, 3, 3); return $l
}
function New-Box([bool]$wrap) {
    $t = New-Object Windows.Forms.TextBox
    $t.Multiline = $true; $t.ReadOnly = $true; $t.WordWrap = $wrap; $t.Dock = 'Fill'
    $t.ScrollBars = if ($wrap) { 'Vertical' } else { 'Both' }
    $t.Font = $monoFont; $t.BackColor = [Drawing.SystemColors]::Window; $t.MaxLength = 0
    return $t
}

# --- 上部バー ---
$top = New-Bar
$btnReload = New-Btn '再読み込み'
$chkWatch = New-Object Windows.Forms.CheckBox
$chkWatch.Text = 'クリップボードの変化を監視して自動更新'; $chkWatch.Checked = $true; $chkWatch.AutoSize = $true
$chkWatch.Margin = New-Object Windows.Forms.Padding(10, 6, 3, 3)
$lblStatus = New-Lbl ''
$lblStatus.ForeColor = [Drawing.Color]::DimGray
$top.Controls.AddRange(@($btnReload, $chkWatch, $lblStatus))

# --- 下部ログ ---
$logPanel = New-Object Windows.Forms.Panel
$logPanel.Dock = 'Bottom'; $logPanel.Height = [int](190 * $S)
$logBar = New-Bar
$btnCopyLog = New-Btn 'ログをコピー'
$btnClearLog = New-Btn 'クリア'
$logBar.Controls.AddRange(@((New-Lbl 'イベントログ'), $btnCopyLog, $btnClearLog))
$txtLog = New-Box $true
$logPanel.Controls.Add($txtLog); $logPanel.Controls.Add($logBar); $txtLog.BringToFront()

# --- 中央: 一覧 + タブ ---
$split = New-Object Windows.Forms.SplitContainer
$split.Dock = 'Fill'

$lv = New-Object Windows.Forms.ListView
$lv.View = 'Details'; $lv.FullRowSelect = $true; $lv.HideSelection = $false; $lv.MultiSelect = $false; $lv.Dock = 'Fill'
foreach ($c in @(@('#', 36), @('ID', 110), @('Name', 210), @('Size', 80), @('種別', 120))) {
    [void]$lv.Columns.Add($c[0], [int]($c[1] * $S))
}
$split.Panel1.Controls.Add($lv)

$tabs = New-Object Windows.Forms.TabControl
$tabs.Dock = 'Fill'
$tabImg = New-Object Windows.Forms.TabPage '画像'
$tabTxt = New-Object Windows.Forms.TabPage 'テキスト'
$tabHex = New-Object Windows.Forms.TabPage 'Hex'
$tabInfo = New-Object Windows.Forms.TabPage '情報'
$tabs.TabPages.AddRange(@($tabImg, $tabTxt, $tabHex, $tabInfo))
$split.Panel2.Controls.Add($tabs)

# 画像タブ
$lblImg = New-Lbl ''
$lblImg.Dock = 'Top'; $lblImg.AutoSize = $false; $lblImg.Height = [int](26 * $S)
$pic = New-Object Windows.Forms.PictureBox
$pic.Dock = 'Fill'; $pic.BackColor = [Drawing.Color]::FromArgb(230, 230, 230)
$tabImg.Controls.Add($pic); $tabImg.Controls.Add($lblImg); $pic.BringToFront()

# テキストタブ
$txtBar = New-Bar
$cmbEnc = New-Object Windows.Forms.ComboBox
$cmbEnc.DropDownStyle = 'DropDownList'; $cmbEnc.Width = [int](170 * $S)
$encList = @('65001 utf-8', '1200 utf-16le', '1201 utf-16be', '932 shift_jis', '51932 euc-jp', '50220 iso-2022-jp', '1252 windows-1252', '437 ibm437')
[void]$cmbEnc.Items.AddRange($encList)
$chkWrap = New-Object Windows.Forms.CheckBox
$chkWrap.Text = '折り返し'; $chkWrap.Checked = $true; $chkWrap.AutoSize = $true
$chkWrap.Margin = New-Object Windows.Forms.Padding(10, 6, 3, 3)
$btnCopyText = New-Btn 'テキストをコピー'
$lblText = New-Lbl ''
$txtBar.Controls.AddRange(@((New-Lbl 'エンコーディング:'), $cmbEnc, $chkWrap, $btnCopyText, $lblText))
$txtText = New-Box $true
$tabTxt.Controls.Add($txtText); $tabTxt.Controls.Add($txtBar); $txtText.BringToFront()

# Hex タブ
$hexBar = New-Bar
$lblHex = New-Lbl ''
$btnHexAll = New-Btn 'すべて表示'
$btnCopyHex = New-Btn 'Hex をコピー'
$hexBar.Controls.AddRange(@($lblHex, $btnHexAll, $btnCopyHex))
$txtHex = New-Box $false
$tabHex.Controls.Add($txtHex); $tabHex.Controls.Add($hexBar); $txtHex.BringToFront()

# 情報タブ
$txtInfo = New-Box $true
$tabInfo.Controls.Add($txtInfo)

$form.Controls.Add($split); $form.Controls.Add($top); $form.Controls.Add($logPanel)
$split.BringToFront()

# ---------------- ロジック ----------------
$script:items = @()
$script:cur = $null
$script:lastSeq = 0
$script:suppressEnc = $false
$HEX_LIMIT = 64KB

function Log([string]$msg) {
    $txtLog.AppendText(('[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss.fff'), $msg) + "`r`n")
}

function Copy-Text([string]$text, [string]$what) {
    try {
        if ([string]::IsNullOrEmpty($text)) { Log "$what : 空なのでコピーしません"; return }
        [Windows.Forms.Clipboard]::SetText($text)
        $script:lastSeq = [ClipNative]::GetSeq()   # 自分のコピーでは自動更新しない
        Log ("{0} をクリップボードにコピー ({1} chars)" -f $what, $text.Length)
    } catch { Log "$what のコピー失敗: $($_.Exception.Message)" }
}

function Fmt-Size([long]$n) {
    if ($n -lt 1KB) { return "$n B" }
    if ($n -lt 1MB) { return ('{0:N1} KB' -f ($n / 1KB)) }
    return ('{0:N2} MB' -f ($n / 1MB))
}

function Fit-Picture {
    if (-not $pic.Image) { return }
    if ($pic.Image.Width -gt $pic.ClientSize.Width -or $pic.Image.Height -gt $pic.ClientSize.Height) { $pic.SizeMode = 'Zoom' }
    else { $pic.SizeMode = 'CenterImage' }
}

function Show-Text {
    $it = $script:cur
    if (-not $it -or $cmbEnc.SelectedIndex -lt 0) { return }
    $cp = [int](($cmbEnc.SelectedItem -split ' ')[0])
    try {
        $s = [ClipNative]::Decode($it.Data, $cp)
        $txtText.Text = $s
        $lblText.Text = ('{0:N0} 文字' -f $s.Length)
    } catch { $txtText.Text = "デコード失敗: $($_.Exception.Message)" }
}

function Show-Hex([int]$max) {
    $it = $script:cur
    $txtHex.Text = [ClipNative]::HexDump($it.Data, $max)
    $shown = [Math]::Min($it.Data.Length, $max)
    $lblHex.Text = ('{0:N0} / {1:N0} bytes 表示中' -f $shown, $it.Data.Length)
    $btnHexAll.Visible = $shown -lt $it.Data.Length
}

function Show-Item([int]$i) {
    $it = $script:items[$i]
    $script:cur = $it
    # 情報
    $info = @(
        "Name : $($it.Name)",
        ('ID   : {0} (0x{0:x4})' -f $it.Id),
        ('Size : {0:N0} bytes' -f $it.Data.Length),
        "種別 : $(if ($it.Kind) { $it.Kind } else { '-' }) / テキスト: $(if ($it.CodePage) { "cp$($it.CodePage)" } else { '-' })"
    )
    if ($it.Note) { $info += "備考 : $($it.Note)" }
    if ($it.Info) { $info += ''; $info += $it.Info }
    $txtInfo.Text = $info -join "`r`n"

    # 画像
    if ($pic.Image) { $old = $pic.Image; $pic.Image = $null; $old.Dispose() }
    $hasImg = $false
    if ($it.Kind) {
        try {
            $img = [ClipNative]::MakeImage($it)
            $pic.Image = $img; Fit-Picture; $hasImg = $true
            $lblImg.Text = '{0} × {1} px  ({2}: {3})' -f $img.Width, $img.Height, $it.Kind, $img.GetType().Name
            Log ("画像プレビュー OK: {0} {1}x{2}" -f $it.Name, $img.Width, $img.Height)
        } catch {
            $lblImg.Text = "画像プレビュー失敗: $($_.Exception.InnerException.Message) $($_.Exception.Message)"
            Log "画像プレビュー失敗: $($it.Name) $($_.Exception.Message)"
        }
    } else { $lblImg.Text = '(画像としてプレビューできないフォーマット)' }

    # テキスト
    $script:suppressEnc = $true
    if ($it.CodePage) {
        $idx = -1
        for ($k = 0; $k -lt $cmbEnc.Items.Count; $k++) { if ($cmbEnc.Items[$k] -like "$($it.CodePage) *") { $idx = $k } }
        if ($idx -lt 0) { $idx = $cmbEnc.Items.Add("$($it.CodePage) cp$($it.CodePage)") }
        $cmbEnc.SelectedIndex = $idx; $cmbEnc.Enabled = $true
        $script:suppressEnc = $false
        Show-Text
    } else {
        $script:suppressEnc = $false
        $cmbEnc.SelectedIndex = -1; $cmbEnc.Enabled = $true
        $txtText.Text = '(テキストとして表示できないフォーマットと判定。エンコーディングを選ぶと強制的にデコードします)'
        $lblText.Text = ''
    }

    # Hex
    Show-Hex $HEX_LIMIT

    $tabs.SelectedTab = if ($hasImg) { $tabImg } elseif ($it.CodePage) { $tabTxt } elseif ($it.Data.Length) { $tabHex } else { $tabInfo }
}

function Reload([string]$reason) {
    Log "--- 読み込み ($reason)"
    $seq = [ClipNative]::GetSeq()
    try { $script:items = [ClipNative]::ReadAll() }
    catch { Log "読み取り失敗: $($_.Exception.InnerException.Message) $($_.Exception.Message)"; return }
    $script:lastSeq = $seq
    $prevId = if ($script:cur) { $script:cur.Id } else { $null }
    $script:cur = $null
    $lv.BeginUpdate(); $lv.Items.Clear()
    $sel = 0; $n = 0
    foreach ($it in $script:items) {
        $row = New-Object Windows.Forms.ListViewItem ([string]($n + 1))
        [void]$row.SubItems.Add(('{0} (0x{0:x4})' -f $it.Id))
        [void]$row.SubItems.Add($it.Name)
        [void]$row.SubItems.Add((Fmt-Size $it.Data.Length))
        $kind = @()
        if ($it.Kind) { $kind += $it.Kind }
        if ($it.CodePage) { $kind += "text:cp$($it.CodePage)" }
        if (-not $it.Data.Length) { $kind += 'データなし' }
        [void]$row.SubItems.Add(($kind -join ' '))
        [void]$lv.Items.Add($row)
        if ($prevId -ne $null -and $it.Id -eq $prevId) { $sel = $n }
        $n++
    }
    $lv.EndUpdate()
    $lblStatus.Text = ('seq={0} / {1} フォーマット / {2} 更新' -f $seq, $n, (Get-Date -Format 'HH:mm:ss'))
    Log ("フォーマット数: {0} : {1}" -f $n, (($script:items | ForEach-Object { $_.Name }) -join ', '))
    if ($n) { $lv.Items[$sel].Selected = $true; $lv.Items[$sel].EnsureVisible() }
    else {
        $txtInfo.Text = 'クリップボードは空です。'; $txtText.Text = ''; $txtHex.Text = ''; $lblHex.Text = ''
        if ($pic.Image) { $old = $pic.Image; $pic.Image = $null; $old.Dispose() }
        $lblImg.Text = ''; $tabs.SelectedTab = $tabInfo
    }
}

# ---------------- イベント ----------------
$lv.add_SelectedIndexChanged({
    if ($lv.SelectedIndices.Count -gt 0) {
        try { Show-Item $lv.SelectedIndices[0] } catch { Log "表示エラー: $($_.Exception.Message)" }
    }
})
$cmbEnc.add_SelectedIndexChanged({ if (-not $script:suppressEnc) { Show-Text } })
$chkWrap.add_CheckedChanged({ $txtText.WordWrap = $chkWrap.Checked; $txtText.ScrollBars = if ($chkWrap.Checked) { 'Vertical' } else { 'Both' } })
$btnCopyText.add_Click({ Copy-Text $txtText.Text "テキスト($($script:cur.Name))" })
$btnHexAll.add_Click({
    if ($script:cur.Data.Length -gt 4MB) {
        $r = [Windows.Forms.MessageBox]::Show('大きいデータです。すべて表示しますか？', 'Hex', 'YesNo')
        if ($r -ne 'Yes') { return }
    }
    Show-Hex ([int]::MaxValue)
})
$btnCopyHex.add_Click({ Copy-Text $txtHex.Text "Hex($($script:cur.Name))" })
$btnReload.add_Click({ Reload 'ボタン' })
$btnCopyLog.add_Click({ Copy-Text $txtLog.Text 'ログ' })
$btnClearLog.add_Click({ $txtLog.Clear() })
$pic.add_Resize({ Fit-Picture })

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 1000
$timer.add_Tick({
    if ($chkWatch.Checked) {
        $s = [ClipNative]::GetSeq()
        if ($s -ne $script:lastSeq) { Reload "変化検出 seq=$s" }
    }
})

$form.add_Shown({
    # ショートカットの「最小化」指定などが引き継がれても通常表示で前面に出す
    $form.WindowState = 'Normal'; $form.Activate()
    $split.SplitterDistance = [int](560 * $S)
    Log ("起動: PowerShell {0} / DPI scale {1} / ACP {2}" -f $PSVersionTable.PSVersion, $S, [ClipNative]::Acp)
    Reload '起動時'
    $timer.Start()
})
$form.add_FormClosed({ $timer.Stop() })

[void]$form.ShowDialog()
