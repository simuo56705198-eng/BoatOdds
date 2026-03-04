"""
RTPT v6.1 — True Market Alpha + Kelly Criterion Engine (Module)
Reusable analysis function for both CLI and Streamlit integration.
"""
import math
import itertools

EV_THRESHOLD = 1.50

def analyze(race_data, bankroll=1000):
    """
    Analyze race data and return structured investment recommendations.
    Returns a dict with:
      - "boats": list of per-boat analysis dicts
      - "targets": list of investment target dicts (sorted by EV desc)
      - "summary": summary stats dict
      - "error": error string if any
    """
    racelist = race_data.get("racelist", {})
    env = race_data.get("environment", {})
    odds_data = race_data.get("odds", {})

    # --- Validation ---
    for k, b in racelist.items():
        if not b.get("exhibition_time") or not b.get("start_exhibition_st"):
            return {"error": "展示データ欠損のため解析不可", "boats": [], "targets": [], "summary": {}}

    # Parse Exhibition Start Timing
    for k, b in racelist.items():
        st_str = str(b["start_exhibition_st"])
        if "F" in st_str:
            f_val = float(st_str.replace("F.", "0."))
            b["parsed_st"] = -f_val
        elif "L" in st_str:
            b["parsed_st"] = 0.25
        else:
            b["parsed_st"] = float(st_str.replace(".", "0."))

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
    avg_exh_time = sum(b.get("exhibition_time", 6.8) for b in racelist.values()) / 6
    alpha_dict = {i: 1.0 for i in range(1, 7)}
    alpha_reasons = {i: [] for i in range(1, 7)}

    # 2-A: Exhibition Time Deviation Alpha
    for k, b in racelist.items():
        boat_id = int(k)
        time_diff = avg_exh_time - b.get("exhibition_time", avg_exh_time)
        adj = time_diff * 3.0
        if abs(adj) > 0.01:
            alpha_dict[boat_id] += adj
            alpha_reasons[boat_id].append(f"ExhTime({time_diff*1000:+.0f}ms→α{adj:+.3f})")

    # 2-B: Wind Direction Alpha
    wind_dir = env.get("wind_direction", "")
    wind_speed = env.get("wind_speed", 0)
    if wind_speed >= 2.0:
        if "追い風" in str(wind_dir):
            for i in [4, 5, 6]:
                alpha_dict[i] *= 1.1
                alpha_reasons[i].append(f"追い風{wind_speed}m(α×1.1)")
            for i in [1, 2]:
                alpha_dict[i] *= 0.95
                alpha_reasons[i].append(f"追い風{wind_speed}m(α×0.95)")
        elif "向かい風" in str(wind_dir):
            for i in [1, 2]:
                alpha_dict[i] *= 1.05
                alpha_reasons[i].append(f"向かい風{wind_speed}m(α×1.05)")

    # 2-C: Wall Decay / Slit Void Exploitation
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
    for i, t in enumerate(investment_targets):
        pct = (kelly_fractions[i] / total_kelly * 100) if total_kelly > 0 else 0
        t["kelly_pct"] = pct
        t["recommended_yen"] = max(100, round(bankroll * pct / 100 / 100) * 100)

    summary = {}
    if investment_targets:
        summary = {
            "count": len(investment_targets),
            "avg_ev": sum(t["ev"] for t in investment_targets) / len(investment_targets),
            "max_ev": investment_targets[0]["ev"],
            "max_ev_combo": investment_targets[0]["combo"],
            "verdict": "投資実行"
        }
    else:
        summary = {"count": 0, "avg_ev": 0, "max_ev": 0, "max_ev_combo": "", "verdict": "見（ケン）"}

    return {"error": None, "boats": boats, "targets": investment_targets, "summary": summary}
