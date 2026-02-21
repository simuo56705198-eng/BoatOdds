import streamlit as st
import requests
import json
import re
import time
from bs4 import BeautifulSoup
from datetime import datetime
import concurrent.futures
import csv
import os

# --- 初期設定 ---
st.set_page_config(page_title="Real-Time Physics Trader v2.2 - Balanced Filter", layout="wide")
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
JCD_MAP = {
    "桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05",
    "浜名湖": "06", "蒲郡": "07", "常滑": "08", "津": "09", "三国": "10",
    "びわこ": "11", "住之江": "12", "尼崎": "13", "鳴門": "14", "丸亀": "15",
    "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20",
    "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24"
}

# 外部CSVの代用（モーター交換月データ）
MOTOR_MONTHS = {
    "桐生": 12, "戸田": 7, "江戸川": 8, "平和島": 6, "多摩川": 8,
    "浜名湖": 9, "蒲郡": 5, "常滑": 12, "津": 9, "三国": 4,
    "びわこ": 6, "住之江": 3, "尼崎": 4, "鳴門": 4, "丸亀": 11,
    "児島": 1, "宮島": 11, "徳山": 5, "下関": 2, "若松": 12,
    "芦屋": 5, "福岡": 6, "唐津": 8, "大村": 6
}

def extract_float(text):
    if not text: return 0.0
    m = re.search(r'-?[\d\.]+', str(text))
    return float(m.group()) if m else 0.0

# --- スクレイピング・エンジン ---

def fetch_html(url, session, retries=3):
    for i in range(retries):
        try:
            res = session.get(url, timeout=10)
            res.raise_for_status()
            res.encoding = 'utf-8'
            return res.text
        except Exception:
            if i == retries - 1:
                return ""
            time.sleep(1)

