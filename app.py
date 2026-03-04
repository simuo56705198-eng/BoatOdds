import streamlit as st
import requests
import json
import re
import time
import csv
import os
from bs4 import BeautifulSoup
from datetime import datetime
import concurrent.futures
from rtpt_engine import analyze  # v6.1 Engine Integration

# --- 初期設定 ---
st.set_page_config(page_title="RTPT v6.1 — True Market Alpha Trader", layout="wide")
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
JCD_MAP = {
    "桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05",
    "浜名湖": "06", "蒲郡": "07", "常滑": "08", "津": "09", "三国": "10",
    "びわこ": "11", "住之江": "12", "尼崎": "13", "鳴門": "14", "丸亀": "15",
    "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20",
    "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24"
}

def extract_float(text):
    if not text: return 0.0
    m = re.search(r'-?[\d\.]+', str(text))
    return float(m.group()) if m else 0.0

# --- スクレイピング・エンジン (既存ロジック) ---

@st.cache_data(ttl=60)
def fetch_available_races(target_date):
    url = f"https://www.boatrace.jp/owpc/pc/race/index?hd={target_date}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        res.encoding = 'utf-8'
        html_text = res.text
        available_dict = {}
        tbodies = re.finditer(r'<tbody.*?>.*?</tbody>', html_text, re.DOTALL)
        for match in tbodies:
            tbody_html = match.group(0)
            stadium_match = re.search(r'alt="([^"]+)"', tbody_html)
            if not stadium_match: continue
            stadium_name = stadium_match.group(1).strip()
            if stadium_name not in JCD_MAP: continue
            if "最終Ｒ発売終了" in tbody_html or "中止" in tbody_html: continue
            current_r = 1
            r_match = re.search(r'>(\d{1,2})R<', tbody_html)
            if r_match: current_r = int(r_match.group(1))
            available_dict[stadium_name] = list(range(current_r, 13))
        return available_dict
    except Exception as e:
        return {}

def fetch_html(url, session, retries=3):
    for i in range(retries):
        try:
            res = session.get(url, timeout=10)
            res.raise_for_status()
            res.encoding = 'utf-8'
            return res.text
        except Exception:
            if i == retries - 1: return ""
            time.sleep(1)

def parse_racelist(html_text, race_data):
    if not html_text: return
    soup = BeautifulSoup(html_text, 'html.parser')
    tbodies = soup.select('.table1.is-tableFixed__3rdadd tbody.is-fs12')
    for tbody in tbodies:
        trs = tbody.find_all('tr')
        if not trs: continue
        tds = trs[0].find_all('td')
        if len(tds) < 8: continue
        b_no_raw = tds[0].text.strip()
        b_no_match = re.search(r'[1-6１-６]', b_no_raw)
        if not b_no_match: continue
        b_no = str(int(b_no_match.group().translate(str.maketrans('１２３４５６', '123456'))))
        class_info_div = tbody.select_one('div.is-fs11')
        rank = ""
        if class_info_div:
            rank_span = class_info_div.select_one('span')
            if rank_span: rank = rank_span.text.strip()
        name_el = tbody.select_one('.is-fs18.is-fBold')
        name = name_el.text.strip().replace('\u3000', ' ') if name_el else ""
        weight_match = re.search(r'([\d\.]+)kg', tds[2].text)
        weight = float(weight_match.group(1)) if weight_match else 0.0
        st_txt = [x.strip() for x in tds[3].get_text(separator='\n').split('\n') if x.strip()]
        nat_win_txt = [x.strip() for x in tds[4].get_text(separator='\n').split('\n') if x.strip()]
        loc_win_txt = [x.strip() for x in tds[5].get_text(separator='\n').split('\n') if x.strip()]
        mot = [x.strip() for x in tds[6].get_text(separator='\n').split('\n') if x.strip()]
        race_data["racelist"][b_no].update({
            "name": name, "class": rank, "weight": weight,
            "win_rate_national": extract_float(nat_win_txt[0]) if nat_win_txt else 0.0,
            "win_rate_local": extract_float(loc_win_txt[0]) if loc_win_txt else 0.0,
            "motor_no": mot[0] if mot else '-',
            "motor_2ren": extract_float(mot[1]) if len(mot) > 1 else 30.0,
            "avg_st": extract_float(st_txt[-1]) if st_txt else 0.15
        })

