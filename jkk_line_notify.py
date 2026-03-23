#!/usr/bin/env python3
"""
JKK空き家状況の増加監視 + LINE Messaging API通知

- 事前にトップページへアクセスしてCookieを取得後、対象ページを取得
- 前回より件数が増えた/新規物件が出現/内訳(号室)が入れ替わった場合のみ通知
- メンテナンス(おわび)時は例外停止せずログ出力して終了
"""

from __future__ import annotations

import hashlib
import html as html_module
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HOME_URL = "https://www.to-kousya.or.jp/"
# 賃貸ポータル（Referer 用・ウォームアップ用）
CHINTAI_URL = "https://www.to-kousya.or.jp/chintai/reco/index.html"
# jhomes のルート https://jhomes.to-kousya.or.jp/ は 404 のため使わない
JH_WARMUP_URL = "https://jhomes.to-kousya.or.jp/search/jkknet/service/akiyaJyoukenStartInit"
# 一覧取得先（空き家条件の検索結果は akiyaJyokenDirect。変更数だけ見る場合は AKIYAchangeCount 等）
_DEFAULT_TARGET_URL = (
    "https://jhomes.to-kousya.or.jp/search/jkknet/service/akiyaJyokenDirect"
)
TARGET_URL = os.getenv("JKK_TARGET_URL", _DEFAULT_TARGET_URL).strip()
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

# 詳細ページ URL（senPage の例: ('','L8851','1280950','0000') → p2=行コード, p3=団地コード, p4=サブ）
# 既定は「…/view?danchi=&room=」形式（実サイトのパスが違う場合は JKK_DETAIL_VIEW_BASE 等で上書き）
_DEFAULT_DETAIL_VIEW_BASE = "https://jhomes.to-kousya.or.jp/search/jkknet/view"
_DEFAULT_DETAIL_QUERY_TEMPLATE = "danchi={p3}&room={p2}"

