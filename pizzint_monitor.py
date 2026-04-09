from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
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
RUN_MODE = os.getenv("RUN_MODE", "daily").strip().lower()
STATE_FILE = Path(os.getenv("STATE_FILE", str(OUTPUT_DIR / "doughcon_state.json")))
ALERT_LEVELS = {
    int(x.strip())
    for x in os.getenv("ALERT_LEVELS", "1,2").split(",")
    if x.strip().isdigit() and 1 <= int(x.strip()) <= 5
}
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

QUIET_STATUSES = {
    "QUIET",
    "CLOSED",
    "NORMAL",
    "NO DATA",
    "LOADING TACTICAL DATA...",
}

IGNORE_LINE_PATTERNS = [
    re.compile(r"^POPULAR TIMES ANALYSIS$", re.I),
    re.compile(r"^LOADING TACTICAL DATA\.\.\.$", re.I),
    re.compile(r"^\d+(?:\.\d+)?\s*mi$", re.I),
    re.compile(r"^STATUS:.*$", re.I),
]

STORE_NAME_RE = re.compile(r"^[A-Z0-9&'\.\-\s]{3,}$")
DOUGHCON_LEVEL_RE = re.compile(r"DOUGHCON\s*([1-5])", re.I)
RELATIVE_HOURS_RE = re.compile(r"(\d+)\s*(?:H|HR|HRS|HOUR|HOURS)\s+AGO", re.I)
RELATIVE_MINUTES_RE = re.compile(r"(\d+)\s*(?:M|MIN|MINS|MINUTE|MINUTES)\s+AGO", re.I)
ABSOLUTE_DATETIME_PATTERNS = [
    re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})(?:[:]\d{2})?\s*(UTC|GMT|CST)?", re.I),
    re.compile(r"([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)\s*(UTC|GMT|CST)?", re.I),
    re.compile(r"(\d{1,2}:\d{2}\s*[AP]M)\s*(UTC|GMT|CST)?", re.I),
]
DOUGHCON_LABEL_MAP = {
    1: "Maximum Readiness",
    2: "Next Step to Maximum Readiness",
    3: "Increase in Force Readiness",
    4: "Increased Intelligence Watch",
    5: "Lowest State of Readiness",
}


@dataclass
class StoreInfo:
    name: str
    status: str | None = None
    distance_mi: float | None = None
    anomalous: bool = False


@dataclass
class PizzintSnapshot:
    fetched_at_bj: str
    fetched_at_utc: str
    site_url: str
    current_doughcon_level: int | None
    current_doughcon_label: str | None
    doughcon_changes_12h: list["DoughconChange"]
    locations_monitored: int | None
    anomalous_stores: list[StoreInfo]
    all_stores: list[StoreInfo]
    raw_notes: list[str]


@dataclass
class DoughconChange:
    observed_at_bj: str
    level: int
    level_label: str


def normalize_line(line: str) -> str:
    line = line.replace("\xa0", " ").replace("•", " • ")
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


