import streamlit as st
import requests
import json
import re
from bs4 import BeautifulSoup
from datetime import datetime

# --- 初期設定 ---
st.set_page_config(page_title="Real-Time Physics Trader v2.2", layout="wide")
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
JCD_MAP = {
    "桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05",
    "浜名湖": "06", "蒲郡": "07", "常滑": "08", "津": "09",
    "三国": "10", "びわこ": "11", "住之江": "12", "尼崎": "13",
    "鳴門": "14", "丸亀": "15",
    "児島": "16", "宮島": "17", "徳山": "18", "下関": "19",
    "若松": "20", "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24"
}

def extract_float(text):
    m = re.search(r'[\d\.]+', text)
    return float(m.group()) if m else 0.0

# --- スクレイピング関数群 (元ロジックを完全復元) ---

def get_racelist(jcd, rno, hd, race_data):
    url = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={jcd}&hd={hd}"
    res = requests.get(url, headers=HEADERS)
    res.encoding = 'utf-8'
    soup = BeautifulSoup(res.text, 'html.parser')
    tbodies = soup.select('.table1.is-tableFixed__3rdadd tbody.is-fs12')
    if not tbodies: return
    for tbody in tbodies:
        tds = tbody.find_all('tr')[0].find_all('td')
        if len(tds) < 8: continue
        boat_no = str(int(tds[0].text.strip()))
        name = tbody.select_one('.is-fs18.is-fBold').text.strip().replace('\u3000', ' ')
        class_rank = tbody.select_one('.is-fColor1').text.strip() if tbody.select_one('.is-fColor1') else ""
        st_list = [x.strip() for x in tds[3].text.split('\n') if x.strip()]
        mot = [x.strip() for x in tds[6].text.split('\n') if x.strip()]
        race_data["racelist"][boat_no].update({
            "name": name, "class": class_rank, 
            "motor_no": mot[0] if mot else '-', "motor_2ren": mot[1] if len(mot)>1 else '-', 
            "avg_st": extract_float(st_list[-1]) if st_list else 0.0
        })

def get_beforeinfo(jcd, rno, hd, race_data):
    url = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={hd}"
    res = requests.get(url, headers=HEADERS)
    res.encoding = 'utf-8'
    soup = BeautifulSoup(res.text, 'html.parser')
    
    env = {"weather": "不明", "wind_direction": "無風", "wind_speed": 0.0, "temperature": 0.0, "water_temp": 0.0, "wave_height": 0.0}
    
    t_el = soup.select_one('.is-direction .weather1_bodyUnitLabelData')
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
            if cls.startswith('is-wind') and cls != 'is-windDirection':
                num = cls.replace('is-wind', '')
                if num.isdigit():
                    dir_map = {1: "追い風", 2: "右斜め追い風", 3: "右斜め追い風", 4: "右斜め追い風", 5: "右横風", 9: "向かい風", 13: "左横風", 14: "左斜め追い風", 15: "左斜め追い風", 16: "左斜め追い風"}
                    env['wind_direction'] = dir_map.get(int(num), "斜め風")
    
    if env['wind_speed'] == 0.0: env['wind_direction'] = "無風"
    race_data["environment"] = env

    for tbody in soup.select('.table1 tbody.is-fs12'):
        tds = tbody.find_all('tr')[0].find_all('td')
        if len(tds) < 6: continue
        boat_no = str(int(tds[0].text.strip()))
        race_data["racelist"][boat_no].update({
            "exhibition_time": extract_float(tds[4].text),
            "tilt": extract_float(tds[5].text)
        })

def get_3_combo_odds(jcd, rno, hd, is_trio, race_data):
    odds_type = 'odds3f' if is_trio else 'odds3t'
    key_name = '3連複' if is_trio else '3連単'
    sep = '=' if is_trio else '-'
    url = f"https://www.boatrace.jp/owpc/pc/race/{odds_type}?rno={rno}&jcd={jcd}&hd={hd}"
    res = requests.get(url, headers=HEADERS); res.encoding = 'utf-8'
    soup = BeautifulSoup(res.text, 'html.parser')
    tbody = soup.select_one('tbody.is-p3-0')
    if not tbody: return
    current_snd, rem_rowspan = [None]*6, [0]*6
    for row in tbody.select('tr'):
        tds = row.find_all('td'); td_idx = 0
        for col_idx in range(6):
            if td_idx >= len(tds): break
            if rem_rowspan[col_idx] == 0:
                snd_td = tds[td_idx]; td_idx += 1
                trd_td = tds[td_idx] if td_idx < len(tds) else None; td_idx += 1
                odds_td = tds[td_idx] if td_idx < len(tds) else None; td_idx += 1
                current_snd[col_idx], rem_rowspan[col_idx] = snd_td, int(snd_td.get('rowspan', 1)) if snd_td else 1
            else:
                snd_td = current_snd[col_idx]
                trd_td = tds[td_idx] if td_idx < len(tds) else None; td_idx += 1
                odds_td = tds[td_idx] if td_idx < len(tds) else None; td_idx += 1
            rem_rowspan[col_idx] -= 1
            if snd_td and trd_td and odds_td and "is-disabled" not in snd_td.get('class', []):
                odds_val = extract_float(odds_td.text)
                if odds_val > 0:
                    combo = f"{col_idx + 1}{sep}{snd_td.text.strip()}{sep}{trd_td.text.strip()}"
                    race_data["odds"][key_name][combo] = odds_val

