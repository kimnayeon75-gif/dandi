"""Fetch and validate the 12 most recent contract months for Daegu Dong-gu."""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DATA = Path(__file__).resolve().parents[1] / "data"
REGION = "27140"
TRADE = "RTMSDataSvcAptTradeDev"
RENT = "RTMSDataSvcAptRent"
PAGE_SIZE = 500


def integer(value):
    return int(value.replace(",", "").strip())


def request_page(service, month, page, key):
    endpoint = f"https://apis.data.go.kr/1613000/{service}/get{service}"
    params = urllib.parse.urlencode(dict(serviceKey=key, LAWD_CD=REGION,
                                        DEAL_YMD=month, pageNo=page, numOfRows=PAGE_SIZE))
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f"{endpoint}?{params}", timeout=25) as response:
                root = ET.fromstring(response.read())
            break
        except (OSError, ET.ParseError):
            if attempt == 2:
                raise RuntimeError(f"{service} {month}: HTTP/XML response failed") from None
            time.sleep(2 ** attempt)
    code = root.findtext("./header/resultCode") or root.findtext(".//returnReasonCode")
    if code not in {"0", "00", "000"}:
        raise RuntimeError(f"{service} {month}: API error code {code or 'unknown'}")
    body = root.find("body")
    if body is None:
        raise RuntimeError(f"{service} {month}: missing response body")
    return integer(body.findtext("totalCount", "")), body.findall("./items/item")


def fetch_month(service, month, key):
    total, items = request_page(service, month, 1, key)
    if len(items) != min(total, PAGE_SIZE):
        raise RuntimeError(f"{service} {month}: incorrect first page size")
    for page in range(2, (total + PAGE_SIZE - 1) // PAGE_SIZE + 1):
        page_total, batch = request_page(service, month, page, key)
        if page_total != total or len(batch) != min(PAGE_SIZE, total - len(items)):
            raise RuntimeError(f"{service} {month}: incomplete or changed result")
        items.extend(batch)
    return items


def normalize(item, service, month):
    def value(field):
        return (item.findtext(field) or "").strip()

    is_sale = service == TRADE
    if is_sale and any(value(field) not in {"", "-"} for field in ("cdealType", "cdealDay")):
        return None

    contract = date(*(integer(value(field)) for field in ("dealYear", "dealMonth", "dealDay")))
    rent = 0 if is_sale else integer(value("monthlyRent"))
    amount = integer(value("dealAmount" if is_sale else "deposit"))
    area = float(value("excluUseAr"))
    if (value("sggCd") not in {"", REGION} or contract.strftime("%Y%m") != month
            or not value("aptNm") or not value("umdNm") or not 0 < area < 10000
            or amount < 0 or rent < 0):
        raise ValueError(f"{service} {month}: unexpected transaction fields")

    return dict(type="sale" if is_sale else ("monthly" if rent else "jeonse"),
                name=value("aptNm"), dong=value("umdNm"), aptSeq=value("aptSeq"),
                price=amount, monthlyRent=rent, area=area, floor=integer(value("floor")),
                date=contract.isoformat(), buildYear=integer(value("buildYear") or "0"),
                roadNm=value("roadNm"), roadNo=value("roadNmBonbun").lstrip("0"),
                jibun=value("jibun"))


def months_ending(today):
    current = today.year * 12 + today.month - 1
    return [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(current - 11, current + 1)]


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                    + "\n", encoding="utf-8")


def refresh(key):
    months = months_ending(datetime.now(ZoneInfo("Asia/Seoul")).date())
    snapshots = {}
    for month in months:
        key_month = month.replace("-", "")
        rows = []
        for service in (TRADE, RENT):
            for item in fetch_month(service, key_month, key):
                row = normalize(item, service, key_month)
                if row is not None:
                    rows.append(row)
        if not rows and month != months[-1]:
            raise RuntimeError(f"{month}: historical month returned no transactions")
        snapshots[month] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))
        print(f"{month}: {len(rows)} rows", flush=True)

    index = json.loads((DATA / "market-index.json").read_text(encoding="utf-8"))
    old_files = {entry["file"] for entry in index["files"]}
    entries = []
    for month, rows in snapshots.items():
        filename = month + ".json"
        write_json(DATA / filename, rows)
        entries.append(dict(month=month, file=filename, count=len(rows)))
    index.update(updatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 range=dict(from_=months[0].replace("-", ""), to=months[-1].replace("-", "")),
                 files=entries)
    index["range"]["from"] = index["range"].pop("from_")
    index["sources"].update(
        sales="https://www.data.go.kr/data/15126468/openapi.do",
        leases="https://www.data.go.kr/data/15126474/openapi.do")
    write_json(DATA / "market-index.json", index)
    for expired in old_files - {entry["file"] for entry in entries}:
        (DATA / expired).unlink(missing_ok=True)


if __name__ == "__main__":
    key = urllib.parse.unquote(os.environ.get("MOLIT_SERVICE_KEY", "").strip())
    if not key:
        sys.exit("Set the MOLIT_SERVICE_KEY repository Actions secret.")
    try:
        refresh(key)
    except (RuntimeError, ValueError) as error:
        sys.exit(str(error))