def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def refresh_runtime_config() -> None:
    global URL, TIMEZONE, OUTPUT_DIR, REQUEST_TIMEOUT, RUN_MODE, STATE_FILE, ALERT_LEVELS, USER_AGENT
    URL = os.getenv("PIZZINT_URL", "https://www.pizzint.watch/")
    TIMEZONE = os.getenv("TIMEZONE", "Asia/Shanghai")
    OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
    RUN_MODE = os.getenv("RUN_MODE", "daily").strip().lower()
    STATE_FILE = Path(os.getenv("STATE_FILE", str(OUTPUT_DIR / "doughcon_state.json")))
    parsed_alert_levels = {
        int(x.strip())
        for x in os.getenv("ALERT_LEVELS", "1,2").split(",")
        if x.strip().isdigit() and 1 <= int(x.strip()) <= 5
    }
    ALERT_LEVELS = parsed_alert_levels or {1, 2}
    USER_AGENT = os.getenv(
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(data: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
    for idx, line in enumerate(lines):
        m = re.search(r"DOUGHCON\s*(\d+)", line, re.I)
        if m:
            level = int(m.group(1))
            label = None
            for nxt in lines[idx + 1 : idx + 4]:
                if nxt.startswith("###"):
                    continue
                if nxt and not re.search(r"SUPPORT|POWERED BY|Intel by the Slice", nxt, re.I):
                    label = nxt
                    break
            return level, label
    return None, None


def extract_locations_monitored(lines: list[str]) -> int | None:
    for line in lines[:30]:
        m = re.search(r"(\d+)\s+LOCATIONS\s+MONITORED", line, re.I)
        if m:
            return int(m.group(1))
    return None


def parse_event_time(text: str, now_bj: datetime) -> datetime | None:
    match = RELATIVE_HOURS_RE.search(text)
    if match:
        hours = int(match.group(1))
        return now_bj - timedelta(hours=hours)

    match = RELATIVE_MINUTES_RE.search(text)
    if match:
        minutes = int(match.group(1))
        return now_bj - timedelta(minutes=minutes)

    for pattern in ABSOLUTE_DATETIME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groups()
        tz_name = (groups[-1] or "").upper()
        tz = ZoneInfo("UTC") if tz_name in {"UTC", "GMT"} else ZoneInfo(TIMEZONE)
        try:
            if pattern is ABSOLUTE_DATETIME_PATTERNS[0]:
                dt = datetime.strptime(f"{groups[0]} {groups[1]}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                return dt.astimezone(ZoneInfo(TIMEZONE))
            if pattern is ABSOLUTE_DATETIME_PATTERNS[1]:
                dt = datetime.strptime(f"{groups[0]} {groups[1]}", "%B %d, %Y %I:%M %p").replace(tzinfo=tz)
                return dt.astimezone(ZoneInfo(TIMEZONE))
            if pattern is ABSOLUTE_DATETIME_PATTERNS[2]:
                base_date = now_bj.date()
                dt = datetime.strptime(groups[0], "%I:%M %p").replace(
                    year=base_date.year,
                    month=base_date.month,
                    day=base_date.day,
                    tzinfo=tz,
                )
                dt = dt.astimezone(ZoneInfo(TIMEZONE))
                if dt > now_bj + timedelta(minutes=5):
                    dt = dt - timedelta(days=1)
                return dt
        except ValueError:
            continue
    return None


def extract_doughcon_changes_12h(lines: list[str], now_bj: datetime) -> list[DoughconChange]:
    entries: list[tuple[datetime, int]] = []
    seen: set[tuple[str, int]] = set()

    for idx, line in enumerate(lines):
        level_match = DOUGHCON_LEVEL_RE.search(line)
        if not level_match:
            continue
        level = int(level_match.group(1))
        context_parts = [line]
        if idx > 0:
            context_parts.append(lines[idx - 1])
        if idx + 1 < len(lines):
            context_parts.append(lines[idx + 1])
        context_text = " ".join(context_parts)
        event_time = parse_event_time(context_text, now_bj)
        if event_time is None:
            continue

        key = (event_time.strftime("%Y-%m-%d %H:%M"), level)
        if key in seen:
            continue
        seen.add(key)
        entries.append((event_time, level))

    window_start = now_bj - timedelta(hours=12)
    filtered = [(ts, lv) for ts, lv in entries if window_start <= ts <= now_bj + timedelta(minutes=5)]
    filtered.sort(key=lambda x: x[0])
    return [
        DoughconChange(
            observed_at_bj=ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
            level=lv,
            level_label=DOUGHCON_LABEL_MAP.get(lv, "Unknown"),
        )
        for ts, lv in filtered
    ]


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

    anomalous = False
    if status:
        upper = status.upper()
        if upper not in QUIET_STATUSES:
            if any(token in upper for token in ["SPIKE", "BUSY", "SURGE", "%", "ALERT", "HIGH", "ELEVATED"]):
                anomalous = True
            elif upper not in QUIET_STATUSES:
                anomalous = True

    return StoreInfo(name=store_name, status=status, distance_mi=distance, anomalous=anomalous)


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


def build_snapshot(html: str) -> PizzintSnapshot:
    lines = extract_text_lines(html)
    doughcon_level, doughcon_label = extract_doughcon(lines)
    stores = extract_stores(lines)
    anomalous_stores = [store for store in stores if store.anomalous]
    now_utc = datetime.now(tz=ZoneInfo("UTC"))
    now_bj = now_utc.astimezone(ZoneInfo(TIMEZONE))
    doughcon_changes_12h = extract_doughcon_changes_12h(lines, now_bj)

    notes: list[str] = []
    if not stores:
        notes.append("未解析到门店列表，可能是页面结构发生变化。")
    if not doughcon_changes_12h:
        notes.append("未解析到过去12小时内可识别时间戳的 Doughcon 变化记录。")

    snapshot = PizzintSnapshot(
        fetched_at_bj=now_bj.strftime("%Y-%m-%d %H:%M:%S %Z"),
        fetched_at_utc=now_utc.strftime("%Y-%m-%d %H:%M:%S %Z"),
        site_url=URL,
        current_doughcon_level=doughcon_level,
        current_doughcon_label=doughcon_label or (DOUGHCON_LABEL_MAP.get(doughcon_level) if doughcon_level else None),
        doughcon_changes_12h=doughcon_changes_12h,
        locations_monitored=extract_locations_monitored(lines),
        anomalous_stores=anomalous_stores,
        all_stores=stores,
        raw_notes=notes,
    )
    return snapshot


def snapshot_to_dict(snapshot: PizzintSnapshot) -> dict[str, Any]:
    data = asdict(snapshot)
    return data


def render_email_text(snapshot: PizzintSnapshot) -> str:
    lines = [
        f"PizzINT 日报（北京时间）: {snapshot.fetched_at_bj}",
        f"网站链接: {snapshot.site_url}",
        f"当前 Doughcon: {snapshot.current_doughcon_level or '未知'}",
        f"当前等级说明: {snapshot.current_doughcon_label or '无'}",
        f"监控门店数: {snapshot.locations_monitored or '未知'}",
        "",
        "过去12小时 Doughcon 变化:",
    ]

    if snapshot.doughcon_changes_12h:
        for change in snapshot.doughcon_changes_12h:
            lines.append(f"- {change.observed_at_bj} | DOUGHCON {change.level}: {change.level_label}")
    else:
        lines.append("- 未解析到过去12小时的 Doughcon 变化记录")

    lines.extend([
        "",
        "异常门店:",
    ])
    if snapshot.anomalous_stores:
        for store in snapshot.anomalous_stores:
            distance = f"{store.distance_mi:.1f} mi" if store.distance_mi is not None else "距离未知"
            lines.append(f"- {store.name} | {store.status or '状态未知'} | {distance}")
    else:
        lines.append("- 当前未识别到异常门店")

    lines.extend(["", "全部门店:"])
    for store in snapshot.all_stores:
        distance = f"{store.distance_mi:.1f} mi" if store.distance_mi is not None else "距离未知"
        lines.append(f"- {store.name} | {store.status or '状态未知'} | {distance}")

    if snapshot.raw_notes:
        lines.extend(["", "备注:"])
        for note in snapshot.raw_notes:
            lines.append(f"- {note}")

    return "\n".join(lines)


def render_email_html(snapshot: PizzintSnapshot) -> str:
    def rows(items: list[StoreInfo]) -> str:
        if not items:
            return "<tr><td colspan='3'>当前未识别到异常门店</td></tr>"
        html_rows = []
        for store in items:
            distance = f"{store.distance_mi:.1f} mi" if store.distance_mi is not None else "距离未知"
            html_rows.append(
                "<tr>"
                f"<td>{store.name}</td>"
                f"<td>{store.status or '状态未知'}</td>"
                f"<td>{distance}</td>"
                "</tr>"
            )
        return "".join(html_rows)

    def doughcon_change_rows(items: list[DoughconChange]) -> str:
        if not items:
            return "<tr><td colspan='3'>未解析到过去12小时的 Doughcon 变化记录</td></tr>"
        html_rows = []
        for item in items:
            html_rows.append(
                "<tr>"
                f"<td>{item.observed_at_bj}</td>"
                f"<td>DOUGHCON {item.level}</td>"
                f"<td>{item.level_label}</td>"
                "</tr>"
            )
        return "".join(html_rows)

    all_rows = rows(snapshot.all_stores)
    anomaly_rows = rows(snapshot.anomalous_stores)
    doughcon_rows = doughcon_change_rows(snapshot.doughcon_changes_12h)
    notes_html = "".join(f"<li>{note}</li>" for note in snapshot.raw_notes) if snapshot.raw_notes else "<li>无</li>"

    return f"""
    <html>
      <body style="font-family: Arial, Helvetica, sans-serif; line-height: 1.5;">
        <h2>PizzINT 日报</h2>
        <p><strong>北京时间：</strong>{snapshot.fetched_at_bj}<br>
           <strong>UTC：</strong>{snapshot.fetched_at_utc}<br>
           <strong>站点：</strong><a href="{snapshot.site_url}">{snapshot.site_url}</a></p>
        <ul>
          <li><strong>当前 Doughcon：</strong>{snapshot.current_doughcon_level or '未知'}</li>
          <li><strong>当前等级说明：</strong>{snapshot.current_doughcon_label or '无'}</li>
          <li><strong>监控门店数：</strong>{snapshot.locations_monitored or '未知'}</li>
        </ul>

        <h3>过去12小时 Doughcon 变化</h3>
        <table border="1" cellspacing="0" cellpadding="6">
          <tr><th>时间（北京时间）</th><th>等级</th><th>说明</th></tr>
          {doughcon_rows}
        </table>

        <h3>异常门店</h3>
        <table border="1" cellspacing="0" cellpadding="6">
          <tr><th>门店</th><th>状态</th><th>距离</th></tr>
          {anomaly_rows}
        </table>

        <h3>全部门店</h3>
        <table border="1" cellspacing="0" cellpadding="6">
          <tr><th>门店</th><th>状态</th><th>距离</th></tr>
          {all_rows}
        </table>

        <h3>备注</h3>
        <ul>{notes_html}</ul>
      </body>
    </html>
    """


def render_alert_text(snapshot: PizzintSnapshot, previous_level: int | None) -> str:
    current_level = snapshot.current_doughcon_level
    current_label = snapshot.current_doughcon_label or DOUGHCON_LABEL_MAP.get(current_level or 0, "Unknown")
    prev_str = str(previous_level) if previous_level is not None else "未知"
    lines = [
        f"PizzINT Doughcon 告警（北京时间）: {snapshot.fetched_at_bj}",
        f"网站链接: {snapshot.site_url}",
        f"上次记录等级: {prev_str}",
        f"当前等级: DOUGHCON {current_level}: {current_label}" if current_level else "当前等级: 未知",
        f"告警条件: DOUGHCON {sorted(ALERT_LEVELS)}",
    ]
    return "\n".join(lines)


def render_alert_html(snapshot: PizzintSnapshot, previous_level: int | None) -> str:
    current_level = snapshot.current_doughcon_level
    current_label = snapshot.current_doughcon_label or DOUGHCON_LABEL_MAP.get(current_level or 0, "Unknown")
    prev_str = str(previous_level) if previous_level is not None else "未知"
    return f"""
    <html>
      <body style="font-family: Arial, Helvetica, sans-serif; line-height: 1.5;">
        <h2>PizzINT Doughcon 告警</h2>
        <p><strong>北京时间：</strong>{snapshot.fetched_at_bj}<br>
           <strong>站点：</strong><a href="{snapshot.site_url}">{snapshot.site_url}</a></p>
        <ul>
          <li><strong>上次记录等级：</strong>{prev_str}</li>
          <li><strong>当前等级：</strong>{f"DOUGHCON {current_level}: {current_label}" if current_level else "未知"}</li>
          <li><strong>告警条件：</strong>{", ".join(f"DOUGHCON {x}" for x in sorted(ALERT_LEVELS))}</li>
        </ul>
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
        print("未检测到 SMTP_USER / SMTP_PASS / MAIL_TO，跳过邮件发送。")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = mail_to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [x.strip() for x in mail_to.split(",") if x.strip()], msg.as_string())
    except OSError as exc:
        print(f"邮件发送失败: {exc}")


def main() -> None:
    load_env_file(".env")
    refresh_runtime_config()
    try:
        html = get_html(URL)
    except requests.RequestException as exc:
        now = datetime.now(tz=ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M")
        message = f"PizzINT 抓取失败: {exc}"
        subject = f"[PizzINT] {now} 抓取失败"
        try:
            send_email(subject, message, f"<p>{message}</p>")
        except Exception as mail_exc:
            print(f"失败告警邮件发送异常: {mail_exc}")
        print(message)
        raise SystemExit(1)

    snapshot = build_snapshot(html)
    json_path = save_snapshot(snapshot)

    if RUN_MODE == "alert":
        state = load_state()
        previous_level = state.get("last_doughcon_level")
        current_level = snapshot.current_doughcon_level

        should_send = (
            isinstance(current_level, int)
            and current_level in ALERT_LEVELS
            and current_level != previous_level
        )

        if should_send:
            subject = (
                f"[PizzINT Alert] {datetime.now(tz=ZoneInfo(TIMEZONE)).strftime('%Y-%m-%d %H:%M')} "
                f"DOUGHCON {current_level}"
            )
            send_email(
                subject,
                render_alert_text(snapshot, previous_level if isinstance(previous_level, int) else None),
                render_alert_html(snapshot, previous_level if isinstance(previous_level, int) else None),
            )
            print(f"已发送告警邮件: DOUGHCON {current_level} (上次: {previous_level})")
        else:
            print(
                "未触发告警: "
                f"当前={current_level}, 上次={previous_level}, 告警等级={sorted(ALERT_LEVELS)}"
            )

        state["last_doughcon_level"] = current_level
        state["last_checked_at_bj"] = snapshot.fetched_at_bj
        state["site_url"] = snapshot.site_url
        save_state(state)
        print(f"状态已保存: {STATE_FILE}")
        print(f"JSON 已保存: {json_path}")
        return

    subject = (
        f"[PizzINT] {datetime.now(tz=ZoneInfo(TIMEZONE)).strftime('%Y-%m-%d %H:%M')} "
        f"过去12小时 Doughcon 变化 {len(snapshot.doughcon_changes_12h)} 条"
    )
    text_body = render_email_text(snapshot)
    html_body = render_email_html(snapshot)
    send_email(subject, text_body, html_body)
    print(text_body)
    print(f"\nJSON 已保存: {json_path}")


if __name__ == "__main__":
    main()
