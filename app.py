"""
app.py v2.0 — RTPT v7.3 + BankrollManager + Archiver 統合版
=============================================================
変更点:
  - bankroll_manager 統合（日次リスク管理）
  - race_data_archive への自動保存
  - 結果照合ボタン追加
  - Calibration / Performance ダッシュボード
  - 警告表示の追加
  - レース選択後の展示待ちガード強化
"""
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

from rtpt_engine import analyze
from bankroll_manager import BankrollManager
from backtest_system import Reconciler, PerformanceAnalyzer, CalibrationChecker, RaceDataArchiver

# MLモデルのインポート（エラー回避付き）
try:
    from ml_model import BoatRaceMLModel
    HAS_ML_MODEL = True
except ImportError:
    HAS_ML_MODEL = False

# 安全にインポート（モジュールがない場合でも動作）
try:
    from data_quality import DataQualityMonitor
    HAS_DATA_QUALITY = True
except ImportError:
    HAS_DATA_QUALITY = False

try:
    from tide_data import TideInjector
    HAS_TIDE = True
except ImportError:
    HAS_TIDE = False

try:
    from race_selector import RaceFilter
    HAS_RACE_SELECTOR = True
except ImportError:
    HAS_RACE_SELECTOR = False

# --- 初期設定 ---
st.set_page_config(page_title="RTPT v7.5 — Production Engine", layout="wide")
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
JCD_MAP = {
    "桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05",
    "浜名湖": "06", "蒲郡": "07", "常滑": "08", "津": "09", "三国": "10",
    "びわこ": "11", "住之江": "12", "尼崎": "13", "鳴門": "14", "丸亀": "15",
    "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20",
    "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24"
}
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "predictions_log.csv")
ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "race_data_archive")

def extract_float(text):
    if not text: return 0.0
    m = re.search(r'-?[\d\.]+', str(text))
    return float(m.group()) if m else 0.0


# --- スクレイピング（app.py v1からほぼ同一、省略部分はコメントで示す） ---