def parse_beforeinfo(html_text, race_data):
    if not html_text: return
    soup = BeautifulSoup(html_text, 'html.parser')
    env = race_data["environment"]
    t_el = soup.select_one('.is-temperature .weather1_bodyUnitLabelData')
    if t_el: env['temperature'] = extract_float(t_el.text)
    w_el = soup.select_one('.is-weather .weather1_bodyUnitLabelTitle')
    if w_el: env['weather'] = w_el.text.strip()
    ws_el = soup.select_one('.is-wind .weather1_bodyUnitLabelData')
    if ws_el: env['wind_speed'] = extract_float(ws_el.text)
    wt_el = soup.select_one('.is-waterTemperature .weather1_bodyUnitLabelData')
    if wt_el: env['water_temp'] = extract_float(wt_el.text)
    wh_el = soup.select_one('.is-wave .weather1_bodyUnitLabelData')
    if wh_el: env['wave_height'] = extract_float(wh_el.text)
    wd_img = soup.select_one('.is-windDirection .weather1_bodyUnitImage')
    if wd_img and wd_img.has_attr('class'):
        for cls in wd_img['class']:
            if cls.startswith('is-wind') and cls not in ['is-windDirection', 'is-wind']:
                try:
                    num = int(cls.replace('is-wind', ''))
                    dir_map = {i: "追い風" if i in [1,2,3,4,14,15,16] else "横風" if i in [5,13] else "向かい風" for i in range(1,17)}
                    env['wind_direction'] = dir_map.get(num, "無風")
                except ValueError: pass
    if env.get('wind_speed') == 0.0: env['wind_direction'] = "無風"
    for tbody in soup.select('.table1 tbody'):
        trs = tbody.find_all('tr')
        if not trs: continue
        tds = trs[0].find_all('td')
        b_no = None; boat_idx = -1
        for i, td in enumerate(tds):
            if td.get('class') and any(c.startswith('is-boatColor') for c in td.get('class')):
                match = re.search(r'\d+', td.text)
                if match: b_no = match.group(); boat_idx = i
                break
        if b_no and boat_idx != -1 and b_no in race_data["racelist"]:
            if len(tds) > boat_idx + 4:
                race_data["racelist"][b_no].update({
                    "tilt": extract_float(tds[boat_idx + 3].text),
                    "exhibition_time": extract_float(tds[boat_idx + 4].text)
                })
    st_ex_divs = soup.select('.table1_boatImage1')
    for course_idx, div in enumerate(st_ex_divs, 1):
        b_no_el = div.select_one('.table1_boatImage1Number')
        st_time_el = div.select_one('.table1_boatImage1Time')
        if b_no_el and st_time_el:
            b_no_match = re.search(r'\d+', b_no_el.text)
            if b_no_match:
                b_no = b_no_match.group()
                st_val = st_time_el.text.strip()
                if b_no in race_data["racelist"]:
                    race_data["racelist"][b_no].update({"start_course": course_idx, "start_exhibition_st": st_val})

