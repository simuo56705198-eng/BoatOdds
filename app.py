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
    m = re.search(r'[\d\.]+', str(text))
    return float(m.group()) if m else 0.0

# --- スクレイピング・エンジン ---

def get_racelist(jcd, rno, hd, race_data):
    url = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={jcd}&hd={hd}"
    res = requests.get(url, headers=HEADERS); res.encoding = 'utf-8'
    soup = BeautifulSoup(res.text, 'html.parser')
    
    # 出走表テーブルの各行を解析
    tbodies = soup.select('.table1.is-tableFixed__3rdadd tbody.is-fs12')
    for tbody in tbodies:
        tds = tbody.find_all('tr')[0].find_all('td')
        if len(tds) < 3: continue
        
        # 艇番の取得 (全角数字にも対応)
        b_no_raw = tds[0].text.strip()
        b_no_match = re.search(r'[1-6１-６]', b_no_raw)
        if not b_no_match: continue
        # 全角を半角に変換して数値化
        b_no = str(int(b_no_match.group().translate(str.maketrans('１２３４５６', '123456'))))

        # 級別（クラス）の取得ロジック修正
        # クラス情報が入っている div (is-fs11) 内の span を取得
        class_info_div = tbody.select_one('div.is-fs11')
        rank = ""
        if class_info_div:
            rank_span = class_info_div.select_one('span')
            if rank_span:
                rank = rank_span.text.strip()

        # 名前
        name = tbody.select_one('.is-fs18.is-fBold').text.strip().replace('\u3000', ' ')
        
        # 体重 (td[2] に登録番号/級別/氏名/出身地/年齢/体重がまとまっている)
        weight_match = re.search(r'([\d\.]+)kg', tds[2].text)
        weight = float(weight_match.group(1)) if weight_match else 0.0

        # 平均ST (td[3] の最後の一行)
        st_txt = [x.strip() for x in tds[3].text.split('\n') if x.strip()]
        
        # モーター番号と2連率 (td[6])
        mot = [x.strip() for x in tds[6].text.split('\n') if x.strip()]
        
        race_data["racelist"][b_no].update({
            "name": name, 
            "class": rank, 
            "weight": weight, 
            "motor_no": mot[0] if mot else '-',
            "motor_2ren": mot[1] if len(mot)>1 else '-', 
            "avg_st": extract_float(st_txt[-1]) if st_txt else 0.0
        })

def get_beforeinfo(jcd, rno, hd, race_data):
    url = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={hd}"
    res = requests.get(url, headers=HEADERS); res.encoding = 'utf-8'
    soup = BeautifulSoup(res.text, 'html.parser')
    env = race_data["environment"]
    
    # 水面気象情報の抽出
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

    # 風向き
    wd_img = soup.select_one('.is-windDirection .weather1_bodyUnitImage')
    if wd_img and wd_img.has_attr('class'):
        for cls in wd_img['class']:
            if cls.startswith('is-wind') and cls != 'is-windDirection':
                num = int(cls.replace('is-wind', ''))
                dir_map = {i: "追い風" if i in [1,2,3,4,14,15,16] else "横風" if i in [5,13] else "向かい風" for i in range(1,17)}
                env['wind_direction'] = dir_map.get(num, "無風")
    if env.get('wind_speed') == 0.0: env['wind_direction'] = "無風"

    # 展示タイム・チルト
    for tbody in soup.select('.table1 tbody.is-fs12'):
        tds = tbody.find_all('tr')[0].find_all('td')
        if len(tds) >= 6:
            b_no = str(int(tds[0].text.strip()))
            race_data["racelist"][b_no].update({
                "exhibition_time": extract_float(tds[4].text), 
                "tilt": extract_float(tds[5].text)
            })

    # スタート展示の解析
    st_ex_divs = soup.select('.table1_boatImage1')
    for course_idx, div in enumerate(st_ex_divs, 1):
        b_no_el = div.select_one('.table1_boatImage1Number')
        st_time_el = div.select_one('.table1_boatImage1Time')
        if b_no_el and st_time_el:
            b_no_match = re.search(r'\d+', b_no_el.text)
            if b_no_match:
                b_no = b_no_match.group()
                st_val = st_time_el.text.strip()
                race_data["racelist"][b_no].update({
                    "start_course": course_idx,
                    "start_exhibition_st": st_val
                })

