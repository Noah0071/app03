import os
import time
import json
import re
import traceback
from pathlib import Path
from typing import Dict, Any, List

import requests
from flask import Flask, jsonify, request, send_from_directory

# ----------------------- Flask -----------------------
# เสิร์ฟไฟล์สแตติกจากโฟลเดอร์เดียวกับ app.py (เช่น index.html, ptc_roster.html)
app = Flask(__name__, static_folder="", static_url_path="")

# ----------------------- PUBG API config -----------------------
# ตั้งค่า API KEY ผ่าน environment variable:  PUBG_API_KEY
PUBG_API_KEY = os.environ.get("PUBG_API_KEY")
PUBG_BASE = os.environ.get("PUBG_BASE", "https://api.pubg.com/shards/steam")

PUBG_HEADERS = {
    "Authorization": f"Bearer {PUBG_API_KEY}",
    "Accept": "application/vnd.api+json",
}

# ----------------------- Twire scraping cache -----------------------
CACHE_DIR = Path(".twire_cache")
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL_SEC = 6 * 60 * 60  # 6 ชั่วโมง

# ----------------------- Utilities -----------------------
def require_pubg_key():
    if not PUBG_API_KEY or PUBG_API_KEY == "YOUR_PUBG_API_KEY_HERE":
        return False, jsonify({"ok": False, "error": "Missing PUBG_API_KEY (set env var)"}), 400
    return True, None, None

def _safe_get(obj, *keys, default=None):
    cur = obj
    try:
        for k in keys:
            if isinstance(cur, dict):
                cur = cur.get(k)
            else:
                return default
        return cur if cur is not None else default
    except Exception:
        return default

def _to_m_ss(seconds: int) -> str:
    """แปลงวินาทีเป็น M:SS (เช่น 7:05)"""
    try:
        sec = int(seconds or 0)
        m, s = divmod(sec, 60)
        return f"{m}:{s:02d}"
    except Exception:
        return "-"

def _distance_str(walk: float, ride: float, swim: float) -> str:
    """รวมระยะทาง (เมตร) แล้วแสดงเป็น km หรือ m"""
    try:
        total = float(walk or 0) + float(ride or 0) + float(swim or 0)
        if total >= 1000:
            return f"{total/1000:.2f} km"
        return f"{total:.0f} m"
    except Exception:
        return "-"

def kd_round_to_int(val) -> int:
    """
    ปัด K/D เป็นจำนวนเต็ม:
      ทศนิยม <= 0.4 ปัดลง
      ทศนิยม >= 0.5 ปัดขึ้น
      ช่วง 0.41–0.49 ปัดลง
    """
    try:
        n = float(val)
    except Exception:
        return 0
    i = int(n)
    frac = abs(n - i)
    if frac <= 0.4:
        return i
    if frac >= 0.5:
        return i + (1 if n >= 0 else -1)
    return i