# 未指定時はこのスクリプト（jkk_line_notify.py）と同じフォルダに保存。
# Colab / GitHub Actions など別パスにしたい場合は JKK_DATA_DIR を設定。
_SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("JKK_DATA_DIR", str(_SCRIPT_DIR))).resolve()
LAST_DATA_FILE = DATA_DIR / "last_data.json"        # 物件名 -> 合算件数
LAST_ROOMS_FILE = DATA_DIR / "last_rooms.json"      # 入れ替わり検知用ハッシュ
LAST_DETAIL_FILE = DATA_DIR / "last_rooms_detail.json"  # 間取り別件数

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": HOME_URL,
}


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_with_session() -> tuple[str, requests.Session, str] | tuple[None, None, None]:
    """
    人間偽装:
    1) to-kousyaトップへアクセスしてCookie取得
    2) そのSessionで対象ページを取得
    戻り値: (html, session, final_url) または (None, None, None)
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        warmup = session.get(HOME_URL, timeout=30)
        warmup.raise_for_status()
    except requests.RequestException as exc:
        print(f"[WARN] 事前アクセス失敗: {exc}")
        return None, None, None

    # 公社の賃貸案内 → jhomes 側の入口（404 にならない URL）
    try:
        session.get(CHINTAI_URL, timeout=30).raise_for_status()
    except requests.RequestException as exc:
        print(f"[WARN] 賃貸案内ページ取得失敗（続行）: {exc}")

    warmup_jh = os.getenv("JKK_JHOMES_WARMUP_URL", JH_WARMUP_URL).strip()
    if warmup_jh:
        try:
            session.get(warmup_jh, timeout=30).raise_for_status()
        except requests.RequestException as exc:
            print(f"[WARN] jhomes ウォームアップ取得失敗（続行）: {exc}")

    try:
        res = session.get(
            TARGET_URL,
            timeout=30,
            headers={**HEADERS, "Referer": CHINTAI_URL},
        )
        res.raise_for_status()
        raw_len = len(res.content or b"")
        ct = (res.headers.get("Content-Type") or "").split(";")[0].strip()
        print(f"[INFO] 取得: 最終URL={res.url} サイズ={raw_len} bytes Content-Type={ct}")
        html1 = decode_html_response(res)
        # akiyaJyokenDirect のような「数秒後に次の画面」中間ページ対策
        html2 = maybe_follow_transition(session, res.url, html1)
        final_html = html2 or html1
        final_url = res.url
        return final_html, session, final_url
    except requests.RequestException as exc:
        print(f"[WARN] ターゲット取得失敗: {exc}")
        return None, None, None


def _html_has_list_markers(text: str) -> bool:
    """
    ヘッダが HTML 実体参照（&#...;）のみの場合、デコード直後の文字列には
    「住宅名」が無くマーカー判定に失敗するため、unescape 後も見る。
    """
    u = html_module.unescape(text)
    markers = (
        "住宅名",
        "募集戸数",
        "物件名",
        "空き家",
        "cell666666",
        "senPage",
        # 「サービス名文字列」だけは中間ページにも出るので、一覧判定に使わない
        # "jkknet", "AKIYAchangeCount", "akiyaJyokenDirect"
    )
    return any(m in text or m in u for m in markers)


def decode_html_response(res: requests.Response) -> str:
    """
    JKK は Shift_JIS / CP932 のことが多く、apparent_encoding だけだと文字化けして
    「住宅名」「募集戸数」が一致せずパース0件になることがある。
    マーカー文字列が読めるデコード結果を優先する。
    """
    raw = res.content or b""
    seen: list[str] = []
    best_guess: str | None = None
    # CP932/Shift_JIS を先に試す（JKK は Shift_JIS が多く、apparent_encoding が
    # ISO-8859-1 等を誤検出すると HTML 実体参照経由でマーカーが偽ヒットするため）
    for enc in ("cp932", "shift_jis", res.apparent_encoding, "euc_jp", "utf-8"):
        if not enc or enc in seen:
            continue
        seen.append(enc)
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if _html_has_list_markers(text):
            return text
        if best_guess is None and ("<html" in text.lower() or "<table" in text.lower()):
            best_guess = text
    if best_guess is not None:
        return best_guess
    try:
        return raw.decode("cp932", errors="replace")
    except LookupError:
        try:
            return raw.decode(seen[0] if seen else "utf-8", errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")


def extract_redirect_urls(html: str, base_url: str) -> list[str]:
    """
    「数秒後に自動で次の画面」系の中間ページから遷移先URL候補を抽出する。
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []

    # meta refresh
    meta = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
    if meta and meta.get("content"):
        m = re.search(r"url\s*=\s*([^;]+)", meta["content"], re.I)
        if m:
            out.append(urljoin(base_url, m.group(1).strip()))

    # 「こちら」をクリックするリンク（href が # の場合も onclick に遷移先があることがある）
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        at = a.get_text(" ", strip=True)
        if not href or href == "#":
            # href が # の場合でも onclick 内に URL があるかもしれない
            onclick = (a.get("onclick") or "").strip()
            if onclick:
                for m in re.finditer(r"(https?://[^'\"\s>]+)", onclick):
                    out.append(m.group(1))
                # senPage(...) 内や location.href(...) 内の文字列など
                for m in re.finditer(r"(location\.href|window\.location|document\.location|replace)\s*[^'\"]*['\"]([^'\"]+)['\"]", onclick, re.I):
                    out.append(urljoin(base_url, m.group(2)))
            continue
        if "こちら" in at or "クリック" in at or "次の画面" in at:
            out.append(urljoin(base_url, href))

    # script 内の location.href 等
    script_text = " ".join(s.get_text(" ", strip=True) for s in soup.find_all("script")[:20])
    for pat in (
        r"location\.href\s*=\s*['\"]([^'\"]+)['\"]",
        r"window\.location\s*=\s*['\"]([^'\"]+)['\"]",
        r"document\.location\s*=\s*['\"]([^'\"]+)['\"]",
        r"location\.replace\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
    ):
        for m in re.finditer(pat, script_text, re.I):
            out.append(urljoin(base_url, m.group(1)))

    # URL断片
    for m in re.finditer(r"(https?://[^'\"\s>]+)", html):
        out.append(m.group(1))
    for m in re.finditer(r"(/search/jkknet/[^'\"\s>]+)", html):
        out.append(urljoin(base_url, m.group(1)))

    # 重複除去（順序維持）
    uniq: list[str] = []
    seen: set[str] = set()
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def try_submit_first_form(
    session: requests.Session,
    base_url: str,
    html: str,
) -> str | None:
    """
    「数秒後に自動で次の画面」系の中間ページが、内部的に <form> を自動送信している場合のフォールバック。
    GET/POST を試し、一覧行を取れれば返す。
    """
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.find_all("form")
    if not forms:
        return None

    for form in forms[:3]:
        action = (form.get("action") or "").strip()
        if not action:
            action_url = base_url
        else:
            action_url = urljoin(base_url, action)

        method = (form.get("method") or "get").strip().lower()
        data: dict[str, str] = {}

        for inp in form.find_all("input"):
            name = (inp.get("name") or "").strip()
            if not name:
                continue
            val = inp.get("value")
            if val is None:
                val = ""
            data[name] = str(val)

        try:
            print(f"[INFO] フォーム送信を試行: method={method} url={action_url} params={len(data)}")
            if method == "post":
                r = session.post(
                    action_url,
                    data=data,
                    timeout=30,
                    headers={**HEADERS, "Referer": base_url},
                )
            else:
                r = session.get(
                    action_url,
                    params=data,
                    timeout=30,
                    headers={**HEADERS, "Referer": base_url},
                )
            r.raise_for_status()
            html2 = decode_html_response(r)
            props = parse_properties(html2)
            if props:
                return html2
        except requests.RequestException:
            continue
        except Exception:
            continue

    return None


