"""
RTPT v8.0 — AI Engine (Machine Learning)
自由意志を手放し、LightGBMが見出した宇宙のオッズの歪み（必然の流れ）にサレンダーするエンジン。
"""
import math
import itertools
import re
import os
import json
import joblib
import pandas as pd
import numpy as np

# ====== ML Setup ======
_MODEL = None
_FEATURES = None

def load_ml_model():
    global _MODEL, _FEATURES
    if _MODEL is not None:
        return
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest")
    model_path = os.path.join(base_dir, "v8_lgb_model.pkl")
    features_path = os.path.join(base_dir, "v8_features.json")
    
    if not os.path.exists(model_path) or not os.path.exists(features_path):
        raise FileNotFoundError(f"モデルファイルが見つかりません: {model_path}")
        
    _MODEL = joblib.load(model_path)
    with open(features_path, "r", encoding="utf-8") as f:
        _FEATURES = json.load(f)

# ====== Constants ======
EV_THRESHOLD = 1.50
TRIFECTA_EV_THRESHOLD = 2.0
TRIFECTA_MIN_PROB_EXACTA = 0.005  # Lowered from 0.03 to allow 200x+ longshots (大荒れ)
TRIFECTA_MIN_PROB_COMBO = 0.015   # Lowered from 0.08 to capture 3連複 upsets
CONCENTRATION_THRESHOLD = 0.60
MAX_BETS_PER_RACE = 3

def extract_float(text):
    if pd.isna(text) or text == "" or text is None: return 0.0
    m = re.search(r'-?[\d\.]+', str(text))
    return float(m.group()) if m else 0.0

def parse_st(st_str):
    st_str = str(st_str).strip()
    try:
        if "F" in st_str.upper():
            raw = re.sub(r'[Ff]', '', st_str).strip()
            if not raw or raw == '.': return 0.0
            return -float('0' + raw) if raw.startswith('.') else -float(raw)
        elif "L" in st_str.upper():
            return 0.25
        elif st_str.startswith("."):
            return float("0" + st_str)
        else:
            return float(st_str)
    except ValueError:
        return 0.10

def build_ml_features(race_data):
    """1レース分のJSONデータから、モデル入力用のDataFrame(6行)を作成する"""
    predata = race_data if "racelist" in race_data else race_data.get("predata", {})
    env = predata.get("environment", {})
    racelist = predata.get("racelist", {})
    odds_data = predata.get("odds", {})
    
    # 1. Environment
    wind_speed = env.get("wind_speed", 0.0)
    wave_height = env.get("wave_height", 0.0)
    wind_dir_raw = env.get("wind_direction", "無風")
    if "追い" in wind_dir_raw: wind_dir = 1
    elif "向かい" in wind_dir_raw: wind_dir = -1
    else: wind_dir = 0
    
    # 2. TMP
    base_probs = {}
    for b_no in range(1, 7):
        win_odd = odds_data.get("単勝", {}).get(str(b_no))
        if win_odd and extract_float(win_odd) > 0:
            base_probs[b_no] = 1.0 / extract_float(win_odd)
        else:
            base_probs[b_no] = 1.0 / 6.0
    total_prob = sum(base_probs.values())
    tmp_win = {k: v / total_prob for k, v in base_probs.items()}
    
    # 3. Boat Base Features
    records = []
    exh_times = []
    win_rates = []
    motor_2rens = []
    sts = []
    
    for b_no in range(1, 7):
        b = racelist.get(str(b_no), {})
        exh = b.get("exhibition_time", 0.0)
        wr = b.get("win_rate_national", 0.0)
        mot = b.get("motor_2ren", 0.0)
        st = parse_st(b.get("start_exhibition_st", "0.15"))
        
        if exh > 0: exh_times.append(exh)
        if wr > 0: win_rates.append(wr)
        if mot > 0: motor_2rens.append(mot)
        sts.append(st)
        
        rank_str = b.get("class", "B1")
        if "A1" in rank_str: class_val = 4
        elif "A2" in rank_str: class_val = 3
        elif "B1" in rank_str: class_val = 2
        elif "B2" in rank_str: class_val = 1
        else: class_val = 2
        
        records.append({
            "boat": b_no,
            "course": b.get("start_course", b_no),
            "class": class_val,
            "win_rate": wr,
            "motor": mot,
            "exh_time": exh,
            "exh_st": st,
            "tilt": b.get("tilt", 0.0)
        })
        
    # Calculate race means/stds across all 6 boats for normalization
    mean_exh = np.mean(exh_times) if exh_times else 0
    std_exh = np.std(exh_times) if len(exh_times)>1 else 1
    mean_wr = np.mean(win_rates) if win_rates else 0
    mean_mot = np.mean(motor_2rens) if motor_2rens else 0
    mean_st = np.mean(sts) if sts else 0
    
    features = []
    for r in records:
        b_no = r["boat"]
        feat = {
            "boat": r["boat"],
            "wind_speed": wind_speed,
            "wave_height": wave_height,
            "wind_dir": wind_dir,
            "tmp_win_prob": tmp_win.get(b_no, 0.166),
            "course": r["course"],
            "class": r["class"],
            "win_rate": r["win_rate"],
            "motor": r["motor"],
            "exh_time": r["exh_time"],
            "exh_st": r["exh_st"],
            "tilt": r["tilt"],
            "exh_time_z": (r["exh_time"] - mean_exh) / (std_exh + 0.001) if r["exh_time"] > 0 else 0,
            "win_rate_diff": r["win_rate"] - mean_wr,
            "motor_diff": r["motor"] - mean_mot,
            "st_diff": r["exh_st"] - mean_st,
        }
        features.append(feat)
        
    df = pd.DataFrame(features)
    
    # Needs to match create_ml_dataset logic exactly!
    # Because there's only 1 race here, we don't need groupby
    df["tmp_rank"] = df["tmp_win_prob"].rank(ascending=False, method="min")
    df["exh_time_rank"] = df["exh_time"].replace(0, np.nan).rank(ascending=True, method="min").fillna(6)
    df["win_rate_rank"] = df["win_rate"].rank(ascending=False, method="min")
    df["motor_rank"] = df["motor"].rank(ascending=False, method="min")
    df["st_rank"] = df["exh_st"].rank(ascending=True, method="min")
    
    df = df.fillna(0)
    
    return df, tmp_win

