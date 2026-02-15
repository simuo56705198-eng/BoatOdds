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
    "浜名湖": "06", "蒲郡": "07", "常滑": "08", "津": "09", "三国": "10",
    "びわこ": "11", "住之江": "12", "尼崎": "13", "鳴門": "14", "丸亀": "15",
    "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20",
    "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24"
}

def extract_float(text):
    if not text: return 0.0
    m = re.search(r'[\d\.]+', text)
    return float(m.group()) if m else 0.0

# --- スクレイピング・コア (元のローカルロジックを完全復元) ---

def get_racelist(jcd, rno, hd, race_data):
    url = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={jcd}&hd={hd}"
    res = requests.get(url, headers=HEADERS); res.encoding = 'utf-8'
    soup = BeautifulSoup(res.text, 'html.parser')
    tbodies = soup.select('.table1.is-tableFixed__3rdadd tbody.is-fs12')
    for tbody in tbodies:
        tds = tbody.find_all('tr')[0].find_all('td')
        if len(tds) < 8: continue
        b_no = str(int(tds[0].text.strip()))
        name = tbody.select_one('.is-fs18.is-fBold').text.strip().replace('\u3000', ' ')
        rank = tbody.select_one('.is-fColor1').text.strip() if tbody.select_one('.is-fColor1') else ""
        st_txt = [x.strip() for x in tds[3].text.split('\n') if x.strip()]
        mot = [x.strip() for x in tds[6].text.split('\n') if x.strip()]
        race_data["racelist"][b_no].update({
            "name": name, "class": rank, "motor_no": mot[0] if mot else '-',
            "motor_2ren": mot[1] if len(mot)>1 else '-', "avg_st": extract_float(st_txt[-1]) if st_txt else 0.0
        })

def get_beforeinfo(jcd, rno, hd, race_data):
    url = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={hd}"
    res = requests.get(url, headers=HEADERS); res.encoding = 'utf-8'
    soup = BeautifulSoup(res.text, 'html.parser')
    env = race_data["environment"]
    
    # 環境データ
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

    # 風向き (16方位完全対応)
    wd_img = soup.select_one('.is-windDirection .weather1_bodyUnitImage')
    if wd_img and wd_img.has_attr('class'):
        for cls in wd_img['class']:
            if cls.startswith('is-wind') and cls != 'is-windDirection':
                num = int(cls.replace('is-wind', ''))
                dir_map = {1: "追い風", 2: "右斜め追い風", 3: "右斜め追い風", 4: "右斜め追い風", 5: "右横風", 
                           6: "右斜め向かい風", 7: "右斜め向かい風", 8: "右斜め向かい風", 9: "向かい風", 
                           10: "左斜め向かい風", 11: "左斜め向かい風", 12: "左斜め向かい風", 13: "左横風", 
                           14: "左斜め追い風", 15: "左斜め追い風", 16: "左斜め追い風"}
                env['wind_direction'] = dir_map.get(num, "無風")
    if env.get('wind_speed') == 0.0: env['wind_direction'] = "無風"

    # 展示
    for tbody in soup.select('.table1 tbody.is-fs12'):
        tds = tbody.find_all('tr')[0].find_all('td')
        if len(tds) >= 6:
            b_no = str(int(tds[0].text.strip()))
            race_data["racelist"][b_no].update({
                "exhibition_time": extract_float(tds[4].text), "tilt": extract_float(tds[5].text)
            })