def maybe_follow_transition(
    session: requests.Session,
    base_url: str,
    html: str,
    max_depth: int = 3,
    visited: set[str] | None = None,
) -> str | None:
    """
    自動遷移中間ページの場合のみ次画面を取りに行く。
    """
    if visited is None:
        visited = set()

    def norm(u: str) -> str:
        return (u or "").rstrip("/")

    base_norm = norm(base_url)
    if base_norm in visited:
        return None
    visited.add(base_norm)

    u = html_module.unescape(html)
    transition_markers = (
        "数秒後に自動で次の画面",
        "しばらくたっても表示されない場合",
        "自動で次の画面",
    )
    if not any(m in u for m in transition_markers):
        return None

    # 既に一覧マーカーがあれば追わない
    if _html_has_list_markers(html):
        return None

    candidates = extract_redirect_urls(html, base_url)
    # 優先度: jkknet 領域。さらに画像/スタイル/JS等を除外して“次画面候補”に寄せる。
    def is_likely_next_page(u: str) -> bool:
        low = u.lower()
        if any(x in low for x in ("/images/", "images/", ".gif", ".css", ".js", ".png", ".jpg", ".jpeg", ".webp")):
            return False
        # list 本体は jsp/view/service 付近に出ることが多いのでそれを優先
        if "search/jkknet/" in low and (".jsp" in low or "service/" in low or "view" in low or "result" in low):
            return True
        # それ以外でも jkknet 内のURLなら一応候補にする
        return "search/jkknet/" in low

    candidates = [c for c in candidates if is_likely_next_page(c)]
    # 元URL自身や、すでに辿ったURLは再試行しない
    candidates = [c for c in candidates if norm(c) != base_norm and norm(c) not in visited]
    # 優先度: jkknet 領域
    candidates = sorted(candidates, key=lambda x: (0 if "search/jkknet" in x else 1, len(x)))
    print(f"[INFO] 自動遷移候補: {len(candidates)} 件")
    if candidates:
        print(f"[INFO] 自動遷移候補（先頭）: {candidates[:3]}")
    else:
        # URL候補が取れない場合は、formの自動送信フォールバックを試す
        next_html = try_submit_first_form(session, base_url, html)
        if next_html:
            return next_html

    for cand in candidates[:5]:
        try:
            cand_norm = norm(cand)
            if cand_norm in visited or cand_norm == base_norm:
                continue
            print(f"[INFO] 自動遷移先を試行: {cand}")
            r2 = session.get(
                cand,
                timeout=30,
                headers={**HEADERS, "Referer": base_url},
            )
            r2.raise_for_status()
            raw_len = len(r2.content or b"")
            ct = (r2.headers.get("Content-Type") or "").split(";")[0].strip()
            print(f"[INFO] 遷移後: 最終URL={r2.url} サイズ={raw_len} bytes Content-Type={ct}")
            html2 = decode_html_response(r2)
            # ここでパース確認（行が取れるなら採用）
            if parse_properties(html2):
                return html2
            # wait.jsp のように“さらに待ち”が必要な多段遷移も追う
            if max_depth > 0:
                u2 = html_module.unescape(html2)
                if any(m in u2 for m in transition_markers):
                    next_html = maybe_follow_transition(
                        session,
                        r2.url,
                        html2,
                        max_depth=max_depth - 1,
                        visited=visited,
                    )
                    if next_html:
                        return next_html
        except requests.RequestException:
            continue
        except Exception:
            continue
    return None


def is_maintenance_page(html: str) -> bool:
    """
    「おわび」やメンテナンス表記を検知。
    タイトル等が HTML 実体参照のみだと「おわび」が平文で無いことがあるため unescape も見る。
    画像ファイル名（ASCII）は実体参照に依存しない目印として使う。
    """
    u = html_module.unescape(html)
    blob = html + "\n" + u
    lowered = blob.lower()
    keywords = ["おわび", "メンテナンス", "maintenance", "service unavailable"]
    if any(k in lowered for k in keywords):
        return True
    if "jkkねっと：おわび" in blob:
        return True
    # おわび画面で使われる画像（文字コードに依存しない）
    if "owabimoji8.gif" in html or "owabi_homuzu.gif" in html:
        return True
    # タイトル簡易判定
    m = re.search(r"<title[^>]*>(.*?)</title>", lowered, re.DOTALL)
    if m and ("おわび" in m.group(1) or "maintenance" in m.group(1)):
        return True
    return False


def parse_count(text: str) -> int | None:
    m = re.search(r"(\d+)", text.replace(",", ""))
    return int(m.group(1)) if m else None


def pick_col_idx(
    headers: list[str],
) -> tuple[int | None, int | None, int | None]:
    """
    戻り値: (物件名/住宅名列, 号室または間取り列, 件数列)
    - AKIYAchangeCount 一覧は「住宅名」「間取り」「募集戸数」形式
    - 旧形式は「物件名」「号室」「空き家数」等も許容
    """
    idx_name = idx_sub = idx_count = None
    for i, h in enumerate(headers):
        if "物件名" in h or "住宅名" in h:
            idx_name = i
        if "号室" in h or h == "室":
            idx_sub = i
        if "間取り" in h and idx_sub is None:
            idx_sub = i
        if "募集戸数" in h:
            idx_count = i
        if ("空き家" in h or "空室" in h) and ("現在" in h or "件数" in h):
            idx_count = i
    if idx_count is None:
        for i, h in enumerate(headers):
            if "空き家" in h or "空室" in h:
                idx_count = i
                break
    return idx_name, idx_sub, idx_count


