from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
from dataclasses import asdict, dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = os.getenv("PIZZINT_URL", "https://www.pizzint.watch/")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Shanghai")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

QUIET_STATUSES = {
    "QUIET",
    "CLOSED",
    "NORMAL",
    "NOMINAL",
    "NO DATA",
    "LOADING TACTICAL DATA...",
}

DOUGHCON_LABELS = {
    1: "Maximum Readiness",
    2: "Next Step to Maximum Readiness",
    3: "Increase in Force Readiness",
    4: "Increased Intelligence Watch",
    5: "Lowest State of Readiness",
}

IGNORE_LINE_PATTERNS = [
    re.compile(r"^POPULAR TIMES ANALYSIS$", re.I),
    re.compile(r"^LOADING TACTICAL DATA\.\.\.$", re.I),
    re.compile(r"^\d+(?:\.\d+)?\s*mi$", re.I),
    re.compile(r"^STATUS:.*$", re.I),
]

STORE_NAME_RE = re.compile(r"^[A-Z0-9&'\.\-\s]{3,}$")


@dataclass
class StoreInfo:
    name: str
    status: str | None = None
    distance_mi: float | None = None
    spike_percent: float | None = None
    signal_score: float = 0.0
    anomalous: bool = False


@dataclass
class PizzintSnapshot:
    fetched_at_bj: str
    fetched_at_utc: str
    site_url: str
    doughcon_level: int | None
    doughcon_label: str | None
    locations_monitored: int | None
    active_stores_count: int
    spike_stores_count: int
    max_spike_percent: float | None
    pentagon_signal_score: float
    anomalous_stores: list[StoreInfo]
    top_signal_stores: list[StoreInfo]
    all_stores: list[StoreInfo]
    raw_notes: list[str]


def normalize_line(line: str) -> str:
    line = line.replace("\xa0", " ").replace("’", "'").replace("–", "-")
    line = re.sub(r"\s+", " ", line).strip()
    return line


def is_store_name(line: str) -> bool:
    if not STORE_NAME_RE.match(line):
        return False
    if "PIZZA" not in line:
        return False
    banned = {
        "PENTAGON PIZZA INDEX",
        "PIZZA INTELLIGENCE HISTORY",
    }
    return line not in banned