@st.cache_data(ttl=60)
def fetch_available_races(target_date):
    url = f"https://www.boatrace.jp/owpc/pc/race/index?hd={target_date}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        res.encoding = 'utf-8'
        available_dict = {}
        tbodies = re.finditer(r'<tbody.*?>.*?</tbody>', res.text, re.DOTALL)
        for match in tbodies:
            tbody_html = match.group(0)
            stadium_match = re.search(r'alt="([^"]+)"', tbody_html)
            if not stadium_match: continue
            name = stadium_match.group(1).strip()
            if name not in JCD_MAP: continue
            if "最終Ｒ発売終了" in tbody_html or "中止" in tbody_html: continue
            current_r = 1
            r_match = re.search(r'>(\d{1,2})R<', tbody_html)
            if r_match: current_r = int(r_match.group(1))
            available_dict[name] = list(range(current_r, 13))
        return available_dict
    except Exception:
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

        # [Bug#29修正] 登録番号を抽出（recent_form.pyのinjectに必要）
        toban = ""
        toban_link = tbody.select_one('a[href*="toban"]')
        if toban_link:
            tm = re.search(r'toban=(\d+)', toban_link.get('href', ''))
            if tm: toban = tm.group(1)

        weight_match = re.search(r'([\d\.]+)kg', tds[2].text)
        weight = float(weight_match.group(1)) if weight_match else 0.0
        st_txt = [x.strip() for x in tds[3].get_text(separator='\n').split('\n') if x.strip()]
        nat_win = [x.strip() for x in tds[4].get_text(separator='\n').split('\n') if x.strip()]
        loc_win = [x.strip() for x in tds[5].get_text(separator='\n').split('\n') if x.strip()]
        mot = [x.strip() for x in tds[6].get_text(separator='\n').split('\n') if x.strip()]
        race_data["racelist"][b_no].update({
            "name": name, "class": rank, "weight": weight,
            "racer_no": toban,
            "win_rate_national": extract_float(nat_win[0]) if nat_win else 0.0,
            "win_rate_local": extract_float(loc_win[0]) if loc_win else 0.0,
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
                    env['wind_direction_code'] = num  # [Bug#24] 生コード保存
                    dm = {i: "追い風" if i in [1,2,3,4,14,15,16] else "横風" if i in [5,13] else "向かい風" for i in range(1,17)}
                    env['wind_direction'] = dm.get(num, "無風")
                except ValueError: pass
    if env.get('wind_speed') == 0.0: env['wind_direction'] = "無風"
    for tbody in soup.select('.table1 tbody'):
        trs = tbody.find_all('tr')
        if not trs: continue
        tds = trs[0].find_all('td')
        b_no = None; bi = -1
        for i, td in enumerate(tds):
            if td.get('class') and any(c.startswith('is-boatColor') for c in td.get('class')):
                match = re.search(r'\d+', td.text)
                if match: b_no = match.group(); bi = i
                break
        if b_no and bi != -1 and b_no in race_data["racelist"]:
            if len(tds) > bi + 4:
                race_data["racelist"][b_no].update({
                    "tilt": extract_float(tds[bi + 3].text),
                    "exhibition_time": extract_float(tds[bi + 4].text)
                })
    for ci, div in enumerate(soup.select('.table1_boatImage1'), 1):
        bn_el = div.select_one('.table1_boatImage1Number')
        st_el = div.select_one('.table1_boatImage1Time')
        if bn_el and st_el:
            m = re.search(r'\d+', bn_el.text)
            if m:
                b = m.group()
                if b in race_data["racelist"]:
                    race_data["racelist"][b].update({"start_course": ci, "start_exhibition_st": st_el.text.strip()})


def parse_all_odds(html_dict, race_data):
    """オッズパーサー（v1から継承。TODO: リファクタリング対象）"""
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
                    if rem_row[c] == 0:
                        if idx + 2 >= len(tds): break
                        snd_td, trd_td, o_td = tds[idx], tds[idx+1], tds[idx+2]; idx += 3
                        cur_snd[c], rem_row[c] = snd_td, int(snd_td.get('rowspan', 1))
                    else:
                        if idx + 1 >= len(tds): break
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
            lt = label_el.text
            mode = "単勝" if "単勝" in lt else "複勝" if "複勝" in lt else None
            if not mode: continue
            for tr in unit.select('table tbody tr'):
                tds = tr.select('td')
                if len(tds) < 3: continue
                bn = tds[0].text.strip(); val = tds[2].text.strip()
                if "is-disabled" not in tds[2].get('class', []):
                    if mode == "単勝": race_data["odds"]["単勝"][bn] = extract_float(val)
                    else: race_data["odds"]["複勝"][bn] = val


# ============================================================
# UI
# ============================================================
st.title("🎯 RTPT v7.5 — Production Engine")
st.caption(f"Multi-Market TMP | 10αソース + MLブレンド {ml_status_msg if 'ml_status_msg' in locals() else ''} | 潮汐交互作用 | Selection Bias補正 | Quarter Kelly")

# --- Sidebar ---
with st.sidebar:
    st.header("⚙️ Settings")
    target_date = st.date_input("日付", datetime.now()).strftime('%Y%m%d')
    initial_bankroll = st.number_input("💰 初期バンクロール（円）", min_value=100, value=10000, step=1000)

    # MLモデルのロード
    ml_model_instance = None
    ml_status_msg = "（Harville数理モデルのみ）"
    if HAS_ML_MODEL:
        try:
            m = BoatRaceMLModel()
            if m.load():
                ml_model_instance = m
                ml_status_msg = "（LightGBM + Harville ブレンド）"
        except Exception as e:
            st.sidebar.warning(f"MLモデルのロードに失敗しました: {e}")

    # BankrollManager
    bm = BankrollManager(initial_bankroll=initial_bankroll)
    budget_info = bm.get_race_budget()

    # ステータス表示
    stats = budget_info["stats"]
    risk_color = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "BLOCKED": "⛔"}.get(stats["risk_level"], "")
    st.markdown(f"### {risk_color} リスク: {stats['risk_level']}")
    st.caption(f"残高: ¥{stats['current_bankroll']:,.0f} | 日次PnL: ¥{stats['daily_pnl']:+,.0f}")
    st.caption(f"DD: {stats['daily_dd_pct']:.1f}% | 連敗: {stats['losing_streak']} | 本日{stats['races_today']}R")

    if not budget_info["allowed"]:
        st.error(budget_info["reason"])
    else:
        st.success(f"予算上限: ¥{budget_info['budget']:,.0f}")
        if budget_info["reason"] != "✅ 通常運用":
            st.warning(budget_info["reason"])

    # 潮汐手動入力（潮汐場の場合）
    tide_option = st.selectbox("🌊 潮汐（該当場のみ）", ["自動推定", "満潮", "干潮", "上げ潮", "下げ潮"])
    tide_map = {"自動推定": None, "満潮": "high", "干潮": "low", "上げ潮": "flood", "下げ潮": "ebb"}

    available = fetch_available_races(target_date)
    if available:
        input_jcd = st.selectbox("🏟️ 開催場", list(available.keys()))
        target_rno = st.selectbox("🏁 レース番号(R)", available[input_jcd])
    else:
        st.caption("※全レース終了 or 取得失敗")
        input_jcd = st.selectbox("開催場", list(JCD_MAP.keys()))
        target_rno = st.selectbox("レース番号(R)", list(range(1, 13)))

    execute = st.button("🚀 解析エンジン起動", type="primary", use_container_width=True,
                        disabled=not budget_info["allowed"])

    st.divider()
    # 結果照合ボタン
    if st.button("📊 結果照合（自動）"):
        with st.spinner("結果をboatrace.jpから取得中..."):
            reconciler = Reconciler()
            try:
                updated = reconciler.reconcile(LOG_FILE)
                st.success(f"照合完了: {updated}件更新")
            except Exception as e:
                st.error(f"照合エラー: {e}")

    # Circuit Breaker リセット
    if stats["risk_level"] == "BLOCKED":
        if st.button("🔄 Circuit Breaker リセット"):
            bm.force_reset(initial_bankroll)
            st.rerun()

# --- メインタブ ---
tab_main, tab_perf, tab_cal = st.tabs(["🎯 解析", "📊 パフォーマンス", "🔬 Calibration"])

with tab_main:
    if execute:
        bankroll = budget_info["budget"]
        target_jcd = JCD_MAP[input_jcd]
        race_data = {
            "metadata": {"date": target_date, "stadium": input_jcd, "race_number": f"{target_rno}R"},
            "environment": {},
            "racelist": {str(i): {} for i in range(1, 7)},
            "odds": {"3連単": {}, "3連複": {}, "2連単": {}, "2連複": {}, "拡連複": {}, "単勝": {}, "複勝": {}}
        }

        # 潮汐データ注入
        tide_val = tide_map.get(tide_option)
        if tide_val:
            race_data["environment"]["tide"] = tide_val

        # === Scrape ===
        with st.status("📡 データ取得中...", expanded=True) as status:
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
            with concurrent.futures.ThreadPoolExecutor(max_workers=7) as ex:
                futs = {ex.submit(fetch_html, u, session): k for k, u in urls.items()}
                for f in concurrent.futures.as_completed(futs):
                    html_data[futs[f]] = f.result()

            parse_racelist(html_data.get("racelist"), race_data)
            parse_beforeinfo(html_data.get("beforeinfo"), race_data)
            parse_all_odds(html_data, race_data)

            # データ品質チェック
            missing = []
            for k in ["odds3t", "odds2tf", "oddstf"]:
                if not html_data.get(k):
                    missing.append(k)
            if missing:
                st.warning(f"⚠️ 取得失敗ページ: {', '.join(missing)}")

            status.update(label="✅ データ取得完了", state="complete")

        # === [Bug#17修正] データ品質チェック ===
        if HAS_DATA_QUALITY:
            dqm = DataQualityMonitor()
            quality = dqm.assess(race_data, html_data)
            if not quality["tradeable"]:
                st.error(f"🛑 {quality['recommendation']}")
                for e in quality["all_errors"]:
                    st.error(f"  ❌ {e}")
                st.stop()
            for w in quality["all_warnings"]:
                st.warning(f"  ⚠️ {w}")

        # === 潮汐自動注入 ===
        if HAS_TIDE:
            injector = TideInjector()
            race_data = injector.inject(race_data)

        # === レース適性チェック ===
        if HAS_RACE_SELECTOR:
            rf = RaceFilter()
            bet_decision = rf.should_bet(race_data)
            if not bet_decision["should_bet"]:
                st.warning(f"⚠️ {bet_decision['reason']}")
            else:
                st.info(f"📊 {bet_decision['reason']} (信頼度: {bet_decision['confidence']:.0%})")

        # === Analyze ===
        result = analyze(race_data, bankroll, ml_model=ml_model_instance)

        # === Archive ===（エラー時は保存しない。バックテストデータを汚染しないため）
        if not result.get("error"):
            archiver = RaceDataArchiver(ARCHIVE_DIR)
            archiver.save(race_data, result)

        # === Log ===
        if not result.get("error") and result.get("targets"):
            log_exists = os.path.isfile(LOG_FILE)
            with open(LOG_FILE, mode="a", encoding="utf-8-sig", newline="") as f:
                fields = ["date", "stadium", "race", "type", "combo",
                          "prob_pct", "odds", "ev", "kelly_pct", "recommended_yen",
                          "result_1st", "result_2nd", "result_3rd", "hit", "payout"]
                writer = csv.DictWriter(f, fieldnames=fields)
                if not log_exists: writer.writeheader()
                for t in result["targets"]:
                    writer.writerow({
                        "date": target_date, "stadium": input_jcd, "race": f"{target_rno}R",
                        "type": t["type"], "combo": t["combo"],
                        "prob_pct": f"{t['prob']*100:.1f}", "odds": t["odds"],
                        "ev": f"{t['ev']:.2f}", "kelly_pct": f"{t['kelly_pct']:.1f}",
                        "recommended_yen": t["recommended_yen"],
                        "result_1st": "", "result_2nd": "", "result_3rd": "", "hit": "", "payout": ""
                    })

        # === 警告表示 ===
        if result.get("warnings"):
            for w in result["warnings"]:
                st.warning(f"⚠️ {w}")

        # === 結果表示 ===
        if result.get("error"):
            st.warning(f"⏳ {result['error']}")
        else:
            st.header(f"🧠 {input_jcd} {target_rno}R — 物理アルファ解析")
            cols = st.columns(6)
            for bi in result["boats"]:
                with cols[bi["boat"] - 1]:
                    delta = bi["post_prob"] - bi["tmp"]
                    st.metric(f"{bi['boat']}号艇", f"{bi['post_prob']*100:.1f}%",
                              f"{delta*100:+.1f}%",
                              delta_color="normal" if delta >= 0 else "inverse")
                    st.caption(bi["name"])
                    st.caption(f"TMP:{bi['tmp']*100:.1f}% α:{bi['alpha']:.3f}")
                    if bi["wd"] < 12.0:
                        st.error(f"⚠️ WD:{bi['wd']:.0f}")
                    for r in bi["reasons"]:
                        st.caption(f"📐 {r}")

            # 投資判断
            st.header("💰 投資判断テーブル")
            summary = result["summary"]

            if summary["verdict"] == "見（ケン）":
                st.error("🛑 **EV閾値を超える買い目なし → 見送り**")
            else:
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("投資対象", f"{summary['count']}点")
                c2.metric("平均EV", f"{summary['avg_ev']:.2f}")
                c3.metric("最大EV", f"{summary['max_ev']:.2f}", summary['max_ev_combo'])
                c4.metric("合計投資", f"¥{summary['total_investment']:,}")

                table = []
                for t in result["targets"]:
                    table.append({
                        "券種": t["type"], "買い目": t["combo"],
                        "推定確率": f"{t['prob']*100:.1f}%", "オッズ": f"{t['odds']:.1f}倍",
                        "EV": f"{t['ev']:.2f}", "Kelly%": f"{t['kelly_pct']:.1f}%",
                        "推奨額": f"¥{t['recommended_yen']:,}"
                    })
                st.dataframe(table, use_container_width=True, hide_index=True)

                # Calibration情報
                cal = summary.get("calibration", {})
                if cal.get("selection_bias_applied"):
                    st.info(f"📊 Selection Bias補正適用済（候補{cal['n_candidates']}件）"
                            f" EV閾値: 2連={cal['ev_thresholds']['2連']}, 3連={cal['ev_thresholds']['3連']}")

        # JSON backup
        with st.expander("📥 JSONデータ"):
            st.download_button("Download", json.dumps(race_data, ensure_ascii=False, indent=2),
                               f"{target_date}_{input_jcd}_{target_rno}R.json", "application/json")

# --- Performance Tab ---
with tab_perf:
    st.header("📊 パフォーマンス分析")
    if os.path.exists(LOG_FILE):
        pa = PerformanceAnalyzer()
        perf = pa.analyze(LOG_FILE)
        if "error" in perf:
            st.info(perf["error"])
        else:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("総ベット数", perf["total_bets"])
            c2.metric("ROI", f"{perf['roi']:+.1f}%")
            c3.metric("Sharpe Ratio", perf["sharpe_ratio"])
            c4.metric("Max DD", f"¥{perf['max_drawdown']:,.0f}")

            c5, c6, c7, c8 = st.columns(4)
            c5.metric("的中率", f"{perf['hit_rate']:.1f}%")
            c6.metric("投資総額", f"¥{perf['total_invested']:,}")
            c7.metric("回収総額", f"¥{perf['total_payout']:,}")
            c8.metric("日平均PnL", f"¥{perf['daily_avg_pnl']:+,.0f}")

            st.subheader("券種別成績")
            type_table = []
            for bt, d in perf.get("by_type", {}).items():
                type_table.append({
                    "券種": bt, "ベット数": d["bets"], "的中": d["hits"],
                    "的中率": f"{d['hits']/max(d['bets'],1)*100:.1f}%",
                    "投資": f"¥{d['invested']:,}", "回収": f"¥{d['payout']:,}",
                    "ROI": f"{(d['payout']-d['invested'])/max(d['invested'],1)*100:+.1f}%"
                })
            st.dataframe(type_table, use_container_width=True, hide_index=True)
    else:
        st.info("まだ予想ログがありません。解析を実行してデータを蓄積してください。")

# --- Calibration Tab ---
with tab_cal:
    st.header("🔬 Calibration検証")
    st.caption("推定確率が実際の的中率と一致しているかを検証します")
    if os.path.exists(LOG_FILE):
        cc = CalibrationChecker()
        cal_result = cc.check(LOG_FILE)
        if "error" in cal_result:
            st.info(cal_result["error"])
        else:
            st.metric("Brier Score", f"{cal_result['brier_score']:.4f}",
                       cal_result["brier_interpretation"])
            st.caption(f"評価対象: {cal_result['total_evaluated']}件")

            if cal_result["buckets"]:
                cal_table = []
                for label, d in cal_result["buckets"].items():
                    cal_table.append({
                        "確率帯": label, "件数": d["n"],
                        "推定平均": f"{d['expected_rate_pct']:.1f}%",
                        "実的中率": f"{d['actual_rate_pct']:.1f}%",
                        "Gap": f"{d['gap']:+.1f}%",
                        "Calibrated": "✅" if d["calibrated"] else "❌"
                    })
                st.dataframe(cal_table, use_container_width=True, hide_index=True)

                st.subheader("Gapの読み方")
                st.markdown("""
                - **Gap > 0**: 的中率が推定より高い → 確率を過小評価（もっと賭けてよい）
                - **Gap < 0**: 的中率が推定より低い → 確率を過大評価（賭けすぎ）
                - **|Gap| < 5%**: 良好なキャリブレーション
                """)
    else:
        st.info("照合済みデータが必要です。結果照合を実行してください。")