def extract_senpage_args(onclick: str) -> tuple[str, str, str, str] | None:
    """onclick 内の senPage('a','b','c','d') またはダブルクォート版から4引数を取り出す。"""
    onclick = onclick or ""
    patterns = (
        r"senPage\s*\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
        r'senPage\s*\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)',
    )
    for pat in patterns:
        m = re.search(pat, onclick)
        if m:
            return (m.group(1), m.group(2), m.group(3), m.group(4))
    return None


def get_detail_url_template() -> str:
    """
    詳細 URL の組み立て用テンプレートを返す。

    優先順位:
    1) JKK_DETAIL_URL_TEMPLATE … 完全な URL 文字列。{p1}{p2}{p3}{p4} を置換。
    2) JKK_DETAIL_VIEW_BASE + JKK_DETAIL_QUERY_TEMPLATE … ? 以降のみ差し替え。
    3) 内蔵デフォルト … {view_base}?danchi={{p3}}&room={{p2}}

    senPage 4引数の意味（一覧の onclick 実例に基づく想定）:
    - p1: 第1引数（空のことが多い）
    - p2: 行・タイプコード（例: L8851）→ 既定では room クエリに割当
    - p3: 団地・建物コード（例: 1280950）→ 既定では danchi クエリに割当
    - p4: サブコード（例: 0000）→ クエリに含めたい場合はテンプレートで {p4} を指定
    """
    explicit = os.getenv("JKK_DETAIL_URL_TEMPLATE", "").strip()
    if explicit:
        return explicit

    base = os.getenv("JKK_DETAIL_VIEW_BASE", "").strip().rstrip("/")
    if not base:
        base = _DEFAULT_DETAIL_VIEW_BASE.rstrip("/")

    query = os.getenv("JKK_DETAIL_QUERY_TEMPLATE", "").strip()
    if not query:
        query = _DEFAULT_DETAIL_QUERY_TEMPLATE

    if "?" in base:
        return f"{base}&{query}"
    return f"{base}?{query}"


def format_detail_url(args: tuple[str, str, str, str]) -> str:
    """senPage 4引数をテンプレートに埋め込む（未使用のプレースホルダはそのまま残さないよう全員渡す）。"""
    tmpl = get_detail_url_template()
    return tmpl.format(p1=args[0], p2=args[1], p3=args[2], p4=args[3])


def build_detail_url_from_row(tr) -> str:
    """
    詳細ボタンは href が空で senPage(...) のことが多い。
    get_detail_url_template() で得たテンプレートに {p1}〜{p4} を埋め込む。
    senPage が取れない場合は空き家検索の入口 URL を返す。
    """
    a = tr.find("a", onclick=True)
    if not a:
        return CHINTAI_URL
    args = extract_senpage_args(a.get("onclick") or "")
    if not args:
        href = (a.get("href") or "").strip()
        return urljoin(TARGET_URL, href) if href else CHINTAI_URL
    return format_detail_url(args)


def _header_row_and_indices(
    tr_list: list,
) -> tuple[int, int, int, int] | None:
    """
    先頭数行のどれかがヘッダ行（住宅名+募集戸数 等）のケースに対応。
    戻り値: (header_row_index, idx_name, idx_sub, idx_count) または None
    """
    max_scan = min(6, len(tr_list))
    for hi in range(max_scan):
        header_cells = tr_list[hi].find_all(["th", "td"])
        headers = [html_module.unescape(c.get_text(strip=True)) for c in header_cells]
        idx_name, idx_sub, idx_count = pick_col_idx(headers)
        if idx_name is not None and idx_count is not None:
            return (hi, idx_name, idx_sub, idx_count)
    return None


