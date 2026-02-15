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

# --- スクレイピング関数群 ---

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
    
    ws_el = soup.select_one('.is-wind .weather1_bodyUnitLabelData')
    if ws_el: env['wind_speed'] = extract_float(ws_el.text)
    
    # 風向き判定
    wd_img = soup.select_one('.is-windDirection .weather1_bodyUnitImage')
    if wd_img and wd_img.has_attr('class'):
        for cls in wd_img['class']:
            if cls.startswith('is-wind') and cls != 'is-windDirection':
                num = cls.replace('is-wind', '')
                if num.isdigit():
                    dir_map = {1: "追い風", 2: "右斜め追い風", 5: "右横風", 9: "向かい風", 13: "左横風"} # 簡略化
                    env['wind_direction'] = dir_map.get(int(num), "斜め風")
    
    race_data["environment"] = env

    for tbody in soup.select('.table1 tbody.is-fs12'):
        tds = tbody.find_all('tr')[0].find_all('td')
        if len(tds) < 6: continue
        boat_no = str(int(tds[0].text.strip()))
        race_data["racelist"][boat_no].update({
            "exhibition_time": extract_float(tds[4].text),
            "tilt": extract_float(tds[5].text)
        })

# --- UI構築 ---
st.title("🚀 Real-Time Physics Trader v2.2")

with st.sidebar:
    st.header("Race Settings")
    input_jcd = st.selectbox("開催場", list(JCD_MAP.keys()))
    target_rno = st.number_input("レース番号(R)", 1, 12, 12)
    target_date = st.date_input("日付", datetime.now()).strftime('%Y%m%d')
    execute = st.button("データ抽出・解析開始")

if execute:
    target_jcd = JCD_MAP[input_jcd]
    race_data = {
        "metadata": {"date": target_date, "stadium": input_jcd, "race_number": f"{target_rno}R"},
        "environment": {},
        "racelist": {str(i): {} for i in range(1, 7)},
        "odds": {"3連単": {}}
    }

    with st.spinner("物理データをスキャン中..."):
        get_racelist(target_jcd, target_rno, target_date, race_data)
        get_beforeinfo(target_jcd, target_rno, target_date, race_data)

    # --- 物理エンジン解析 (固有ロジック適用) ---
    st.header("🛡️ Physics Analysis Report")
    
    cols = st.columns(6)
    for i in range(1, 7):
        b = race_data["racelist"][str(i)]
        with cols[i-1]:
            st.metric(f"{i}号艇", f"{b.get('exhibition_time', 0)}s")
            st.caption(f"{b.get('name', '不明')} ({b.get('class', '-')})")
            
            # 1. Deterministic Void (真空判定)
            avg_st = b.get('avg_st', 0)
            if i < 6:
                next_st = race_data["racelist"][str(i+1)].get('avg_st', 0)
                if abs(avg_st - next_st) >= 0.08:
                    st.warning("⚠️ Void Detected")

    # --- 物理データサマリ ---
    st.subheader("Raw Data")
    col_env, col_raw = st.columns([1, 2])
    with col_env:
        st.write("**Environment**")
        st.json(race_data["environment"])
    with col_raw:
        st.write("**Race List**")
        st.json(race_data["racelist"])