def analyze(race_data, bankroll=1000):
    try:
        load_ml_model()
    except Exception as e:
        return {"error": f"ML準備エラー: {str(e)}"}
        
    predata = race_data if "racelist" in race_data else race_data.get("predata", {})
    if not predata.get("racelist"):
        return {"error": "宇宙からの事前情報がまだ届いていません (No Data)"}
        
    # --- ML Inference ---
    df_feat, tmp_dict = build_ml_features(race_data)
    
    # 欠損した列の補填（学習時と合わせる）
    for c in _FEATURES:
        if c not in df_feat.columns:
            df_feat[c] = 0
            
    # 学習時と同じ順序に並び替え
    X = df_feat[_FEATURES].copy()
    
    # Categorical variables
    cat_features = ["boat", "course", "class"]
    for c in cat_features:
        if c in X.columns:
            X[c] = X[c].astype("category")

    # Predict
    # classes are usually sorted [0, 1, 2, 3] by LightGBM
    preds = _MODEL.predict(X)
    
    # 1st-place probability is index 1 (because target was 1 for 1st)
    post_probs = {}
    for i in range(6):
        b_no = i + 1
        post_probs[b_no] = float(preds[i, 1])  # probability of being 1st
        
    # Normalize
    total_post = sum(post_probs.values())
    if total_post == 0: total_post = 1
    prob_dict = {k: v / total_post for k, v in post_probs.items()}
    
    # For UI display
    boats = []
    racelist = predata.get("racelist", {})
    for k in range(1, 7):
        b = racelist.get(str(k), {})
        boats.append({
            "boat": k,
            "name": b.get("name", "").strip(),
            "tmp": tmp_dict.get(k, 0),
            "alpha": prob_dict[k] / tmp_dict.get(k, 0.001) if tmp_dict.get(k, 0) > 0 else 1.0, # Visual ratio
            "post_prob": prob_dict[k],
            "reasons": [f"AI 1着確率: {post_probs[k]*100:.1f}%"]
        })

    # === Harville (120 permutations) ===
    harville_probs = {}
    for perm in itertools.permutations([1, 2, 3, 4, 5, 6], 3):
        first, second, third = perm
        p1 = prob_dict[first]
        if p1 <= 0: continue
        p2 = prob_dict[second] / (1 - prob_dict[first] + 1e-9)
        if p2 <= 0: continue
        p3 = prob_dict[third] / (1 - prob_dict[first] - prob_dict[second] + 1e-9)
        harville_probs[perm] = p1 * p2 * p3

    # === Expected Value (EV) & Kelly ===
    odds_data = predata.get("odds", {})
    targets = []
    
    def add_bet(b_type, combo_str, success_prob, ticket_type="連単"):
        if success_prob <= 0: return
        odds_dict = odds_data.get(b_type, {})
        odds_val = extract_float(odds_dict.get(combo_str, 0))
        if odds_val <= 0: return
        
        # 売上が小さく直前にオッズが暴落しやすい券種へのスリッページ補正（ペナルティ）
        slippage_multiplier = 1.0
        if "複勝" in b_type or "単勝" in b_type:
            slippage_multiplier = 0.5  # 半額に落ちると想定して厳しめに計算する
            
        adjusted_odds = max(1.0, odds_val * slippage_multiplier)
        
        ev = success_prob * adjusted_odds
        
        # --- Fractional Kelly / Risk Control ---
        # 「フルケリー」は資金の増減が乱高下し、全損（破産）の確率が極めて高くなるため
        # 極端に保守的な「1/8ケリー」を採用。これにより、一度に賭ける額を小さく抑え
        # ユーザーの資金（4万円）をドローダウンから絶対に死守する。
        KELLY_FRACTION = 0.125
        kelly = max(0, (ev - 1) / (adjusted_odds - 1)) * KELLY_FRACTION if adjusted_odds > 1 else 0
        
        thres = TRIFECTA_EV_THRESHOLD if "3連" in b_type else EV_THRESHOLD
        if "3連単" in b_type and success_prob < TRIFECTA_MIN_PROB_EXACTA: return
        if "3連複" in b_type and success_prob < TRIFECTA_MIN_PROB_COMBO: return
            
        if ev >= thres and kelly > 0.0001:
            rec_yen = max(100, math.floor((bankroll * kelly) / 100) * 100)
            if rec_yen > 0:
                targets.append({
                    "type": b_type,
                    "combo": combo_str,
                    "prob": success_prob,
                    "odds": odds_val,
                    "ev": ev,
                    "kelly_pct": kelly,
                    "recommended_yen": rec_yen
                })

    # Enum combinations
    for f in range(1, 7):
        for s in range(1, 7):
            if f == s: continue
            # 2連単
            prob_exacta = sum(v for k, v in harville_probs.items() if k[0] == f and k[1] == s)
            add_bet("2連単", f"{f}-{s}", prob_exacta)
            # 3連単
            for t in range(1, 7):
                if t in (f, s): continue
                prob_trifecta = harville_probs[(f, s, t)]
                add_bet("3連単", f"{f}-{s}-{t}", prob_trifecta)
    
    # 2連複
    for f, s in itertools.combinations([1, 2, 3, 4, 5, 6], 2):
        prob_quinella = sum(v for k, v in harville_probs.items() if set(k[:2]) == {f, s})
        add_bet("2連複", f"{f}={s}", prob_quinella, "連複")
    
    # 3連複
    for comb in itertools.combinations([1, 2, 3, 4, 5, 6], 3):
        prob_trio = sum(v for k, v in harville_probs.items() if set(k) == set(comb))
        comb_str = "=".join(map(str, sorted(comb)))
        add_bet("3連複", comb_str, prob_trio, "連複")
        
    # 拡連複 (Wide)
    for f, s in itertools.combinations([1, 2, 3, 4, 5, 6], 2):
        prob_wide = 0
        for k, v in harville_probs.items():
            if f in k and s in k: prob_wide += v
        add_bet("拡連複", f"{f}={s}", prob_wide, "連複")

    # 複勝 (Show) 1~2着に入る確率
    for b in range(1, 7):
        prob_show = 0
        for k, v in harville_probs.items():
            if b in k[:2]: prob_show += v
        add_bet("複勝", str(b), prob_show, "単勝/複勝")

    # 単勝 (Win) 1着になる確率
    for b in range(1, 7):
        prob_win = prob_dict[b]  # すでに計算済みの1着確率をそのまま使う
        add_bet("単勝", str(b), prob_win, "単勝/複勝")

    # Sort & Limit
    targets.sort(key=lambda x: x["ev"], reverse=True)
    targets = targets[:MAX_BETS_PER_RACE]

    return {
        "boats": boats,
        "targets": targets,
        "is_concentrated": any(t["kelly_pct"] >= CONCENTRATION_THRESHOLD for t in targets)
    }

if __name__ == "__main__":
    pass