def parse_properties(html: str) -> list[dict[str, Any]]:
    """
    対象HTMLから最低限以下を抽出:
    - 物件名（住宅名）
    - 号室または間取り（行の識別・入れ替わり検知用）
    - 現在の募集戸数 / 空き家数
    - 詳細URL（senPage または href）
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []

    # 実ページは table.cell666666（ヘッダが td）のことが多い
    tables = soup.select("table.cell666666")
    if not tables:
        tables = soup.find_all("table")

    for table in tables:
        tr_list = table.find_all("tr")
        if len(tr_list) < 2:
            continue

        parsed = _header_row_and_indices(tr_list)
        if not parsed:
            continue
        hi, idx_name, idx_sub, idx_count = parsed

        for tr in tr_list[hi + 1 :]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= max(idx_name, idx_count):
                continue

            name = cells[idx_name].get_text(" ", strip=True)
            sub = (
                cells[idx_sub].get_text(" ", strip=True)
                if idx_sub is not None and idx_sub < len(cells)
                else "-"
            )
            count = parse_count(cells[idx_count].get_text(" ", strip=True))
            if not name or count is None:
                continue

            detail_url = build_detail_url_from_row(tr)
            a = tr.find("a", onclick=True)
            sen_args = extract_senpage_args((a.get("onclick") or "")) if a else None

            rows.append(
                {
                    "name": name,
                    "room": sub or "-",
                    "count": count,
                    "detail_url": detail_url,
                    "senpage": ",".join(sen_args) if sen_args else "",
                }
            )

        if rows:
            return rows

    fixed = parse_properties_cell666666_fixed(soup)
    if fixed:
        return fixed

    return rows


def parse_properties_cell666666_fixed(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """
    ヘッダ文字列が実体参照・画像のみ等で pick_col_idx に失敗したときのフォールバック。
    実ページの列順: 0外観 1住宅名 2地域 … 5間取り … 9募集戸数
    """
    out: list[dict[str, Any]] = []
    for table in soup.select("table.cell666666"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 10:
                continue
            name = html_module.unescape(cells[1].get_text(" ", strip=True))
            sub = html_module.unescape(cells[5].get_text(" ", strip=True))
            count = parse_count(cells[9].get_text(" ", strip=True))
            if not name or count is None:
                continue
            if name == "住宅名" or "募集戸数" in name:
                continue

            detail_url = build_detail_url_from_row(tr)
            a = tr.find("a", onclick=True)
            sen_args = extract_senpage_args((a.get("onclick") or "")) if a else None

            out.append(
                {
                    "name": name,
                    "room": sub or "-",
                    "count": count,
                    "detail_url": detail_url,
                    "senpage": ",".join(sen_args) if sen_args else "",
                }
            )
    return out


def log_parse_failure_hint(html: str) -> None:
    """パース0件時のヒント（文字化け・構造・JSのみ等）。"""
    u = html_module.unescape(html)
    low = html.lower()
    if "cloudflare" in low or "cf-ray" in low:
        print("[HINT] Cloudflare 等のブロック画面の可能性があります（Colab の出口IPが弾かれている等）。")
    if "403" in html or "forbidden" in low:
        print("[HINT] 403 / Forbidden の文言があります。アクセス拒否を疑ってください。")
    if "senPage" in html and "cell666666" in html:
        print("[HINT] 一覧らしき表はあるがデータ行の解析に失敗しています。列名変更の可能性があります。")
    elif "住宅名" not in u and "募集戸数" not in u:
        print(
            "[HINT] HTML内に「住宅名」「募集戸数」（実体参照展開後も）がありません。"
            "リダイクト先がログイン画面・エラーHTML・ブロック画面の可能性があります。"
            " [INFO] の最終URLと jkk_notify_debug.html を確認してください。"
        )
    elif "<table" not in low:
        print("[HINT] <table> がありません。JavaScript描画のみのページの可能性があります。")


def save_debug_html(html: str) -> Path:
    path = DATA_DIR / "jkk_notify_debug.html"
    path.write_text(html, encoding="utf-8", errors="replace")
    return path


def _collect_form_data(soup: BeautifulSoup, max_showcount: bool = False) -> tuple[dict[str, str], str | None]:
    """
    frmMain フォームの全フィールド値を収集して返す。
    max_showcount=True のとき akiyaRefRM.showCount を最大値に設定する。
    戻り値: (form_data, form_name)
    """
    form = soup.find("form", {"name": "frmMain"}) or soup.find("form")
    if not form:
        return {}, None
    form_name = form.get("name")
    data: dict[str, str] = {}
    for tag in form.find_all(["input", "select"]):
        name = (tag.get("name") or "").strip()
        if not name:
            continue
        if tag.name == "select":
            if max_showcount and name == "akiyaRefRM.showCount":
                # 最大オプション値を取得
                options = tag.find_all("option")
                if options:
                    vals = [o.get("value", "") for o in options if o.get("value")]
                    data[name] = max(vals, key=lambda v: int(v) if v.isdigit() else 0)
                else:
                    data[name] = tag.get("value") or ""
            else:
                selected = tag.find("option", selected=True) or (tag.find("option") or None)
                data[name] = selected.get("value", "") if selected else ""
        else:
            data[name] = str(tag.get("value") or "")
    return data, form_name


def try_get_all_with_showcount(
    session: requests.Session,
    html: str,
    base_url: str,
) -> list[dict[str, Any]]:
    """
    akiyaRefRM.showCount を最大値（50）にして AKIYAchangeCount へ POST し、
    全件を1ページで取得を試みる。成功した行リストを返す。失敗時は空リスト。
    """
    soup = BeautifulSoup(html, "html.parser")
    data, _ = _collect_form_data(soup, max_showcount=True)
    if "akiyaRefRM.showCount" not in data:
        return []
    count_url = urljoin(base_url, "AKIYAchangeCount")
    try:
        print(f"[INFO] showCount={data['akiyaRefRM.showCount']} で全件取得を試行: {count_url}")
        r = session.post(
            count_url,
            data=data,
            timeout=30,
            headers={**HEADERS, "Referer": base_url},
        )
        r.raise_for_status()
        html2 = decode_html_response(r)
        rows = parse_properties(html2)
        print(f"[INFO] showCount 全件取得結果: {len(rows)} 件")
        return rows
    except requests.RequestException as exc:
        print(f"[WARN] showCount 全件取得失敗: {exc}")
        return []


def extract_paging_form_requests(
    html: str, base_url: str
) -> list[tuple[str, dict[str, str]]]:
    """
    JKK の JavaScript ページネーション
    `movePagingInputGridPageAbs('pageNum', 'HASH')` を解析し、
    各追加ページを取得するための (action_url, form_data) のリストを返す。
    action URL は submitAction の動作に従い service/ 配下に解決する。
    """
    soup = BeautifulSoup(html, "html.parser")
    base_data, _ = _collect_form_data(soup)

    # service/ 配下の AKIYApageNum エンドポイント
    service_base = re.sub(r"/[^/]+$", "/", base_url)  # 末尾のパスセグメントを除去
    page_action_url = urljoin(service_base, "AKIYApageNum")

    pat = re.compile(
        r"movePagingInputGridPageAbs\s*\(\s*'(\w+)'\s*,\s*'([^']+)'\s*\)", re.I
    )
    seen: set[str] = set()
    result: list[tuple[str, dict[str, str]]] = []
    for a in soup.find_all("a"):
        m = pat.search(a.get("onclick") or "")
        if m:
            field, hash_val = m.group(1), m.group(2)
            if hash_val not in seen:
                seen.add(hash_val)
                result.append((page_action_url, {**base_data, field: hash_val}))
    return result


def extract_next_page_url(html: str, base_url: str) -> str | None:
    """
    ページネーションの「次の○件」「次ページ」リンクを探して URL を返す。
    見つからない場合は None。
    """
    soup = BeautifulSoup(html, "html.parser")
    next_keywords = ("次の", "次ページ", "次へ", "&#27425;", "&gt;&gt;", ">>", "＞＞")
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href == "#":
            continue
        text = html_module.unescape(a.get_text(" ", strip=True))
        if any(k in text for k in next_keywords):
            return urljoin(base_url, href)
    return None


def collect_all_rows(
    session: requests.Session,
    first_html: str,
    first_url: str,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """
    1ページ目の HTML からページネーションを辿り、全ページの行を結合して返す。
    href ベースのリンクが無い場合は JKK の JavaScript フォームページネーションを使う。
    """
    all_rows: list[dict[str, Any]] = []
    html = first_html
    url = first_url
    visited: set[str] = set()

    for page_num in range(1, max_pages + 1):
        norm_url = url.rstrip("/")
        if norm_url in visited:
            break
        visited.add(norm_url)

        rows = parse_properties(html)
        print(f"[INFO] ページ {page_num}: {len(rows)} 件取得 ({url})")
        all_rows.extend(rows)

        # href ベースの次ページを優先
        next_url = extract_next_page_url(html, url)
        if next_url and next_url.rstrip("/") not in visited:
            try:
                res = session.get(
                    next_url,
                    timeout=30,
                    headers={**HEADERS, "Referer": url},
                )
                res.raise_for_status()
                html = decode_html_response(res)
                url = res.url
                continue
            except requests.RequestException as exc:
                print(f"[WARN] ページ {page_num + 1} 取得失敗（ここまでの結果を使用）: {exc}")
                break

        # JKK 形式: 1ページ目のみ追加ページを処理
        if page_num == 1:
            # 優先: showCount=50 で全件を1回のリクエストで取得
            full_rows = try_get_all_with_showcount(session, html, url)
            if full_rows:
                # 1ページ目の行を置き換えて全件採用
                all_rows = full_rows
                break

            # フォールバック: movePagingInputGridPageAbs でページ別取得
            paging_requests = extract_paging_form_requests(html, url)
            if paging_requests:
                print(f"[INFO] フォームページネーション: 追加 {len(paging_requests)} ページを取得")
                for i, (action_url, form_data) in enumerate(paging_requests, start=2):
                    try:
                        res2 = session.post(
                            action_url,
                            data=form_data,
                            timeout=30,
                            headers={**HEADERS, "Referer": url},
                        )
                        res2.raise_for_status()
                        html2 = decode_html_response(res2)
                        rows2 = parse_properties(html2)
                        print(f"[INFO] ページ {i}: {len(rows2)} 件取得")
                        all_rows.extend(rows2)
                    except requests.RequestException as exc:
                        print(f"[WARN] ページ {i} 取得失敗: {exc}")
            break

        break  # それ以上のページなし

    print(f"[INFO] 全ページ合計: {len(all_rows)} 行")
    return all_rows


def build_property_map(rows: list[dict[str, Any]]) -> dict[str, int]:
    """
    要件に合わせて「物件名: 件数」の辞書を作る。
    同一住宅名が間取り別に複数行ある場合は募集戸数を合算する。
    """
    result: dict[str, int] = {}
    for r in rows:
        n = str(r["name"])
        c = int(r["count"])
        result[n] = result.get(n, 0) + c
    return result


def build_room_fingerprint(rows: list[dict[str, Any]]) -> dict[str, str]:
    """
    30→30 のような件数同一の入れ替わりを検知するため、
    物件ごとに「間取り/号室 + 募集戸数 + senPage」の行単位シグネチャをハッシュする。
    """
    bucket: dict[str, list[str]] = {}
    for r in rows:
        sig = f"{r['room']}|{r['count']}|{r.get('senpage', '')}"
        bucket.setdefault(r["name"], []).append(sig)

    fingerprint: dict[str, str] = {}
    for name, sigs in bucket.items():
        norm = "||".join(sorted(sigs))
        fingerprint[name] = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return fingerprint


def build_room_detail_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """
    間取り別件数を物件ごとに集計する。
    戻り値: { "物件名": { "間取り": 件数, ... }, ... }
    """
    result: dict[str, dict[str, int]] = {}
    for r in rows:
        name = str(r["name"])
        room = str(r["room"] or "-")
        count = int(r["count"])
        result.setdefault(name, {})[room] = result.get(name, {}).get(room, 0) + count
    return result


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def detect_changes(
    current_map: dict[str, int],
    prev_map: dict[str, int],
    current_fp: dict[str, str],
    prev_fp: dict[str, str],
    current_detail: dict[str, dict[str, int]],
    prev_detail: dict[str, dict[str, int]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    通知対象:
    - 前回より件数が増えた
    - 前回存在しなかった物件が出現
    - 件数同一でも号室内訳が変化（入れ替わり）
    各エントリに間取り別の現在件数と前回件数を含める。
    """
    url_by_name: dict[str, str] = {}
    for r in rows:
        url_by_name.setdefault(r["name"], r["detail_url"])

    notices: list[dict[str, Any]] = []
    for name, now_count in current_map.items():
        prev_count = prev_map.get(name)
        cur_rooms = current_detail.get(name, {})
        prv_rooms = prev_detail.get(name, {})

        if prev_count is None:
            notices.append({
                "name": name,
                "increase": now_count,
                "current_total": now_count,
                "reason": "new",
                "detail_url": url_by_name.get(name, CHINTAI_URL),
                "cur_rooms": cur_rooms,
                "prv_rooms": {},
            })
            continue

        if now_count > prev_count:
            notices.append({
                "name": name,
                "increase": now_count - prev_count,
                "current_total": now_count,
                "reason": "increase",
                "detail_url": url_by_name.get(name, CHINTAI_URL),
                "cur_rooms": cur_rooms,
                "prv_rooms": prv_rooms,
            })
            continue

        if now_count == prev_count and current_fp.get(name) != prev_fp.get(name):
            notices.append({
                "name": name,
                "increase": 0,
                "current_total": now_count,
                "reason": "rotation",
                "detail_url": url_by_name.get(name, CHINTAI_URL),
                "cur_rooms": cur_rooms,
                "prv_rooms": prv_rooms,
            })

    return notices


