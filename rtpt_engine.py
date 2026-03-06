"""
RTPT v6.3 — True Market Alpha + Kelly Criterion Engine (Module)
Reusable analysis function for both CLI and Streamlit integration.
Alpha source: Wall Decay (Slit Void Exploitation) ONLY.
All public-information alphas (exhibition time, wind) removed as double-counting.
"""
import math
import itertools
import re

EV_THRESHOLD = 1.50
TRIFECTA_EV_THRESHOLD = 2.0     # 3連単/3連複の最低EV（より厳しい）
TRIFECTA_MIN_PROB_EXACTA = 0.03  # 3連単：最低確率3%（低確率の長打ちを除外）
TRIFECTA_MIN_PROB_COMBO = 0.08   # 3連複：最低確率8%（上位3着の確度が高い時のみ）
CONCENTRATION_THRESHOLD = 0.60  # Kelly%がこれを超えたら1点集中推奨

def analyze(race_data, bankroll=1000):
    """
    Analyze race data and return structured investment recommendations.
    Returns a dict with:
      - "boats": list of per-boat analysis dicts
      - "targets": list of investment target dicts (sorted by EV desc)
      - "summary": summary stats dict (includes concentration_mode flag)
      - "error": error string if any
    """
    racelist = race_data.get("racelist", {})
    env = race_data.get("environment", {})
    odds_data = race_data.get("odds", {})

    # --- Validation ---
    # NOTE: exhibition_time must be > 0, not just truthy (0.0 is falsy in Python)
    for k, b in racelist.items():
        if b.get("exhibition_time", 0) <= 0 or not b.get("start_exhibition_st"):
            return {"error": "展示データ欠損のため解析不可", "boats": [], "targets": [], "summary": {}}

    # Parse Exhibition Start Timing
    # ".08" → "0.08", "F.01" → -0.01, "L" → 0.25
    # JSONにparsed_stが既に存在する場合は数値として読み直す（型を保証）
    for k, b in racelist.items():
        if "parsed_st" in b:
            b["parsed_st"] = float(b["parsed_st"])
            continue
        st_str = str(b.get("start_exhibition_st", "0.10")).strip()
        try:
            if "F" in st_str.upper():
                raw = re.sub(r'[Ff]', '', st_str).strip()
                if not raw or raw == '.':
                    b["parsed_st"] = 0.0
                elif raw.startswith('.'):
                    b["parsed_st"] = -float('0' + raw)
                else:
                    b["parsed_st"] = -float(raw)
            elif "L" in st_str.upper():
                b["parsed_st"] = 0.25
            elif st_str.startswith("."):
                b["parsed_st"] = float("0" + st_str)
            else:
                b["parsed_st"] = float(st_str)
        except ValueError:
            b["parsed_st"] = 0.10  # フォールバック

    # === Step 1: True Market Probability (TMP) ===
    raw_tmp = {}
    win_odds = odds_data.get("単勝", {})
    if not win_odds:
        return {"error": "単勝オッズが存在しないため解析不可", "boats": [], "targets": [], "summary": {}}
    
    for k_str, odds_val in win_odds.items():
        val = float(odds_val) if odds_val else 100.0
        raw_tmp[int(k_str)] = 1.0 / max(val, 1.0)

    total_raw_tmp = sum(raw_tmp.values())
    tmp_dict = {k: v / total_raw_tmp for k, v in raw_tmp.items()}

    # === Step 2: Physics Alpha ===
    alpha_dict = {i: 1.0 for i in range(1, 7)}
    alpha_reasons = {i: [] for i in range(1, 7)}

    # 2-A: Exhibition Time Alpha — 削除 (v6.2)
    # 理由: 展示タイムは市場参加者全員が見ている公開情報である。
    # TMP（単勝オッズ）にはすでに展示タイムの評価が完全に織り込まれているため、
    # ここで再度アルファとして加算すると「二重取り」となり、
    # 我々の唯一の真のエッジ（Wall Decay）を汚染する。
    # 真のクオンツは「市場が見ていない情報だけ」をアルファにする。

    # 2-B: Wind Direction Alpha — 削除 (v6.3)
    # 理由: 風向き・風速はboatrace.jpに公開されており全市場参加者が見ている。
    # TMP（単勝オッズ）にすでに完全に織り込まれているため二重取りになる。

    # 2-C: Wall Decay / Slit Void Exploitation（唯一の真のエッジ）
    wd_dict = {i: 12.0 for i in range(1, 7)}
    for i in range(1, 6):
        inner_st = racelist[str(i)].get("parsed_st", 0.1)
        outer_st = racelist[str(i+1)].get("parsed_st", 0.1)
        delta_st = inner_st - outer_st

        if delta_st >= 0.08:
            wd_dict[i] = 0.0
            alpha_dict[i+1] *= 1.5
            alpha_dict[i] *= 0.5
            alpha_reasons[i+1].append(f"VoidExploit(ΔST={delta_st:.2f}→α×1.5)")
            alpha_reasons[i].append(f"WallDecay(ΔST={delta_st:.2f}→α×0.5)")
        elif delta_st >= 0.04:
            wd_dict[i] *= 0.5
            alpha_dict[i+1] *= 1.2
            alpha_dict[i] *= 0.8
            alpha_reasons[i+1].append(f"VoidExploit(ΔST={delta_st:.2f}→α×1.2)")
            alpha_reasons[i].append(f"WallHalf(ΔST={delta_st:.2f}→α×0.8)")

    # === Step 3: Posterior Probability ===
    posterior_raw = {k: tmp_dict.get(k, 1/6) * max(0.1, alpha_dict[k]) for k in range(1, 7)}
    total_posterior = sum(posterior_raw.values())
    prob_dict = {k: v / total_posterior for k, v in posterior_raw.items()}

    boats = []
    for k in range(1, 7):
        boats.append({
            "boat": k,
            "name": racelist[str(k)].get("name", "").strip(),
            "tmp": tmp_dict.get(k, 0),
            "alpha": alpha_dict[k],
            "post_prob": prob_dict[k],
            "wd": wd_dict[k],
            "reasons": alpha_reasons[k]
        })

    # === Step 4: Harville (120 permutations) ===
    harville_probs = {}
    for perm in itertools.permutations([1, 2, 3, 4, 5, 6], 3):
        first, second, third = perm
        p1 = prob_dict[first]
        p2 = prob_dict[second] / (1.0 - p1)
        p3 = prob_dict[third] / (1.0 - p1 - prob_dict[second])
        harville_probs[perm] = p1 * p2 * p3

    # === Step 5: Extract ALL qualifying bets ===
    investment_targets = []

    if "2連単" in odds_data:
        for k, odds in odds_data["2連単"].items():
            first, second = map(int, k.split('-'))
            est_prob = sum(harville_probs[(first, second, t)] for t in range(1, 7) if t not in (first, second))
            ev = est_prob * float(odds)
            if ev >= EV_THRESHOLD:
                investment_targets.append({"type": "2連単", "combo": k, "prob": est_prob, "odds": float(odds), "ev": ev})

    if "2連複" in odds_data:
        for k, odds in odds_data["2連複"].items():
            first, second = map(int, k.split('='))
            est_prob = sum(harville_probs[p] for p in harville_probs if (p[0] == first and p[1] == second) or (p[0] == second and p[1] == first))
            ev = est_prob * float(odds)
            if ev >= EV_THRESHOLD:
                investment_targets.append({"type": "2連複", "combo": k, "prob": est_prob, "odds": float(odds), "ev": ev})

    if "拡連複" in odds_data:
        for k, odds_str in odds_data["拡連複"].items():
            first, second = map(int, k.split('='))
            est_prob = sum(harville_probs[p] for p in harville_probs if first in p and second in p)
            try:
                min_odds = float(str(odds_str).split('-')[0])
                ev = est_prob * min_odds
                if ev >= EV_THRESHOLD:
                    investment_targets.append({"type": "拡連複", "combo": k, "prob": est_prob, "odds": min_odds, "ev": ev})
            except:
                pass

    # 3連単: EV >= 2.0 かつ 推定確率 >= 3% の時のみ（自信がある買い目に限定）
    if "3連単" in odds_data:
        for k, odds in odds_data["3連単"].items():
            try:
                first, second, third = map(int, k.split('-'))
                est_prob = harville_probs.get((first, second, third), 0.0)
                ev = est_prob * float(odds)
                if ev >= TRIFECTA_EV_THRESHOLD and est_prob >= TRIFECTA_MIN_PROB_EXACTA:
                    investment_targets.append({"type": "3連単", "combo": k, "prob": est_prob, "odds": float(odds), "ev": ev})
            except (ValueError, KeyError):
                pass

    # 3連複: EV >= 1.8 かつ 推定確率 >= 8% の時のみ（上位3艇の確度が高い時のみ）
    if "3連複" in odds_data:
        for k, odds in odds_data["3連複"].items():
            try:
                boats_combo = list(map(int, k.split('=')))
                est_prob = sum(harville_probs[p] for p in itertools.permutations(boats_combo, 3))
                ev = est_prob * float(odds)
                if ev >= TRIFECTA_EV_THRESHOLD and est_prob >= TRIFECTA_MIN_PROB_COMBO:
                    investment_targets.append({"type": "3連複", "combo": k, "prob": est_prob, "odds": float(odds), "ev": ev})
            except (ValueError, KeyError):
                pass

    investment_targets.sort(key=lambda x: x["ev"], reverse=True)

    # === Step 6: Kelly Criterion ===
    kelly_fractions = []
    for t in investment_targets:
        b = t["odds"] - 1.0
        if b > 0:
            kelly_f = max(0.0, (t["prob"] * b - (1.0 - t["prob"])) / b)
        else:
            kelly_f = 0.0
        kelly_fractions.append(kelly_f)

    total_kelly = sum(kelly_fractions)
    
    # Kelly配分を算出し、合計がバンクロールを超えないよう正規化する
    raw_yen = []
    for i, t in enumerate(investment_targets):
        pct = (kelly_fractions[i] / total_kelly * 100) if total_kelly > 0 else 0
        t["kelly_pct"] = pct
        raw_yen.append(bankroll * pct / 100)
    
    # === Step 7: Concentration Mode Detection ===
    # 最上位買い目のKelly割合が全体の60%を超える場合は「1点集中推奨」とする
    # 数学的根拠: 複数の相関した買い目に分散するより、最大EVの1点に全額投下する方が
    # 期待ログ成長率が高くなる局面がある。
    concentration_mode = False
    if kelly_fractions and total_kelly > 0:
        top_kelly_ratio = kelly_fractions[0] / total_kelly
        if top_kelly_ratio >= CONCENTRATION_THRESHOLD:
            concentration_mode = True
            # 集中モード: 1位の買い目にバンクロール全額を推奨
            for i, t in enumerate(investment_targets):
                if i == 0:
                    t["recommended_yen"] = bankroll
                    t["concentration"] = True
                else:
                    t["recommended_yen"] = 0  # 購入しない
                    t["concentration"] = False
        else:
            # 分散モード: 100円単位に丸め、合計がバンクロールを超えないよう調整
            concentration_mode = False
            total_raw = sum(raw_yen)
            for i, t in enumerate(investment_targets):
                if total_raw > 0:
                    normalized = raw_yen[i] / total_raw * bankroll
                else:
                    normalized = 0
                t["recommended_yen"] = max(100, round(normalized / 100) * 100)
                t["concentration"] = False
    else:
        for i, t in enumerate(investment_targets):
            t["recommended_yen"] = 100
            t["concentration"] = False

    summary = {}
    if investment_targets:
        summary = {
            "count": len(investment_targets),
            "avg_ev": sum(t["ev"] for t in investment_targets) / len(investment_targets),
            "max_ev": investment_targets[0]["ev"],
            "max_ev_combo": investment_targets[0]["combo"],
            "verdict": "投資実行",
            "concentration_mode": concentration_mode,
            "top_kelly_ratio": (kelly_fractions[0] / total_kelly) if (kelly_fractions and total_kelly > 0) else 0
        }
    else:
        summary = {"count": 0, "avg_ev": 0, "max_ev": 0, "max_ev_combo": "", "verdict": "見（ケン）", "concentration_mode": False, "top_kelly_ratio": 0}

    return {"error": None, "boats": boats, "targets": investment_targets, "summary": summary}