def parse_racelist(html_text, race_data):
    if not html_text: return
    soup = BeautifulSoup(html_text, 'html.parser')
    tbodies = soup.select('.table1.is-tableFixed__3rdadd tbody.is-fs12')
    for tbody in tbodies:
        tds = tbody.find_all('tr')[0].find_all('td')
        if len(tds) < 8: continue
        
        b_no_raw = tds[0].text.strip()
        b_no_match = re.search(r'[1-6１-６]', b_no_raw)
        if not b_no_match: continue
        b_no = str(int(b_no_match.group().translate(str.maketrans('１２３４５６', '123456'))))

        class_info_div = tbody.select_one('div.is-fs11')
        rank = ""
        if class_info_div:
            rank_span = class_info_div.select_one('span')
            if rank_span:
                rank = rank_span.text.strip()

        name = tbody.select_one('.is-fs18.is-fBold').text.strip().replace('\u3000', ' ')
        
        weight_match = re.search(r'([\d\.]+)kg', tds[2].text)
        weight = float(weight_match.group(1)) if weight_match else 0.0

        st_txt = [x.strip() for x in tds[3].get_text(separator='\n').split('\n') if x.strip()]
        mot = [x.strip() for x in tds[6].get_text(separator='\n').split('\n') if x.strip()]
        
        race_data["racelist"][b_no].update({
            "name": name, 
            "class": rank, 
            "weight": weight, 
            "motor_no": mot[0] if mot else '-',
            "motor_2ren": extract_float(mot[1]) if len(mot)>1 else 30.0, 
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
                except ValueError:
                    pass
    if env.get('wind_speed') == 0.0: env['wind_direction'] = "無風"

    for tbody in soup.select('.table1 tbody'):
        trs = tbody.find_all('tr')
        if not trs: continue
        tds = trs[0].find_all('td')
        
        b_no = None
        boat_idx = -1
        for i, td in enumerate(tds):
            if td.get('class') and any(c.startswith('is-boatColor') for c in td.get('class')):
                match = re.search(r'\d+', td.text)
                if match:
                    b_no = match.group()
                    boat_idx = i
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
                    race_data["racelist"][b_no].update({
                        "start_course": course_idx,
                        "start_exhibition_st": st_val
                    })

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
                b_no = tds[0].text.strip()
                val = tds[2].text.strip()
                if "is-disabled" not in tds[2].get('class', []):
                    if mode == "単勝":
                        race_data["odds"]["単勝"][b_no] = extract_float(val)
                    else:
                        race_data["odds"]["複勝"][b_no] = val

# --- 緩和版：絶対的除外フィルター (Step 0) ---
def evaluate_ken_conditions(race_data):
    reasons = []
    env = race_data.get("environment", {})
    rl = race_data.get("racelist", {})
    stadium = race_data["metadata"]["stadium"]
    
    valid_ex_times = [d.get("exhibition_time", 0.0) for d in rl.values() if d.get("exhibition_time", 0.0) > 0]
    if len(valid_ex_times) == 0:
        return ["NOT_READY"]

    # 1. データ汚染判定 (緩和：交換月そのもののみ排除)
    month = int(race_data["metadata"]["date"][4:6])
    motor_month = MOTOR_MONTHS.get(stadium, 1)
    diff_month = month - motor_month
    if diff_month < 0: diff_month += 12
    if diff_month == 0:  # 1ヶ月以内(<=1)から、交換当月(==0)に緩和
        reasons.append(f"データ汚染限界: モーター交換({motor_month}月)直後のため平滑化未了")

    # 2. 異常気象・極限流体カオス (緩和)
    wind = env.get("wind_speed", 0.0)
    wave = env.get("wave_height", 0.0)
    if wind >= 8.0:
        reasons.append(f"異常気象限界: 風速が8m/s以上 ({wind}m/s)")
    if stadium == "江戸川" and (wave >= 6.0 or wind >= 7.0): # 5.0 -> 6.0/7.0に緩和
        reasons.append(f"極限流体カオス (江戸川): 物理的限界値超過")
    if stadium == "びわこ" and wind >= 5.0: # 4.0 -> 5.0に緩和
        reasons.append(f"極限流体カオス (びわこ): 風速5m/s以上")

    # 3. 幾何学的カオス (緩和：B級5名以上)
    b_class_count = sum(1 for d in rl.values() if d.get("class") in ["B1", "B2", ""])
    if stadium in ["戸田", "尼崎"] and b_class_count >= 5: # 4 -> 5名に緩和
        reasons.append(f"幾何学的カオス誘発 ({stadium}): B級選手が5名以上参戦")

    # 4. 住之江特効判定 (緩和：0.08s)
    if stadium == "住之江":
        ex_times = [d["exhibition_time"] for d in rl.values() if d.get("exhibition_time", 0.0) > 0]
        avg_et = sum(ex_times) / len(ex_times)
        limit_et = 0.05 if env.get("weather") in ["雨", "雪"] else 0.08 # 0.03/0.05 -> 0.05/0.08に緩和
        for b_no in ["1", "2", "3"]:
            d = rl.get(b_no, {})
            if d.get("class") not in ["A1", "A2"] and d.get("exhibition_time", 0.0) > 0:
                if (d["exhibition_time"] - avg_et) >= limit_et:
                    reasons.append(f"極限流体カオス (住之江): {b_no}号艇の遅延が許容限界を突破")

    # 5. 前付け (緩和：1号艇が1コースを守っていればOKとする)
    if rl.get("1", {}).get("start_course") != 1:
        reasons.append("初期値崩壊: 1号艇がインコースを奪取されました")

    # 6. 展示スナップショット乖離 (緩和：0.15s / 0.20s)
    for b_no, d in rl.items():
        st_str = d.get("start_exhibition_st", "").replace("F", "").replace("L", "").replace(".", "0.")
        try:
            st_val = float(st_str) if st_str else 0.25
        except ValueError:
            st_val = 0.25
        
        diff = abs(st_val - d.get("avg_st", 0.15))
        limit_st = 0.20 if d.get("class") in ["A1", "A2"] else 0.15 # 0.15/0.10 -> 0.20/0.15に緩和
        if diff >= limit_st:
            reasons.append(f"展示乖離: {b_no}号艇のSTノイズが限界突破({diff:.2f})")

    return list(set(reasons))

# --- UI & 解析ロジック ---
st.title("🚀 Real-Time Physics Trader v2.2 - Balanced Filter")

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
        st.write("🌐 通信セッションを確立し、7つのページを並列取得中...")
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
                key = future_to_key[future]
                html_data[key] = future.result()

        st.write("🧠 取得したHTMLデータを解析中...")
        
        parse_racelist(html_data.get("racelist"), race_data)
        parse_beforeinfo(html_data.get("beforeinfo"), race_data)
        parse_all_odds(html_data, race_data)

        status.update(label="解析準備完了", state="complete")

    # --- 事前「見（ケン）」フィルターの実行 ---
    ken_reasons = evaluate_ken_conditions(race_data)
    
    if ken_reasons == ["NOT_READY"]:
        st.warning("⏳ **【情報未公開】** 直前情報がまだ公開されていません。")
    elif ken_reasons:
        st.error("🚨 **【見（ケン）推奨レース】** 以下の致命的ノイズが検知されました。")
        for r in ken_reasons:
            st.warning(f"・ {r}")
    else:
        st.success("✅ **【ノイズクリア】** AIへ解析を依頼してください。")

    # --- バックテスト用ロギング関数 ---
    def log_race_data_to_csv(race_data, ken_reasons):
        log_file = "rtpt_backtest_log.csv"
        file_exists = os.path.isfile(log_file)
        
        env = race_data.get("environment", {})
        rl = race_data.get("racelist", {})
        meta = race_data.get("metadata", {})
        
        # 記録するデータをフラットに展開
        log_row = {
            "date": meta.get("date"),
            "stadium": meta.get("stadium"),
            "race_number": meta.get("race_number"),
            "wind_speed": env.get("wind_speed", 0.0),
            "wave_height": env.get("wave_height", 0.0),
            "ken_filter_passed": "Yes" if not ken_reasons else "No",
            "ken_reasons": " | ".join(ken_reasons) if ken_reasons else ""
        }
        
        for i in range(1, 7):
            b = rl.get(str(i), {})
            log_row[f"boat{i}_class"] = b.get("class", "")
            log_row[f"boat{i}_motor2ren"] = b.get("motor_2ren", 0.0)
            log_row[f"boat{i}_ex_time"] = b.get("exhibition_time", 0.0)
            log_row[f"boat{i}_avg_st"] = b.get("avg_st", 0.0)
            log_row[f"boat{i}_ex_st"] = b.get("start_exhibition_st", "")
        
        # CSV書き込み
        fieldnames = list(log_row.keys())
        with open(log_file, mode="a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(log_row)
    
    # 実行ブロック（ # --- JSONダウンロード --- の直前に配置）
    log_race_data_to_csv(race_data, ken_reasons)
    
    # --- JSONダウンロード ---
    json_export = json.dumps(race_data, ensure_ascii=False, indent=2)
    st.download_button(
        label="📥 AI解析用JSONをダウンロード",
        data=json_export,
        file_name=f"{target_date}_{input_jcd}_{target_rno}R_AIデータ.json",
        mime="application/json"
    )

    # --- 物理レポート表示 (ここもフル復活) ---
    st.header("🛡️ Physics Analysis Report")
    
    b1 = race_data["racelist"]["1"]
    if b1.get('exhibition_time', 0) > 0:
        ex_times = [race_data["racelist"][str(i)].get('exhibition_time', 0) for i in range(1,7) if race_data["racelist"][str(i)].get('exhibition_time', 0) > 0]
        if ex_times and b1.get('exhibition_time', 0) == max(ex_times):
            st.error("📉 Conditional Renormalization: 1号艇に物理的欠陥を探知。")

    cols = st.columns(6)
    for i in range(1, 7):
        b = race_data["racelist"][str(i)]
        with cols[i-1]:
            ex_time = b.get('exhibition_time', 0)
            st.metric(f"{i}号艇", f"{ex_time}s" if ex_time > 0 else "-")
            
            if ex_time > 0:
                st.write(f"展示進入: {b.get('start_course', '-')}コース")
                st.write(f"展示ST: {b.get('start_exhibition_st', '-')}")
                st.caption(f"{b.get('name', '取得エラー')} ({b.get('class', '-')}) / {b.get('weight', 0.0)}kg")
                
                # 隣接判定ロジックのUI表示
                if i < 6:
                    next_b = race_data["racelist"][str(i+1)]
                    if next_b.get('avg_st'):
                        if abs(b.get('avg_st', 0) - next_b.get('avg_st', 0)) >= 0.08:
                            st.warning("⚠️ Void Risk")
                
                if i > 1:
                    prev_b = race_data["racelist"][str(i-1)]
                    if prev_b.get('exhibition_time'):
                        diff = prev_b.get('exhibition_time', 0) - b.get('exhibition_time', 0)
                        if diff >= 0.07: st.error("🌊 Wake Rejection")
                        elif diff <= 0.06 and b.get('class') == 'A1': st.success("⚡ Skill Offset")

    with st.expander("Raw AI Data を確認"):
        st.json(race_data)