def parse_all_odds(html_dict, race_data):
    for otype in ['odds3t', 'odds3f', 'odds2tf']:
        html = html_dict.get(otype)
        if not html: continue
        soup = BeautifulSoup(html, 'html.parser')
        tbs = soup.select('tbody.is-p3-0')
        if otype == 'odds3t': key, sep = '3連単', '-'
        elif otype == 'odds3f': key, sep = '3連複', '='
        if 'odds3' in otype:
            tb = tbs[0] if tbs else None
            if not tb: continue
            cur_snd, rem_row = [None]*6, [0]*6
            for row in tb.select('tr'):
                tds = row.find_all('td'); idx = 0
                for c in range(6):
                    if idx >= len(tds): break
                    if rem_row[c] == 0:
                        snd_td, trd_td, o_td = tds[idx], tds[idx+1], tds[idx+2]; idx += 3
                        cur_snd[c], rem_row[c] = snd_td, int(snd_td.get('rowspan', 1))
                    else:
                        trd_td, o_td = tds[idx], tds[idx+1]; idx += 2; snd_td = cur_snd[c]
                    rem_row[c] -= 1
                    if "is-disabled" not in o_td.get('class', []):
                        race_data["odds"][key][f"{c+1}{sep}{snd_td.text.strip()}{sep}{trd_td.text.strip()}"] = extract_float(o_td.text)
        else:
            for i, k in enumerate(["2連単", "2連複"]):
                if len(tbs) > i:
                    s = '-' if i == 0 else '='
                    for row in tbs[i].select('tr'):
                        tds = row.find_all('td')
                        for c in range(6):
                            if c*2+1 < len(tds) and "is-disabled" not in tds[c*2].get('class', []):
                                race_data["odds"][k][f"{c+1}{s}{tds[c*2].text.strip()}"] = extract_float(tds[c*2+1].text)
    html_k = html_dict.get('oddsk')
    if html_k:
        tbk = BeautifulSoup(html_k, 'html.parser').select_one('tbody.is-p3-0')
        if tbk:
            for row in tbk.select('tr'):
                tds = row.find_all('td')
                for c in range(6):
                    if c*2+1 < len(tds) and "is-disabled" not in tds[c*2].get('class', []):
                        race_data["odds"]["拡連複"][f"{c+1}={tds[c*2].text.strip()}"] = tds[c*2+1].text.strip()
    html_tf = html_dict.get('oddstf')
    if html_tf:
        soup_tf = BeautifulSoup(html_tf, 'html.parser')
        for unit in soup_tf.select('.grid_unit'):
            label_el = unit.select_one('.title7_mainLabel')
            if not label_el: continue
            label_text = label_el.text
            mode = "単勝" if "単勝" in label_text else "複勝" if "複勝" in label_text else None
            if not mode: continue
            for tr in unit.select('table tbody tr'):
                tds = tr.select('td')
                if len(tds) < 3: continue
                b_no = tds[0].text.strip(); val = tds[2].text.strip()
                if "is-disabled" not in tds[2].get('class', []):
                    if mode == "単勝": race_data["odds"]["単勝"][b_no] = extract_float(val)
                    else: race_data["odds"]["複勝"][b_no] = val

# ============================================================
# UI
# ============================================================
st.title("🎯 RTPT v6.1 — True Market Alpha Trader")
st.caption("ボタン1つで「データ取得 → 物理解析 → Kelly投資判断」を即座に実行")

with st.sidebar:
    st.header("⚙️ Settings")
    target_date = st.date_input("日付", datetime.now()).strftime('%Y%m%d')
    bankroll = st.number_input("💰 バンクロール（円）", min_value=100, value=1000, step=100)
    
    available_races_dict = fetch_available_races(target_date)
    if available_races_dict:
        stadiums = list(available_races_dict.keys())
        if stadiums:
            input_jcd = st.selectbox("🏟️ 開催場", stadiums)
            target_rno = st.selectbox("🏁 レース番号(R)", available_races_dict[input_jcd])
        else:
            st.caption("※全レース終了")
            input_jcd = st.selectbox("開催場", list(JCD_MAP.keys()))
            target_rno = st.selectbox("レース番号(R)", list(range(1, 13)))
    else:
        input_jcd = st.selectbox("開催場", list(JCD_MAP.keys()))
        target_rno = st.selectbox("レース番号(R)", list(range(1, 13)))
    
    execute = st.button("🚀 解析エンジン起動", type="primary", use_container_width=True)