def get_2_combo_odds(jcd, rno, hd, race_data):
    url = f"https://www.boatrace.jp/owpc/pc/race/odds2tf?rno={rno}&jcd={jcd}&hd={hd}"
    res = requests.get(url, headers=HEADERS); res.encoding = 'utf-8'
    tbodies = BeautifulSoup(res.text, 'html.parser').select('tbody.is-p3-0')
    if len(tbodies) > 0:
        for row in tbodies[0].select('tr'):
            tds = row.find_all('td')
            for c in range(6):
                if c*2+1 < len(tds) and "is-disabled" not in tds[c*2].get('class', []):
                    race_data["odds"]["2連単"][f"{c+1}-{tds[c*2].text.strip()}"] = extract_float(tds[c*2+1].text)
    if len(tbodies) > 1:
        for row in tbodies[1].select('tr'):
            tds = row.find_all('td')
            for c in range(6):
                if c*2+1 < len(tds) and "is-disabled" not in tds[c*2].get('class', []):
                    race_data["odds"]["2連複"][f"{c+1}={tds[c*2].text.strip()}"] = extract_float(tds[c*2+1].text)

# --- UI構築 ---
st.title("🚀 Real-Time Physics Trader v2.2")
st.caption("Deterministic Void & Wake Rejection Analysis Engine")

with st.sidebar:
    st.header("Race Settings")
    input_jcd = st.selectbox("開催場", list(JCD_MAP.keys()))
    target_rno = st.number_input("レース番号(R)", 1, 12, 12)
    target_date = st.date_input("日付", datetime.now()).strftime('%Y%m%d')
    execute = st.button("物理解析エンジン起動")

if execute:
    target_jcd = JCD_MAP[input_jcd]
    race_data = {
        "metadata": {"date": target_date, "stadium": input_jcd, "race_number": f"{target_rno}R"},
        "environment": {},
        "racelist": {str(i): {} for i in range(1, 7)},
        "odds": {"3連単": {}, "3連複": {}, "2連単": {}, "2連複": {}, "拡連複": {}, "単勝": {}, "複勝": {}}
    }

    with st.spinner("流体データおよびオッズを取得中..."):
        get_racelist(target_jcd, target_rno, target_date, race_data)
        get_beforeinfo(target_jcd, target_rno, target_date, race_data)
        get_3_combo_odds(target_jcd, target_rno, target_date, False, race_data) # 3連単
        get_3_combo_odds(target_jcd, target_rno, target_date, True, race_data)  # 3連複
        get_2_combo_odds(target_jcd, target_rno, target_date, race_data)        # 2連単複

    # --- 物理判定アルゴリズム (固有ロジック) ---
    st.header("🛡️ Physics Analysis Report")
    
    # 状態判定用フラグ
    void_flags = []
    wake_rejection = []

    cols = st.columns(6)
    for i in range(1, 7):
        b = race_data["racelist"][str(i)]
        with cols[i-1]:
            st.metric(f"{i}号艇", f"{b.get('exhibition_time', 0)}s")
            st.caption(f"{b.get('name', '不明')} ({b.get('class', '-')})")
            
            # ロジック1: Deterministic Void
            if i < 6:
                st_diff = b.get('avg_st', 0) - race_data["racelist"][str(i+1)].get('avg_st', 0)
                if abs(st_diff) >= 0.08:
                    st.error("⚠️ Void Detected")
                    void_flags.append(f"{i}-{i+1}間")

            # ロジック3: Wake Rejection (航跡拒絶)
            if i > 1:
                inner_ex = race_data["racelist"][str(i-1)].get('exhibition_time', 9.9)
                ex_diff = inner_ex - b.get('exhibition_time', 0)
                if ex_diff >= 0.07:
                    st.info("🌊 Wake Rejection")
                    wake_rejection.append(f"{i}号艇による突破")
                elif ex_diff <= 0.06 and b.get('class') == 'A1':
                    st.success("⚡ A1 Breakthrough")

    # --- サマリ表示 ---
    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Environment")
        st.table([race_data["environment"]])
    with c2:
        st.subheader("Physics Log")
        if void_flags: st.write(f"【真空】{', '.join(void_flags)}")
        if wake_rejection: st.write(f"【航跡拒絶】{', '.join(wake_rejection)}")
        if not void_flags and not wake_rejection: st.write("特筆すべき物理干渉なし")

    # --- Raw Data (JSON形式) ---
    st.subheader("Raw AI Data (JSON)")
    st.json(race_data)

    # ダウンロードボタン
    json_str = json.dumps(race_data, ensure_ascii=False, indent=2)
    st.download_button("JSONをダウンロード", json_str, file_name=f"{target_date}_{input_jcd}_{target_rno}R.json")