# ----------------------- PUBG helpers -----------------------
def get_player_id(player_name: str) -> str:
    """
    ดึง player id จากชื่อ (steam shard)
    """
    url = f"{PUBG_BASE}/players"
    params = {"filter[playerNames]": player_name}
    r = requests.get(url, headers=PUBG_HEADERS, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = data.get("data") or []
    if not items:
        return ""
    return items[0].get("id", "")

def get_player_match_ids(player_id: str) -> List[str]:
    """
    จาก player id ดึง relationships.matches → list match ids
    """
    url = f"{PUBG_BASE}/players/{player_id}"
    r = requests.get(url, headers=PUBG_HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    rel = _safe_get(data, "data", "relationships", "matches", "data", default=[])
    ids = [x.get("id") for x in rel if x.get("type") == "match"]
    return ids

def get_match_detail(match_id: str) -> Dict[str, Any]:
    """
    ดึงรายละเอียดแมตช์ (รวม included)
    """
    url = f"{PUBG_BASE}/matches/{match_id}"
    r = requests.get(url, headers=PUBG_HEADERS, timeout=25)
    r.raise_for_status()
    return r.json()

def extract_player_stats_from_match(match_json: Dict[str, Any], player_name: str) -> Dict[str, Any]:
    """
    จาก payload /matches/<id> หา participant ของผู้เล่น แล้วแปลงเป็นสถิติที่ UI ใช้
    """
    data = match_json.get("data", {})
    attrs = data.get("attributes", {})
    included = match_json.get("included", []) or []

    # ข้อมูลแมตช์ระดับบน
    game_mode = attrs.get("gameMode") or "-"
    map_name = attrs.get("mapName") or "-"
    created_at = attrs.get("createdAt") or "-"

    # หา participant ของ player
    participant = None
    for inc in included:
        if inc.get("type") == "participant":
            s = _safe_get(inc, "attributes", "stats", default={})
            if s.get("name", "").lower() == (player_name or "").lower():
                participant = s
                break

    # ค่า default ถ้าไม่เจอผู้เล่น (เช่น ถูกลบชื่อ)
    out = {
        "match_id": data.get("id"),
        "mode": game_mode,
        "map": map_name,
        "createdAt": created_at,
        "rank": None,
        "kills": 0,
        "damage": 0.0,
        "dbno": 0,
        "traveled": "-",
        "timeAlive": "-",
    }
    if not participant:
        return out

    # แปลงค่า
    out.update({
        "rank": participant.get("winPlace"),
        "kills": participant.get("kills", 0),
        "damage": participant.get("damageDealt", 0.0),
        "dbno": participant.get("DBNOs", 0),
        "traveled": _distance_str(
            participant.get("walkDistance", 0.0),
            participant.get("rideDistance", 0.0),
            participant.get("swimDistance", 0.0),
        ),
        "timeAlive": _to_m_ss(participant.get("timeSurvived", 0)),
    })
    return out

# ----------------------- API: PUBG -----------------------
@app.route("/api/matches/<player_name>")
def api_matches(player_name: str):
    """
    ดึงแมตช์ล่าสุดของผู้เล่น (แบบเพจ)
      GET /api/matches/<player_name>?page=0&limit=10
    คืนค่า: {"ok": True, "player": "...", "matches": [ {...}, ... ]}
    """
    ok, err, code = require_pubg_key()
    if not ok:
        return err, code

    page = max(int(request.args.get("page", 0) or 0), 0)
    limit = int(request.args.get("limit", 10) or 10)
    limit = 1 if limit < 1 else (50 if limit > 50 else limit)

    try:
        pid = get_player_id(player_name)
        if not pid:
            return jsonify({"ok": True, "player": player_name, "matches": []})

        all_ids = get_player_match_ids(pid)  # รายการล่าสุดมาก่อน
        if not all_ids:
            return jsonify({"ok": True, "player": player_name, "matches": []})

        # เพจ: ตัดช่วงที่ต้องการ
        start = page * limit
        end = start + limit
        slice_ids = all_ids[start:end]

        matches = []
        for mid in slice_ids:
            try:
                mj = get_match_detail(mid)
                st = extract_player_stats_from_match(mj, player_name)
                matches.append(st)
            except Exception:
                # ถ้าแมตช์ใดพัง ให้ข้าม แล้วไปต่อ
                continue

        return jsonify({"ok": True, "player": player_name, "page": page, "limit": limit, "matches": matches})
    except requests.HTTPError as e:
        return jsonify({"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}), 502
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

# ----------------------- API: Twire scraping -----------------------
# ใช้ Playwright เรนเดอร์หน้า Twire (SPA) แล้วพาร์สตาราง Player Stats
# ติดตั้งครั้งเดียว:
#   pip install playwright beautifulsoup4 lxml
#   playwright install chromium
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

def cache_path(tid: str, group: str, round_: str) -> Path:
    key = f"{tid}__g={group or ''}__r={round_ or ''}".replace("/", "_")
    return CACHE_DIR / f"player_stats_{key}.json"

def load_cache(tid: str, group: str, round_: str):
    p = cache_path(tid, group, round_)
    if p.exists():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if time.time() - obj.get("_ts", 0) <= CACHE_TTL_SEC:
                return obj.get("data")
        except Exception:
            pass
    return None

def save_cache(tid: str, group: str, round_: str, data):
    p = cache_path(tid, group, round_)
    p.write_text(json.dumps({"_ts": int(time.time()), "data": data}, ensure_ascii=False, indent=2), encoding="utf-8")

def parse_twire_players(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    target = None
    wanted = ["player", "k/d", "kills", "assists", "headshot", "longest"]

    for tb in tables:
        ths = [th.get_text(strip=True).lower() for th in tb.select("thead th")]
        if not ths:
            fr = tb.find("tr")
            if fr:
                ths = [c.get_text(strip=True).lower() for c in fr.find_all(["th", "td"])]
        score = sum(any(w in h for h in ths) for w in wanted)
        if score >= 4:
            target = tb
            break

    players = []
    if not target:
        return players

    headers = [h.get_text(strip=True).lower() for h in target.select("thead th")]
    body_rows = target.select("tbody tr")
    if not headers:
        first = target.find("tr")
        if first:
            headers = [c.get_text(strip=True).lower() for c in first.find_all(["th", "td"])]
            body_rows = first.find_all_next("tr")
        else:
            body_rows = target.find_all("tr")[1:]

    def find_idx(keys):
        for i, h in enumerate(headers):
            for k in keys:
                if k in h:
                    return i
        return -1

    i_player = find_idx(["player", "ign", "nickname", "name"])
    i_kd = find_idx(["k/d", "kd"])
    i_kills = find_idx(["kill"])
    i_ast = find_idx(["assist"])
    i_hs = find_idx(["headshot"])
    i_long = find_idx(["longest"])

    def as_int(s):
        s = (s or "").replace(",", "")
        m = re.search(r"-?\d+", s)
        return int(m.group()) if m else 0

    def as_float(s):
        s = (s or "").replace(",", "")
        m = re.search(r"-?\d+(\.\d+)?", s)
        return float(m.group()) if m else 0.0

    for tr in body_rows:
        tds = tr.find_all(["td", "th"])
        if not tds or len(tds) < 3:
            continue
        def val(i):
            return tds[i].get_text(" ", strip=True) if 0 <= i < len(tds) else ""

        ign = val(i_player)
        if not ign or ign.lower() in ("player", "name"):
            continue
        kd = kd_round_to_int(val(i_kd))
        players.append({
            "ign": ign,
            "kd": kd,
            "kills_total": as_int(val(i_kills)),
            "assists_total": as_int(val(i_ast)),
            "headshot_kills": as_int(val(i_hs)),
            "longest_kill": as_float(val(i_long)),
        })
    return players

def fetch_twire_players(tournament_id: str, group: str, round_: str) -> List[Dict[str, Any]]:
    # ใช้เส้นทาง hashbang ของ Twire เพื่อเปิดหน้า player-stats
    base = f"https://twire.gg/#!/en/pubg/tournaments/tournament/{tournament_id}/pubg-thailand-championship-2025-phase-2/player-stats"
    qs = []
    if group is not None:
        qs.append(f"group={group}")
    if round_ is not None:
        qs.append(f"round={round_}")
    url = base + ("?" + "&".join(qs) if qs else "")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        # รอให้มี table ปรากฏ
        try:
            page.wait_for_selector("table", timeout=15000)
        except Exception:
            page.wait_for_timeout(2000)
        html = page.content()
        context.close()
        browser.close()

    return parse_twire_players(html)

@app.route("/api/twire/player-stats")
def api_twire_player_stats():
    """
    ดึง Player Stats จาก Twire: GET /api/twire/player-stats?tournament_id=2257&group=&round=
    คืน fields: ign, kd(ปัดตามกฎ), kills_total, assists_total, headshot_kills, longest_kill
    """
    tid = (request.args.get("tournament_id") or "").strip()
    if not tid:
        return jsonify({"ok": False, "error": "missing tournament_id"}), 400
    group = request.args.get("group", "")
    round_ = request.args.get("round", "")

    cached = load_cache(tid, group, round_)
    if cached is not None:
        return jsonify({"ok": True, "cached": True, "tournament_id": tid, "group": group, "round": round_, "players": cached})

    try:
        players = fetch_twire_players(tid, group, round_)
        save_cache(tid, group, round_, players)
        return jsonify({"ok": True, "cached": False, "tournament_id": tid, "group": group, "round": round_, "players": players})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

# ----------------------- Static files -----------------------
@app.route("/")
def root():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/<path:path>")
def static_proxy(path):
    return send_from_directory(app.static_folder, path)

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon.ico")

# ----------------------- Main -----------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # debug=True เพื่อสะดวกตอนพัฒนา
    app.run(host="0.0.0.0", port=port, debug=True)