if execute:
    target_jcd = JCD_MAP[input_jcd]
    race_data = {
        "metadata": {"date": target_date, "stadium": input_jcd, "race_number": f"{target_rno}R"},
        "environment": {}, "racelist": {str(i): {} for i in range(1, 7)},
        "odds": {"3連単": {}, "3連複": {}, "2連単": {}, "2連複": {}, "拡連複": {}, "単勝": {}, "複勝": {}}
    }

    # === Phase 1: Scrape ===
    with st.status("📡 データ取得中...", expanded=True) as status:
        st.write("7ページを並列取得中...")
        base_url = "https://www.boatrace.jp/owpc/pc/race"
        urls = {
            "racelist": f"{base_url}/racelist?rno={target_rno}&jcd={target_jcd}&hd={target_date}",
            "beforeinfo": f"{base_url}/beforeinfo?rno={target_rno}&jcd={target_jcd}&hd={target_date}",
            "odds3t": f"{base_url}/odds3t?rno={target_rno}&jcd={target_jcd}&hd={target_date}",
            "odds3f": f"{base_url}/odds3f?rno={target_rno}&jcd={target_jcd}&hd={target_date}",
            "odds2tf": f"{base_url}/odds2tf?rno={target_rno}&jcd={target_jcd}&hd={target_date}",
            "oddsk": f"{base_url}/oddsk?rno={target_rno}&jcd={target_jcd}&hd={target_date}",
            "oddstf": f"{base_url}/oddstf?rno={target_rno}&jcd={target_jcd}&hd={target_date}"
        }
        html_data = {}
        session = requests.Session()
        session.headers.update(HEADERS)
        with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
            future_to_key = {executor.submit(fetch_html, url, session): key for key, url in urls.items()}
            for future in concurrent.futures.as_completed(future_to_key):
                html_data[future_to_key[future]] = future.result()
        
        st.write("HTMLを解析中...")
        parse_racelist(html_data.get("racelist"), race_data)
        parse_beforeinfo(html_data.get("beforeinfo"), race_data)
        parse_all_odds(html_data, race_data)
        status.update(label="✅ データ取得完了", state="complete")

    # === Phase 2: v6.1 Engine Analysis ===
    result = analyze(race_data, bankroll)

    if result.get("error"):
        st.warning(f"⏳ {result['error']}")
    else:
        # --- 物理アルファ表示 ---
        st.header(f"🧠 {input_jcd} {target_rno}R — 物理アルファ解析")
        cols = st.columns(6)
        for boat_info in result["boats"]:
            with cols[boat_info["boat"] - 1]:
                delta = boat_info["post_prob"] - boat_info["tmp"]
                st.metric(
                    f"{boat_info['boat']}号艇",
                    f"{boat_info['post_prob']*100:.1f}%",
                    f"{delta*100:+.1f}%",
                    delta_color="normal" if delta >= 0 else "inverse"
                )
                st.caption(f"{boat_info['name']}")
                st.caption(f"TMP:{boat_info['tmp']*100:.1f}% α:{boat_info['alpha']:.3f}")
                if boat_info["wd"] < 12.0:
                    st.error(f"⚠️ WD:{boat_info['wd']:.0f}")
                for r in boat_info["reasons"]:
                    st.caption(f"📐 {r}")

        # --- 投資判断テーブル ---
        st.header("💰 投資判断テーブル (Kelly Criterion)")
        summary = result["summary"]
        
        if summary["verdict"] == "見（ケン）":
            st.error("🛑 **【判定: 見（ケン）】** EV 1.50以上の投資対象なし。本レースは完全見送り。")
        else:
            col1, col2, col3 = st.columns(3)
            col1.metric("投資対象", f"{summary['count']}点")
            col2.metric("平均EV", f"{summary['avg_ev']:.2f}")
            col3.metric("最大EV", f"{summary['max_ev']:.2f}", f"{summary['max_ev_combo']}")
            
            # Table
            table_data = []
            for t in result["targets"]:
                table_data.append({
                    "券種": t["type"],
                    "買い目": t["combo"],
                    "推定確率": f"{t['prob']*100:.1f}%",
                    "オッズ": f"{t['odds']:.1f}倍",
                    "EV": f"{t['ev']:.2f}",
                    "Kelly%": f"{t['kelly_pct']:.1f}%",
                    "推奨額": f"{t['recommended_yen']}円"
                })
            st.dataframe(table_data, use_container_width=True, hide_index=True)
    
    # --- JSON Download (backup) ---
    with st.expander("📥 JSONデータ（バックアップ）"):
        json_export = json.dumps(race_data, ensure_ascii=False, indent=2)
        st.download_button(
            label="JSONダウンロード",
            data=json_export,
            file_name=f"{target_date}_{input_jcd}_{target_rno}R_AIデータ.json",
            mime="application/json"
        )
        st.json(race_data)