def _build_change_block(c: dict[str, Any]) -> str:
    """1件分の変化テキストブロックを生成する。"""
    reason_label = {"new": "新規掲載", "increase": "増加", "rotation": "内訳変更"}.get(
        c["reason"], c["reason"]
    )
    cur: dict[str, int] = c.get("cur_rooms", {})
    prv: dict[str, int] = c.get("prv_rooms", {})
    all_rooms = sorted(set(cur) | set(prv))
    room_lines: list[str] = []
    for room in all_rooms:
        now_cnt = cur.get(room, 0)
        prv_cnt = prv.get(room, 0)
        if prv_cnt == 0 and now_cnt > 0:
            room_lines.append(f"  {room}: {now_cnt}戸 ★新規")
        elif now_cnt > prv_cnt:
            room_lines.append(f"  {room}: {now_cnt}戸（前回: {prv_cnt}戸 +{now_cnt - prv_cnt}）")
        elif now_cnt < prv_cnt:
            room_lines.append(f"  {room}: {now_cnt}戸（前回: {prv_cnt}戸 -{prv_cnt - now_cnt}）")
        elif now_cnt > 0:
            room_lines.append(f"  {room}: {now_cnt}戸")

    room_block = "\n".join(room_lines) if room_lines else "  （内訳情報なし）"
    increase_str = f"+{c['increase']}" if c["increase"] > 0 else "±0"
    return (
        f"▶ {c['name']}（{reason_label}）\n"
        f"{room_block}\n"
        f"  合計: {c['current_total']}戸（{increase_str}）"
    )