def fetch_all_odds(jcd, rno, hd, race_data):
    # 3連単・3連複
    for is_trio in [False, True]:
        key = '3連複' if is_trio else '3連単'; sep = '=' if is_trio else '-'
        res = requests.get(f"https://www.boatrace.jp/owpc/pc/race/odds3{'f' if is_trio else 't'}?rno={rno}&jcd={jcd}&hd={hd}")
        soup = BeautifulSoup(res.text, 'html.parser')
        tbody = soup.select_one('tbody.is-p3-0')
        if not tbody: continue
        cur_snd, rem_row = [None]*6, [0]*6
        for row in tbody.select('tr'):
            tds = row.find_all('td'); idx = 0
            for c in range(6):
                if idx >= len(tds): break
                if rem_row[c] == 0:
                    snd_td, trd_td, o_td = tds[idx], tds[idx+1], tds[idx+2]
                    idx += 3; cur_snd[c] = snd_td; rem_row[c] = int(snd_td.get('rowspan', 1))
                else:
                    trd_td, o_td = tds[idx], tds[idx+1]; idx += 2; snd_td = cur_snd[c]
                rem_row[c] -= 1
                if "is-disabled" not in o_td.get('class', []):
                    race_data["odds"][key][f"{c+1}{sep}{snd_td.text.strip()}{sep}{trd_td.text.strip()}"] = extract_float(o_td.text)

    # 2連単・2連複
    res2 = requests.get(f"https://www.boatrace.jp/owpc/pc/race/odds2tf?rno={rno}&jcd={jcd}&hd={hd}")
    soup2 = BeautifulSoup(res2.text, 'html.parser'); tbs = soup2.select('tbody.is-p3-0')
    for i, key in enumerate(["2連単", "2連複"]):
        if len(tbs) > i:
            sep = '-' if i == 0 else '='
            for row in tbs[i].select('tr'):
                tds = row.find_all('td')
                for c in range(6):
                    if c*2+1 < len(tds) and "is-disabled" not in tds[c*2].get('class', []):
                        race_data["odds"][key][f"{c+1}{sep}{tds[c*2].text.strip()}"] = extract_float(tds[c*2+1].text)

    # ★拡連複 (ワイド) の復元
    resk = requests.get(f"https://www.boatrace.jp/owpc/pc/race/oddsk?rno={rno}&jcd={jcd}&hd={hd}")
    soupk = BeautifulSoup(resk.text, 'html.parser'); tbk = soupk.select_one('tbody.is-p3-0')
    if tbk:
        for row in tbk.select('tr'):
            tds = row.find_all('td')
            for c in range(6):
                if c*2+1 < len(tds) and "is-disabled" not in tds[c*2].get('class', []):
                    race_data["odds"]["拡連複"][f"{c+1}={tds[c*2].text.strip()}"] = tds[c*2+1].text.strip()

    # 単勝・複勝
    restf = requests.get(f"https://www.boatrace.jp/owpc/pc/race/oddstf?rno={rno}&jcd={jcd}&hd={hd}")
    souptf = BeautifulSoup(restf.text, 'html.parser'); tbtf = souptf.select('tbody.is-p3-0')
    for i, key in enumerate(["単勝", "複勝"]):
        if len(tbtf) > i:
            for row in tbtf[i].select('tr'):
                tds = row.find_all('td')
                if len(tds) >= 2: race_data["odds"][key][tds[0].text.strip()] = extract_float(tds[1].text)

# --- Streamlit UI ---
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
        "environment": {}, "racelist": {str(i): {} for i in range(1, 7)},
        "odds": {"3連単": {}, "3連複": {}, "2連単": {}, "2連複": {}, "拡連複": {}, "単勝": {}, "複勝": {}}
    }

    with st.status("物理データ同期中...", expanded=True) as status:
        get_racelist(target_jcd, target_rno, target_date, race_data)
        get_beforeinfo(target_jcd, target_rno, target_date, race_data)
        fetch_all_odds(target_jcd, target_rno, target_date, race_data)
        status.update(label="同期完了", state="complete")

    # --- 物理レポート ---
    st.header("🛡️ Physics Analysis Report")
    cols = st.columns(6)
    for i in range(1, 7):
        b = race_data["racelist"][str(i)]
        with cols[i-1]:
            st.metric(f"{i}号艇", f"{b.get('exhibition_time', 0)}s")
            # 物理判定: 真空
            if i < 6:
                if abs(b.get('avg_st', 0) - race_data["racelist"][str(i+1)].get('avg_st', 0)) >= 0.08:
                    st.warning("⚠️ Void")
            # 物理判定: 航跡拒絶
            if i > 1:
                if race_data["racelist"][str(i-1)].get('exhibition_time', 9.9) - b.get('exhibition_time', 0) >= 0.07:
                    st.error("🌊 Wake Rejection")

    st.subheader("Raw AI Data")
    st.json(race_data)