def get_html(url: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def extract_text_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = []
    for raw in text.splitlines():
        line = normalize_line(raw)
        if line:
            lines.append(line)
    return lines


def extract_doughcon(lines: list[str]) -> tuple[int | None, str | None]:
    for line in lines:
        m = re.search(r"DOUGHCON\s*(\d+)", line, re.I)
        if m:
            level = int(m.group(1))
            label = DOUGHCON_LABELS.get(level)
            return level, label
    return None, None


def extract_locations_monitored(lines: list[str]) -> int | None:
    for line in lines[:40]:
        m = re.search(r"(\d+)\s+LOCATIONS\s+MONITORED", line, re.I)
        if m:
            return int(m.group(1))
    return None


def extract_spike_percent(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", text, re.I)
    if not m:
        return None
    return float(m.group(1))


def calc_store_signal_score(status: str | None, distance_mi: float | None) -> float:
    if not status:
        return 0.0

    upper = status.upper()
    spike_percent = extract_spike_percent(status) or 0.0
    if upper in QUIET_STATUSES and spike_percent <= 0:
        return 0.0

    # Percent spike is the primary signal. Keywords are additive hints.
    base = min(spike_percent, 300.0) / 3.0
    if any(token in upper for token in ["SPIKE", "SURGE"]):
        base += 18.0
    elif any(token in upper for token in ["BUSY", "HIGH", "ELEVATED", "ALERT"]):
        base += 10.0
    elif upper not in QUIET_STATUSES:
        base += 6.0

    if distance_mi is None:
        distance_factor = 0.75
    else:
        distance_factor = max(0.35, 1.0 - (distance_mi / 15.0) * 0.65)

    return round(min(100.0, base * distance_factor), 2)


def parse_store_block(block_lines: list[str], store_name: str) -> StoreInfo:
    status = None
    distance = None

    for line in block_lines:
        if line == store_name:
            continue
        m = re.match(r"^(\d+(?:\.\d+)?)\s*mi$", line, re.I)
        if m and distance is None:
            distance = float(m.group(1))
            continue
        if any(pat.match(line) for pat in IGNORE_LINE_PATTERNS):
            continue
        if status is None:
            status = line
            continue

    spike_percent = extract_spike_percent(status)
    signal_score = calc_store_signal_score(status, distance)
    anomalous = signal_score > 0

    return StoreInfo(
        name=store_name,
        status=status,
        distance_mi=distance,
        spike_percent=spike_percent,
        signal_score=signal_score,
        anomalous=anomalous,
    )


def extract_stores(lines: list[str]) -> list[StoreInfo]:
    stores: list[StoreInfo] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if is_store_name(line):
            store_name = line
            block: list[str] = [store_name]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if is_store_name(nxt) or nxt in {"MARKET INTELLIGENCE", "OSINT FEED", "PIZZA INTELLIGENCE HISTORY"}:
                    break
                block.append(nxt)
                j += 1
            stores.append(parse_store_block(block, store_name))
            i = j
        else:
            i += 1
    return stores


def summarize_pentagon_signal(stores: list[StoreInfo]) -> tuple[int, int, float | None, float, list[StoreInfo]]:
    active_stores = [s for s in stores if s.status and s.status.upper() not in QUIET_STATUSES]
    spike_stores = [s for s in stores if s.spike_percent is not None and s.spike_percent >= 20]
    max_spike = max((s.spike_percent for s in stores if s.spike_percent is not None), default=None)

    ranked = sorted(
        [s for s in stores if s.signal_score > 0],
        key=lambda s: (s.signal_score, s.spike_percent or 0.0, -(s.distance_mi or 999.0)),
        reverse=True,
    )
    top_signal_stores = ranked[:5]

    if not ranked:
        total_score = 0.0
    else:
        avg_top3 = sum(s.signal_score for s in ranked[:3]) / min(3, len(ranked))
        active_bonus = min(20.0, len(active_stores) * 3.5)
        nearby_active_bonus = min(15.0, sum(1 for s in active_stores if (s.distance_mi or 999.0) <= 3.0) * 5.0)
        total_score = min(100.0, round(avg_top3 + active_bonus + nearby_active_bonus, 2))

    return len(active_stores), len(spike_stores), max_spike, total_score, top_signal_stores


def build_snapshot(html: str) -> PizzintSnapshot:
    lines = extract_text_lines(html)
    doughcon_level, doughcon_label = extract_doughcon(lines)
    stores = extract_stores(lines)
    anomalous_stores = [store for store in stores if store.anomalous]
    (
        active_stores_count,
        spike_stores_count,
        max_spike_percent,
        pentagon_signal_score,
        top_signal_stores,
    ) = summarize_pentagon_signal(stores)

    now_utc = datetime.now(tz=ZoneInfo("UTC"))
    now_bj = now_utc.astimezone(ZoneInfo(TIMEZONE))

    notes: list[str] = []
    if not stores:
        notes.append("No store blocks were parsed. Page structure may have changed.")

    return PizzintSnapshot(
        fetched_at_bj=now_bj.strftime("%Y-%m-%d %H:%M:%S %Z"),
        fetched_at_utc=now_utc.strftime("%Y-%m-%d %H:%M:%S %Z"),
        site_url=URL,
        doughcon_level=doughcon_level,
        doughcon_label=doughcon_label,
        locations_monitored=extract_locations_monitored(lines),
        active_stores_count=active_stores_count,
        spike_stores_count=spike_stores_count,
        max_spike_percent=max_spike_percent,
        pentagon_signal_score=pentagon_signal_score,
        anomalous_stores=anomalous_stores,
        top_signal_stores=top_signal_stores,
        all_stores=stores,
        raw_notes=notes,
    )


def snapshot_to_dict(snapshot: PizzintSnapshot) -> dict[str, Any]:
    return asdict(snapshot)


def render_email_text(snapshot: PizzintSnapshot) -> str:
    max_spike = f"{snapshot.max_spike_percent:.1f}%" if snapshot.max_spike_percent is not None else "N/A"
    lines = [
        f"PizzINT Daily Snapshot (Beijing): {snapshot.fetched_at_bj}",
        f"URL: {snapshot.site_url}",
        f"Doughcon: {snapshot.doughcon_level or 'N/A'}",
        f"Doughcon Label: {snapshot.doughcon_label or 'N/A'}",
        f"Locations Monitored: {snapshot.locations_monitored or 'N/A'}",
        f"Active Stores: {snapshot.active_stores_count}",
        f"Spike Stores (>=20%): {snapshot.spike_stores_count}",
        f"Max Spike: {max_spike}",
        f"Pentagon Pizza Signal Score: {snapshot.pentagon_signal_score:.1f}/100",
        "",
        "Top Signals (Top 5):",
    ]

    if snapshot.top_signal_stores:
        for store in snapshot.top_signal_stores:
            distance = f"{store.distance_mi:.1f} mi" if store.distance_mi is not None else "N/A"
            spike = f"{store.spike_percent:.1f}%" if store.spike_percent is not None else "-"
            lines.append(
                f"- {store.name} | {store.status or 'N/A'} | spike={spike} | score={store.signal_score:.1f} | {distance}"
            )
    else:
        lines.append("- No elevated store signal detected")

    lines.extend(["", "All Stores:"])
    for store in snapshot.all_stores:
        distance = f"{store.distance_mi:.1f} mi" if store.distance_mi is not None else "N/A"
        spike = f"{store.spike_percent:.1f}%" if store.spike_percent is not None else "-"
        lines.append(
            f"- {store.name} | {store.status or 'N/A'} | spike={spike} | score={store.signal_score:.1f} | {distance}"
        )

    if snapshot.raw_notes:
        lines.extend(["", "Notes:"])
        for note in snapshot.raw_notes:
            lines.append(f"- {note}")

    return "\n".join(lines)


def render_email_html(snapshot: PizzintSnapshot) -> str:
    def rows(items: list[StoreInfo]) -> str:
        if not items:
            return "<tr><td colspan='5'>No elevated store signal detected</td></tr>"

        html_rows = []
        for store in items:
            distance = f"{store.distance_mi:.1f} mi" if store.distance_mi is not None else "N/A"
            spike = f"{store.spike_percent:.1f}%" if store.spike_percent is not None else "-"
            html_rows.append(
                "<tr>"
                f"<td>{store.name}</td>"
                f"<td>{store.status or 'N/A'}</td>"
                f"<td>{spike}</td>"
                f"<td>{store.signal_score:.1f}</td>"
                f"<td>{distance}</td>"
                "</tr>"
            )
        return "".join(html_rows)

    top_rows = rows(snapshot.top_signal_stores)
    all_rows = rows(snapshot.all_stores)
    notes_html = "".join(f"<li>{note}</li>" for note in snapshot.raw_notes) if snapshot.raw_notes else "<li>None</li>"
    max_spike = f"{snapshot.max_spike_percent:.1f}%" if snapshot.max_spike_percent is not None else "N/A"

    return f"""
    <html>
      <body style="font-family: Arial, Helvetica, sans-serif; line-height: 1.5;">
        <h2>PizzINT Daily Snapshot</h2>
        <p><strong>Beijing Time:</strong> {snapshot.fetched_at_bj}<br>
           <strong>UTC:</strong> {snapshot.fetched_at_utc}<br>
           <strong>URL:</strong> <a href="{snapshot.site_url}">{snapshot.site_url}</a></p>
        <ul>
          <li><strong>Doughcon:</strong> {snapshot.doughcon_level or 'N/A'}</li>
          <li><strong>Doughcon Label:</strong> {snapshot.doughcon_label or 'N/A'}</li>
          <li><strong>Locations Monitored:</strong> {snapshot.locations_monitored or 'N/A'}</li>
          <li><strong>Active Stores:</strong> {snapshot.active_stores_count}</li>
          <li><strong>Spike Stores (&gt;=20%):</strong> {snapshot.spike_stores_count}</li>
          <li><strong>Max Spike:</strong> {max_spike}</li>
          <li><strong>Pentagon Pizza Signal Score:</strong> {snapshot.pentagon_signal_score:.1f}/100</li>
        </ul>

        <h3>Top Signals (Top 5)</h3>
        <table border="1" cellspacing="0" cellpadding="6">
          <tr><th>Store</th><th>Status</th><th>Spike</th><th>Signal Score</th><th>Distance</th></tr>
          {top_rows}
        </table>

        <h3>All Stores</h3>
        <table border="1" cellspacing="0" cellpadding="6">
          <tr><th>Store</th><th>Status</th><th>Spike</th><th>Signal Score</th><th>Distance</th></tr>
          {all_rows}
        </table>

        <h3>Notes</h3>
        <ul>{notes_html}</ul>
      </body>
    </html>
    """


def save_snapshot(snapshot: PizzintSnapshot) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    path = OUTPUT_DIR / f"pizzint_snapshot_{timestamp}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(snapshot_to_dict(snapshot), f, ensure_ascii=False, indent=2)

    latest = OUTPUT_DIR / "latest.json"
    latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def send_email(subject: str, text_body: str, html_body: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.qq.com")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    mail_to = os.getenv("MAIL_TO")

    if not smtp_user or not smtp_pass or not mail_to:
        print("SMTP_USER / SMTP_PASS / MAIL_TO not found, skip email sending.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = mail_to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
        server.login(smtp_user, smtp_pass)
        recipients = [x.strip() for x in mail_to.split(",") if x.strip()]
        server.sendmail(smtp_user, recipients, msg.as_string())


def main() -> None:
    html = get_html(URL)
    snapshot = build_snapshot(html)
    json_path = save_snapshot(snapshot)

    subject = (
        f"[PizzINT] {datetime.now(tz=ZoneInfo(TIMEZONE)).strftime('%Y-%m-%d %H:%M')} "
        f"Doughcon {snapshot.doughcon_level or '?'} "
        f"Signal {snapshot.pentagon_signal_score:.1f}"
    )
    text_body = render_email_text(snapshot)
    html_body = render_email_html(snapshot)
    send_email(subject, text_body, html_body)

    print(text_body)
    print(f"\nJSON saved: {json_path}")


if __name__ == "__main__":
    main()