def build_line_messages(changes: list[dict[str, Any]]) -> list[dict[str, str]]:
    """変化リストを1通にまとめたLINEメッセージを返す（5000字超なら分割）。"""
    url = CHINTAI_URL
    header = f"【JKK 空き家状況更新】{len(changes)}件\n"
    footer = f"\n詳細はこちら: {url}"

    blocks = [_build_change_block(c) for c in changes]
    separator = "\n─────────────\n"

    # 5000字以内に収まるよう必要なら分割
    messages: list[dict[str, str]] = []
    current_blocks: list[str] = []
    for block in blocks:
        candidate = header + separator.join(current_blocks + [block]) + footer
        if current_blocks and len(candidate) > 5000:
            text = header + separator.join(current_blocks) + footer
            messages.append({"type": "text", "text": text})
            current_blocks = [block]
        else:
            current_blocks.append(block)

    if current_blocks:
        text = header + separator.join(current_blocks) + footer
        messages.append({"type": "text", "text": text})

    return messages


def send_line_push(messages: list[dict[str, str]]) -> bool:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    to = os.getenv("LINE_TO", "").strip()  # userId/groupId/roomId
    if not token or not to:
        print("[INFO] LINE設定が未指定のため通知をスキップします。")
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # LINE Messaging APIのpushは1回あたり最大5メッセージ
    for i in range(0, len(messages), 5):
        chunk = messages[i : i + 5]
        payload = {"to": to, "messages": chunk}
        try:
            r = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=30)
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"[WARN] LINE通知失敗: {exc}")
            return False
    return True