def fetch_all_odds(jcd, rno, hd, race_data):
    # 連番系オッズ (3連単, 3連複, 2連単, 2連複)
    for otype in ['odds3t', 'odds3f', 'odds2tf']:
        res = requests.get(f"https://www.boatrace.jp/owpc/pc/race/{otype}?rno={rno}&jcd={jcd}&hd={hd}")
        soup = BeautifulSoup(res.text, 'html.parser')
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

    # 拡連複
    resk = requests.get(f"https://www.boatrace.jp/owpc/pc/race/oddsk?rno={rno}&jcd={jcd}&hd={hd}")
    tbk = BeautifulSoup(resk.text, 'html.parser').select_one('tbody.is-p3-0')
    if tbk:
        for row in tbk.select('tr'):
            tds = row.find_all('td')
            for c in range(6):
                if c*2+1 < len(tds) and "is-disabled" not in tds[c*2].get('class', []):
                    race_data["odds"]["拡連複"][f"{c+1}={tds[c*2].text.strip()}"] = tds[c*2+1].text.strip()

    # 単勝・複勝
    res_tf = requests.get(f"https://www.boatrace.jp/owpc/pc/race/oddstf?rno={rno}&jcd={jcd}&hd={hd}")
    soup_tf = BeautifulSoup(res_tf.text, 'html.parser')
    
    for unit in soup_tf.select('.grid_unit'):
        label_el = unit.select_one('.title7_mainLabel')
        if not label_el: continue
        
        label_text = label_el.text
        mode = "単勝" if "単勝" in label_text else "複勝" if "複勝" in label_text else None
        if not mode: continue
        
        for tr in unit.select('table tbody tr'):
            tds = tr.select('td')
            if len(tds) < 3: continue
            b_no = tds[0].text.strip()
            val = tds[2].text.strip()
            if "is-disabled" not in tds[2].get('class', []):
                if mode == "単勝":
                    race_data["odds"]["単勝"][b_no] = extract_float(val)
                else:
                    race_data["odds"]["複勝"][b_no] = val

# --- UI & 解析ロジック ---
st.title("🚀 Real-Time Physics Trader v2.2")

with st.sidebar:
    st.header("Race Settings")
    input_jcd = st.selectbox("開催場", list(JCD_MAP.keys()))
    target_rno = st.number_input("レース番号(R)", 1, 12, 12)
    target_date = st.date_input("日付", datetime.now()).strftime('%Y%m%d')
    execute = st.button("物理解析エンジン 起動")

if execute:
    target_jcd = JCD_MAP[input_jcd]
    race_data = {
        "metadata": {"date": target_date, "stadium": input_jcd, "race_number": f"{target_rno}R"},
        "environment": {}, "racelist": {str(i): {} for i in range(1, 7)},
        "odds": {"3連単": {}, "3連複": {}, "2連単": {}, "2連複": {}, "拡連複": {}, "単勝": {}, "複勝": {}}
    }

    with st.status("同期中...", expanded=True) as status:
        get_racelist(target_jcd, target_rno, target_date, race_data)
        get_beforeinfo(target_jcd, target_rno, target_date, race_data)
        fetch_all_odds(target_jcd, target_rno, target_date, race_data)
        status.update(label="解析準備完了", state="complete")

    # --- JSONダウンロードボタンを最上部に移動 ---
    json_export = json.dumps(race_data, ensure_ascii=False, indent=2)
    st.download_button(
        label="📥 AI解析用JSONをダウンロード",
        data=json_export,
        file_name=f"{target_date}_{input_jcd}_{target_rno}R_AIデータ.json",
        mime="application/json"
    )

    # --- 物理レポート ---
    st.header("🛡️ Physics Analysis Report")
    
    b1 = race_data["racelist"]["1"]
    if b1.get('exhibition_time', 0) > 0:
        ex_times = [race_data["racelist"][str(i)].get('exhibition_time', 0) for i in range(1,7) if race_data["racelist"][str(i)].get('exhibition_time', 0) > 0]
        if ex_times and b1.get('exhibition_time', 0) == max(ex_times):
            st.error("📉 Conditional Renormalization: 1号艇に物理的欠陥を探知。確率空間を再計算してください。")

    cols = st.columns(6)
    for i in range(1, 7):
        b = race_data["racelist"][str(i)]
        with cols[i-1]:
            ex_time = b.get('exhibition_time', 0)
            st.metric(f"{i}号艇", f"{ex_time}s")
            
            # --- 展示タイム0.0のアラート ---
            if ex_time == 0 or ex_time == 0.0:
                st.warning("⚠️ 計測不能")
            
            st.write(f"展示進入: {b.get('start_course', '-')}コース")
            st.write(f"展示ST: {b.get('start_exhibition_st', '-')}")
            st.caption(f"{b.get('name')} ({b.get('class', '-')}) / {b.get('weight', 0.0)}kg")
            
            if i < 6:
                next_b = race_data["racelist"][str(i+1)]
                if abs(b.get('avg_st', 0) - next_b.get('avg_st', 0)) >= 0.08:
                    st.warning("⚠️ Void")
            
            if i > 1:
                prev_b = race_data["racelist"][str(i-1)]
                diff = prev_b.get('exhibition_time', 0) - b.get('exhibition_time', 0)
                if diff >= 0.07: st.error("🌊 Wake Rejection")
                elif diff <= 0.06 and b.get('class') == 'A1': st.success("⚡ Skill Offset")

    st.subheader("Raw AI Data")
    st.json(race_data)