def main() -> None:
    ensure_data_dir()
    print(f"[INFO] 監視URL: {TARGET_URL}")

    html, session, final_url = get_with_session()
    if not html:
        print("[INFO] データ取得失敗のため終了します。")
        return

    if is_maintenance_page(html):
        print("[INFO] メンテナンス中（おわび画面）を検知したため終了します。")
        return

    rows = collect_all_rows(session, html, final_url)
    if not rows:
        # 構造変化・文字化け・空一覧・一時不調時も停止せずログ終了
        print("[INFO] 一覧から物件行を1件も取得できませんでした。通知せず終了します。")
        log_parse_failure_hint(html)
        dbg = save_debug_html(html)
        print(f"[INFO] 取得HTMLを保存しました: {dbg}")
        return

    current_map = build_property_map(rows)
    current_fp = build_room_fingerprint(rows)
    current_detail = build_room_detail_map(rows)

    prev_map = load_json(LAST_DATA_FILE, {})
    prev_fp = load_json(LAST_ROOMS_FILE, {})
    prev_detail = load_json(LAST_DETAIL_FILE, {})

    changes = detect_changes(
        current_map, prev_map, current_fp, prev_fp, current_detail, prev_detail, rows
    )

    # 成否に関係なく今回状態は保存
    save_json(LAST_DATA_FILE, current_map)
    save_json(LAST_ROOMS_FILE, current_fp)
    save_json(LAST_DETAIL_FILE, current_detail)

    if not changes:
        print("[INFO] 在庫増・新規・内訳入れ替わりはありません。")
        return

    messages = build_line_messages(changes)
    sent = send_line_push(messages)
    if sent:
        print(f"[INFO] {len(changes)}件の更新をLINE通知しました。")
    else:
        print(f"[INFO] {len(changes)}件の更新を検知（通知は未送信/失敗）。")


def send_daily_report() -> None:
    """--daily フラグ用: 保存済みの在庫状況を日次レポートとして送信する。"""
    import datetime
    ensure_data_dir()
    saved = load_json(LAST_DATA_FILE, {})
    saved_detail = load_json(LAST_DETAIL_FILE, {})

    jst = datetime.timezone(datetime.timedelta(hours=9))
    today = datetime.datetime.now(jst)
    weekday = ["月", "火", "水", "木", "金", "土", "日"][today.weekday()]
    date_str = f"{today.month}月{today.day}日({weekday})"

    if not saved:
        text = f"【JKK 在庫状況】{date_str}\n\n現在、空き家情報はありません。"
    else:
        lines = [f"【JKK 在庫状況】{date_str}\n"]
        total_units = 0
        for name, count in saved.items():
            total_units += count
            rooms = saved_detail.get(name, {})
            lines.append(f"▶ {name}: {count}戸")
            for room, cnt in sorted(rooms.items()):
                lines.append(f"  {room}: {cnt}戸")
        lines.append(f"\n合計: {len(saved)}物件 / {total_units}戸")
        lines.append(f"詳細はこちら: {CHINTAI_URL}")
        text = "\n".join(lines)

    print(f"[INFO] 日次レポート送信:\n{text}\n")
    sent = send_line_push([{"type": "text", "text": text[:5000]}])
    if sent:
        print("[INFO] 日次レポート送信成功。")
    else:
        print("[WARN] 日次レポート送信失敗（LINE設定を確認してください）。")


def send_test_message() -> None:
    """--test フラグ用: 現在の last_data.json の先頭1件を使ってテスト通知を送る。"""
    ensure_data_dir()
    saved = load_json(LAST_DATA_FILE, {})
    saved_detail = load_json(LAST_DETAIL_FILE, {})

    if saved:
        name, total = next(iter(saved.items()))
        rooms = saved_detail.get(name, {})
    else:
        name, total, rooms = "テスト物件（サンプル）", 3, {"1K": 2, "2DK": 1}

    test_change = {
        "name": name,
        "increase": 1,
        "current_total": total,
        "reason": "increase",
        "detail_url": CHINTAI_URL,
        "cur_rooms": rooms,
        "prv_rooms": {k: max(0, v - 1) for k, v in rooms.items()},
    }
    messages = build_line_messages([test_change])
    print(f"[INFO] テストメッセージ送信:\n{messages[0]['text']}\n")
    sent = send_line_push(messages)
    if sent:
        print("[INFO] テスト送信成功。LINEを確認してください。")
    else:
        print("[WARN] テスト送信失敗（LINE設定を確認してください）。")


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        send_test_message()
    elif "--daily" in sys.argv:
        send_daily_report()
    else:
        main()
