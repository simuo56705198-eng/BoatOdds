"""
RTPT v7.5 — All Bugs Resolved + Statistical Improvements
==========================================================
v7.4からの修正:
  Bug#11: Kelly正規化を廃止。kelly_quarterをそのままbankroll比率として使用
  Bug#13: 風向きマッピングを場ごとに定義
  Bug#15: HHI計算で複式の二重カウントを修正
  Issue#11: Shrinkageを市場ソース数に応じて動的化
  Issue#12: α適用を加法ベースに変更し順序依存性を排除
  Issue#13: 3連複インデックスを追加
  Issue#14: 級別補正を全場で発火（荒れ場では増幅）
"""
import math
import itertools
import re
import json
import os
from datetime import datetime
from collections import Counter

DEFAULT_PARAMS = {
    "wall_decay_strong": 1.30, "wall_decay_weak": 1.12,
    "wall_penalty_strong": 0.65, "wall_penalty_weak": 0.88,
    "ex_time_coeff": 0.06, "ex_time_min_z": 1.0,
    "motor_coeff": 0.12, "motor_min_deviation": 0.15,
    "weight_coeff": 0.008, "weight_min_diff": 3.0,
    "henery_gamma": 0.91,
    "shrinkage_base": 0.20,        # [Issue#11] ソース1つの場合のShrinkage
    "shrinkage_per_source": 0.05,   # [Issue#11] ソース1つ追加ごとに減らす量
    "shrinkage_min": 0.05,          # [Issue#11] 最低Shrinkage
    "course_bias_coeff": 0.20,
    "class_b_boost": 0.04,          # [Issue#14] B級補正（全場で発火）
    "class_a1_penalty": 0.03,       # [Issue#14] A1本命ペナルティ（全場で発火）
    "volatile_amplify": 0.30,       # 荒れ場でのα増幅率
    "tide_alpha_coeff": 0.10,
    "alpha_soft_cap": 0.55,
    "selection_bias_mult": 0.08,
    "selection_bias_sqrt": 0.015,
    "selection_bias_base": 50,
    "kelly_fraction": 0.15, "max_bet_ratio": 0.20, "max_total_bet_ratio": 0.80,
    "max_targets": 3,
    "ev_threshold_2ren": 1.50, "ev_threshold_3ren": 1.80, "ev_threshold_wide": 1.50,
    "trifecta_min_prob_exacta": 0.015, "trifecta_min_prob_combo": 0.05,
    "max_odds": 50.0, "max_ev": 5.0,
}
PARAMS_FILE = "alpha_params.json"
MIN_BET_YEN = 100

def load_params():
    if os.path.exists(PARAMS_FILE):
        try:
            with open(PARAMS_FILE, 'r') as f:
                return {**DEFAULT_PARAMS, **json.load(f)}
        except (json.JSONDecodeError, IOError): pass
    return dict(DEFAULT_PARAMS)

VENUE_COURSE_BIAS = {
    "桐生":[.52,.15,.13,.11,.06,.03],"戸田":[.43,.16,.14,.13,.08,.06],
    "江戸川":[.43,.15,.13,.13,.09,.07],"平和島":[.45,.16,.13,.12,.08,.06],
    "多摩川":[.53,.15,.12,.11,.06,.03],"浜名湖":[.52,.15,.13,.11,.06,.03],
    "蒲郡":[.53,.15,.12,.11,.06,.03],"常滑":[.52,.15,.13,.11,.06,.03],
    "津":[.52,.15,.13,.11,.06,.03],"三国":[.55,.14,.12,.10,.06,.03],
    "びわこ":[.47,.15,.13,.12,.07,.06],"住之江":[.55,.14,.12,.10,.06,.03],
    "尼崎":[.53,.15,.12,.11,.06,.03],"鳴門":[.47,.16,.14,.12,.07,.04],
    "丸亀":[.50,.15,.13,.11,.07,.04],"児島":[.50,.15,.13,.11,.07,.04],
    "宮島":[.50,.15,.13,.11,.07,.04],"徳山":[.56,.14,.12,.10,.05,.03],
    "下関":[.55,.14,.12,.10,.06,.03],"若松":[.54,.14,.12,.11,.06,.03],
    "芦屋":[.53,.15,.12,.11,.06,.03],"福岡":[.46,.15,.14,.12,.08,.05],
    "唐津":[.54,.15,.12,.10,.06,.03],"大村":[.58,.14,.11,.09,.05,.03],
}
VENUE_VOLATILITY = {
    "桐生":.7,"戸田":.9,"江戸川":1.,"平和島":.9,"多摩川":.3,
    "浜名湖":.3,"蒲郡":.3,"常滑":.3,"津":.5,"三国":.4,
    "びわこ":.9,"住之江":.4,"尼崎":.4,"鳴門":.9,"丸亀":.7,
    "児島":.7,"宮島":.7,"徳山":.3,"下関":.3,"若松":.3,
    "芦屋":.4,"福岡":.9,"唐津":.3,"大村":.3,
}
MOTOR_EXCHANGE_MONTH = {
    "桐生":12,"戸田":7,"江戸川":8,"平和島":6,"多摩川":8,
    "浜名湖":9,"蒲郡":5,"常滑":12,"津":9,"三国":4,
    "びわこ":6,"住之江":3,"尼崎":4,"鳴門":4,"丸亀":11,
    "児島":1,"宮島":11,"徳山":5,"下関":3,"若松":12,
    "芦屋":6,"福岡":2,"唐津":8,"大村":3,
}
VENUE_COMPOUND_RULES = {
    ("平和島","追い風",4,"any"):{6:1.35,5:1.12},
    ("常滑","向かい風",4,"any"):{4:1.25,2:.88},
    ("津","向かい風",5,"any"):{1:.82,2:1.18,3:1.15},
    ("宮島","追い風",3,"high"):{1:1.25},
    ("宮島","追い風",3,"any"):{1:1.12},
    ("江戸川","向かい風",3,"ebb"):{1:.55,4:1.30,5:1.25,6:1.15},
    ("江戸川","向かい風",3,"any"):{1:.70,4:1.18,5:1.12},
    ("福岡","追い風",3,"any"):{1:.85,2:1.20},
    ("鳴門","追い風",4,"ebb"):{1:.70,2:1.30,4:1.25},
    ("鳴門","追い風",4,"any"):{1:.80,2:1.20,4:1.12},
    ("丸亀","追い風",3,"low"):{3:1.20,4:1.18},
    ("丸亀","向かい風",3,"low"):{3:1.15,4:1.20},
    ("児島","追い風",0,"ebb"):{4:1.15,5:1.12},
    ("びわこ","向かい風",3,"any"):{1:.80,3:1.15,4:1.18},
}
TIDAL_VENUES = {"江戸川","鳴門","丸亀","児島","宮島","福岡","下関","若松","唐津"}

# [Bug#13] 場ごとのコース方位角（北を0度として時計回り。ホームストレッチの進行方向）
# boatrace.jpの場レイアウトから概算
VENUE_COURSE_HEADING = {
    "桐生": 0, "戸田": 350, "江戸川": 20, "平和島": 10, "多摩川": 0,
    "浜名湖": 340, "蒲郡": 350, "常滑": 330, "津": 340, "三国": 320,
    "びわこ": 280, "住之江": 270, "尼崎": 280, "鳴門": 350, "丸亀": 0,
    "児島": 10, "宮島": 330, "徳山": 300, "下関": 280, "若松": 300,
    "芦屋": 310, "福岡": 320, "唐津": 280, "大村": 290,
}

# === Utility ===
def _parse_exhibition_st(b):
    if "parsed_st" in b:
        b["parsed_st"] = float(b["parsed_st"]); return
    s = str(b.get("start_exhibition_st", "0.10")).strip()
    try:
        if "F" in s.upper():
            r = re.sub(r'[Ff]', '', s).strip()
            if not r or r == '.': b["parsed_st"] = -0.05  # フライング（数値なし）はペナルティ
            elif r.startswith('.'): b["parsed_st"] = -float('0' + r)
            else: b["parsed_st"] = -float(r)
        elif "L" in s.upper(): b["parsed_st"] = 0.25
        elif s.startswith("."): b["parsed_st"] = float("0" + s)
        else: b["parsed_st"] = float(s)
    except ValueError: b["parsed_st"] = 0.10

def _months_since_exchange(venue, ds):
    em = MOTOR_EXCHANGE_MONTH.get(venue)
    if not em: return 12
    try: rd = datetime.strptime(ds, "%Y%m%d")
    except (ValueError, TypeError): return 12
    d = rd.month - em; return d if d >= 0 else d + 12

def _clamp(v, lo, hi): return max(lo, min(hi, v))

def _soft_cap_alpha(a, cap):
    if cap <= 0: return a
    return 1.0 + cap * math.tanh((a - 1.0) / cap)

def _henery_prob(probs, gamma):
    adj = {k: p ** gamma for k, p in probs.items()}
    t = sum(adj.values())
    return {k: v / t for k, v in adj.items()}

def _harville(pd):
    probs = {}
    for perm in itertools.permutations(range(1, 7), 3):
        f, s, t = perm
        p1 = pd[f]
        r1 = {k: v for k, v in pd.items() if k != f}; t1 = sum(r1.values())
        p2 = (r1[s] / t1) if t1 > 0 else 0
        r2 = {k: v for k, v in r1.items() if k != s}; t2 = sum(r2.values())
        p3 = (r2[t] / t2) if t2 > 0 else 0
        probs[perm] = p1 * p2 * p3
    return probs

def _cond_dep_adjust(harv, pd, venue):
    cb = VENUE_COURSE_BIAS.get(venue)
    if not cb: return harv
    iw = cb[0]; adj = {}
    for perm, p in harv.items():
        f, s, _ = perm; m = 1.0
        if f == 1 and iw > .50: m *= 1.06 if s <= 3 else (.94 if s >= 5 else 1.)
        elif f != 1 and iw > .50 and pd.get(f, 0) < .15: m *= 1.04
        adj[perm] = p * m
    tt = sum(adj.values())
    return {k: v / tt for k, v in adj.items()}

# [Bug#24修正] 場ごとの風向き分類を実装
def _classify_wind(venue, raw_wind_dir, wind_speed, wind_code=None):
    """
    場のコース方位を考慮した風向き分類。
    wind_code: boatrace.jpの16方位コード（1=北, 5=東, 9=南, 13=西）
    コードがあれば場の向きに応じて正しく分類。なければ従来の分類をそのまま使用。
    """
    if wind_speed == 0: return "無風"

    if wind_code is not None and venue in VENUE_COURSE_HEADING:
        # 風の「吹いてくる方向」を角度に変換（1=0°=北から）
        wind_from_deg = (wind_code - 1) * 22.5

        # コースの進行方向
        course_heading = VENUE_COURSE_HEADING[venue]

        # 相対角度: 風の吹いてくる方向とコース進行方向の差
        # 0° = 正面から（向かい風）、180° = 背後から（追い風）
        relative = (wind_from_deg - course_heading) % 360

        if 135 <= relative <= 225:
            return "追い風"    # 背後から
        elif relative <= 45 or relative >= 315:
            return "向かい風"  # 正面から
        else:
            return "横風"      # 側面から

    # コードがない場合は従来の分類をそのまま返す
    return raw_wind_dir if raw_wind_dir else "無風"

def _multi_market_tmp(od):
    """Multi-Market TMP + ソース数を返す"""
    est = {i: [] for i in range(1, 7)}; wt = {i: [] for i in range(1, 7)}
    n_sources = 0

    wo = od.get("単勝", {})
    if wo:
        raw = {}
        for k, v in wo.items():
            try:
                bk = int(k)
                if 1 <= bk <= 6:
                    raw[bk] = 1. / max(float(v) if v else 100., 1.)
            except (ValueError, TypeError):
                pass
        tt = sum(raw.values())
        if tt > 0:
            for k, v in raw.items(): est[k].append(v / tt); wt[k].append(1.0)
            n_sources += 1

    po = od.get("複勝", {})
    if po:
        raw = {}
        for k, vs in po.items():
            try: raw[int(k)] = 1. / max(float(str(vs).split('-')[0]), 1.)
            except (ValueError, IndexError): pass
        tt = sum(raw.values())
        if tt > 0 and len(raw) >= 4:
            for k, v in raw.items(): est[k].append(v / tt); wt[k].append(0.5)
            n_sources += 1

    nf = od.get("2連複", {})
    if nf and len(nf) >= 10:
        st = {i: 0. for i in range(1, 7)}
        for k, odds in nf.items():
            p = k.split('=')
            if len(p) != 2: continue
            try:
                a, b = int(p[0]), int(p[1]); inv = 1. / max(float(odds), 1.)
                st[a] += inv; st[b] += inv
            except (ValueError, IndexError): pass
        tt = sum(st.values())
        if tt > 0:
            for k, v in st.items(): est[k].append(v / tt); wt[k].append(0.8)
            n_sources += 1

    res = {}
    for bn in range(1, 7):
        if est[bn]:
            ws = sum(wt[bn])
            res[bn] = sum(e * w for e, w in zip(est[bn], wt[bn])) / ws
        else: res[bn] = 1. / 6.
    tt = sum(res.values())
    return {k: v / tt for k, v in res.items()}, max(n_sources, 1)

def _adj_ev_th(base, nc, P):
    if nc <= P["selection_bias_base"]: return base
    ratio = nc / P["selection_bias_base"]
    bonus = P["selection_bias_mult"] * math.log(ratio) + P.get("selection_bias_sqrt", .015) * math.sqrt(ratio)
    return base + bonus

def _infer_tide(env):
    if "tide" in env: return env["tide"]
    return "ebb" if env.get("wave_height", 0) >= 5 else "any"

def _build_prob_index(harv):
    """Harville→全券種インデックス（2連単/2連複/拡連複/3連複）"""
    exacta = {}; quinella = {}; wide = {}; trifecta_combo = {}
    for (f, s, t), p in harv.items():
        ek = (f, s); exacta[ek] = exacta.get(ek, 0) + p
        qk = frozenset({f, s}); quinella[qk] = quinella.get(qk, 0) + p
        for pair in [frozenset({f, s}), frozenset({f, t}), frozenset({s, t})]:
            wide[pair] = wide.get(pair, 0) + p
        # [Issue#13] 3連複インデックス
        tk = frozenset({f, s, t}); trifecta_combo[tk] = trifecta_combo.get(tk, 0) + p
    return exacta, quinella, wide, trifecta_combo

# [Bug#15修正] HHI計算（複式の重みを0.5に）
def _hhi_correlation_penalty(targets):
    if not targets: return 1.0
    # 単式と複式を区別した集中度計算
    counts = Counter()
    total_weight = 0
    for t in targets:
        if t["type"] in ("2連単", "3連単"):
            fb = t["combo"].split('-')[0]
            counts[fb] += 1.0
            total_weight += 1.0
        else:
            parts = re.split(r'[-=]', t["combo"])
            w = 1.0 / len(parts)  # 複式は1/N票ずつ
            for p in parts:
                counts[p] += w
                total_weight += w

    if total_weight == 0: return 1.0
    hhi = sum((c / total_weight) ** 2 for c in counts.values())
    return max(0.50, 1.0 - hhi * 0.50)


# === Main Analysis ===
def analyze(race_data, bankroll=1000, params_override=None, ml_model=None):
    P = load_params()
    if params_override: P.update(params_override)
    rl = race_data.get("racelist", {})
    env = race_data.get("environment", {})
    od = race_data.get("odds", {})
    meta = race_data.get("metadata", {})
    venue = meta.get("stadium", ""); rdate = meta.get("date", "")
    warns = []

    # 展示データ: 利用可能な艇数をカウント（4艇以上あればα計算を実行）
    ex_count = sum(
        1 for i in range(1, 7)
        if rl[str(i)].get("exhibition_time", 0) > 0
        and rl[str(i)].get("start_exhibition_st")
    )
    has_ex = ex_count >= 4  # 4艇以上あれば展示系αを有効化
    if has_ex:
        for b in rl.values(): _parse_exhibition_st(b)
        if ex_count < 6:
            warns.append(f"展示データ一部欠損({ex_count}/6艇): 欠損艇はデフォルト値で補完")
            # 欠損艇にデフォルト値を設定
            for i in range(1, 7):
                b = rl[str(i)]
                if not b.get("exhibition_time") or b.get("exhibition_time", 0) <= 0:
                    b["exhibition_time"] = 6.80
                if not b.get("start_exhibition_st"):
                    b["start_exhibition_st"] = "0.15"
                _parse_exhibition_st(b)
    else:
        warns.append(f"展示データ不足({ex_count}/6艇): 展示系αは無効化")

    wo = od.get("単勝", {})
    if not wo:
        return {"error": "単勝オッズなし", "boats": [], "targets": [], "summary": {}, "warnings": []}

    # [Issue#11] 動的Shrinkage (V7.5_fixed: removed high shrinkage to prevent artificial EV explosion on longshots)
    tmp, n_sources = _multi_market_tmp(od)
    shrinkage = max(0.00, P["shrinkage_base"] - (n_sources - 1) * P["shrinkage_per_source"])
    # Hard clamp shrinkage as 0.20 base was destroying all EV calculations
    shrinkage = min(shrinkage, 0.05) 
    u = 1. / 6.
    tmp = {k: (1 - shrinkage) * tmp[k] + shrinkage * u for k in tmp}
    tt = sum(tmp.values()); tmp = {k: v / tt for k, v in tmp.items()}

    # [Issue#12] α計算を加法ベースに変更
    # 旧: alpha *= multiplier （順序依存、指数爆発）
    # 新: alpha_additive += (multiplier - 1.0) → 最後に 1.0 + sum を乗数に変換
    # これで全αが対等に加算され、順序依存性が消える
    alpha_add = {i: 0.0 for i in range(1, 7)}
    rsn = {i: [] for i in range(1, 7)}
    wd = {i: 12. for i in range(1, 7)}

    # α-A: Wall Decay
    if has_ex:
        for i in range(1, 6):
            inn = rl[str(i)].get("parsed_st", .1); out = rl[str(i+1)].get("parsed_st", .1)
            d = inn - out
            if d >= .08:
                wd[i] = 0.
                alpha_add[i+1] += P["wall_decay_strong"] - 1.0
                alpha_add[i] += P["wall_penalty_strong"] - 1.0
                rsn[i+1].append(f"VoidExploit(ΔST={d:.2f}→+{P['wall_decay_strong']-1:.2f})")
                rsn[i].append(f"WallDecay(ΔST={d:.2f}→{P['wall_penalty_strong']-1:+.2f})")
            elif d >= .04:
                wd[i] *= .5
                alpha_add[i+1] += P["wall_decay_weak"] - 1.0
                alpha_add[i] += P["wall_penalty_weak"] - 1.0
                rsn[i+1].append(f"VoidExploit(ΔST={d:.2f}→+{P['wall_decay_weak']-1:.2f})")
                rsn[i].append(f"WallHalf(ΔST={d:.2f}→{P['wall_penalty_weak']-1:+.2f})")

    # α-B: ExTime × Tilt
    TB = -0.5
    if has_ex:
        aet = {}
        for k, b in rl.items():
            et = b.get("exhibition_time", 0); tl = b.get("tilt", TB)
            aet[int(k)] = (et + (tl - TB) * .02) if et > 0 else 6.80
        if len(aet) == 6:
            av = sum(aet.values()) / 6
            sd = max(.01, (sum((v - av) ** 2 for v in aet.values()) / 5) ** .5)  # 標本標準偏差(N-1=5)
            for bn, et in aet.items():
                z = (av - et) / sd
                if abs(z) >= P["ex_time_min_z"]:
                    delta = _clamp(z * P["ex_time_coeff"], -.20, .20)
                    alpha_add[bn] += delta
                    rsn[bn].append(f"ExT(z={z:+.2f}→{delta:+.3f})")

    # α-C: Motor
    mo = _months_since_exchange(venue, rdate); mt = min(1., mo / 3.)
    for bn in range(1, 7):
        m2 = rl[str(bn)].get("motor_2ren", 30.); dv = (m2 - 30.) / 30.
        if abs(dv) > P["motor_min_deviation"]:
            delta = _clamp(dv * P["motor_coeff"] * mt, -.12, .12)
            alpha_add[bn] += delta
            rsn[bn].append(f"Mot({m2:.0f}%→{delta:+.3f})")

    # α-D: ST Reversion
    if has_ex:
        for bn in range(1, 7):
            b = rl[str(bn)]; ast = b.get("avg_st", .15); est = b.get("parsed_st", .10)
            if ast > 0 and est > 0:
                g = ast - est
                if g > .04:
                    delta = _clamp(-g * 1.2, -.12, 0.)
                    alpha_add[bn] += delta
                    rsn[bn].append(f"STRev({g:.2f}→{delta:+.3f})")
                elif g < -.03:
                    delta = _clamp(g * .8, -.08, 0.)
                    alpha_add[bn] += delta
                    rsn[bn].append(f"STSlow({g:.2f}→{delta:+.3f})")

    # α-E: Weight
    wts = {int(k): b.get("weight", 0) for k, b in rl.items() if b.get("weight", 0) > 0}
    if len(wts) >= 4:
        aw = sum(wts.values()) / len(wts)
        for bn, w in wts.items():
            d = aw - w
            if abs(d) >= P["weight_min_diff"]:
                delta = _clamp(d * P["weight_coeff"], -.04, .04)
                alpha_add[bn] += delta
                rsn[bn].append(f"Wt({w:.0f}kg→{delta:+.3f})")

    # [Issue#14] α-F: 級別補正（全場で発火、荒れ場で増幅）
    vol = VENUE_VOLATILITY.get(venue, .5)
    vol_amp = 1.0 + vol * P["volatile_amplify"]  # 荒れ場で増幅
    for bn in range(1, 7):
        rc = rl[str(bn)].get("class", "")
        if rc in ("B1", "B2"):
            delta = P["class_b_boost"] * vol_amp
            alpha_add[bn] += delta
            rsn[bn].append(f"Class({rc}→{delta:+.3f})")
        elif rc == "A1" and tmp.get(bn, 0) > .30:
            delta = -P["class_a1_penalty"] * vol_amp
            alpha_add[bn] += delta
            rsn[bn].append(f"Class(A1fav→{delta:+.3f})")

    # α-G: Course Bias
    cb = VENUE_COURSE_BIAS.get(venue); c2b = {}
    for k, b in rl.items():
        sc = b.get("start_course")
        if sc: c2b[int(sc)] = int(k)
    if cb and len(c2b) == 6:
        ab = 1. / 6.
        for ci, bn in c2b.items():
            if 1 <= ci <= 6:
                r = cb[ci - 1] / ab
                if abs(r - 1.) > .15:
                    delta = _clamp((r - 1.) * P["course_bias_coeff"], -.15, .15)
                    alpha_add[bn] += delta
                    rsn[bn].append(f"CBias(C{ci}→{delta:+.3f})")

    # α-H: Wind × Tide
    wdir = _classify_wind(venue, env.get("wind_direction", "無風"),
                          env.get("wind_speed", 0), env.get("wind_direction_code"))
    wspd = env.get("wind_speed", 0)
    tide = _infer_tide(env)
    if venue in TIDAL_VENUES and tide == "any":
        warns.append(f"{venue}は潮汐の影響大。tide_data.pyで自動注入推奨")
    applied = None
    for (v, wd_rule, mw, tc), adjs in VENUE_COMPOUND_RULES.items():
        if venue == v and wdir == wd_rule and wspd >= mw:
            if tc == tide: applied = adjs; break
            elif tc == "any" and applied is None: applied = adjs
    if applied:
        for cno, mult in applied.items():
            tgt = c2b.get(cno, cno) if c2b else cno
            if 1 <= tgt <= 6:
                delta = mult - 1.0
                alpha_add[tgt] += delta
                rsn[tgt].append(f"Wind×Tide({venue}/{wdir}/{wspd}m/{tide}→C{cno}{delta:+.2f})")

    # [Issue#12] 加法αを乗法αに変換 + ソフトキャップ
    cap = P["alpha_soft_cap"]
    al = {}
    for bn in range(1, 7):
        # 荒れ場増幅: 全αの合計乖離を増幅
        raw_deviation = alpha_add[bn]
        if vol >= 0.7 and abs(raw_deviation) > 0.03:
            raw_deviation *= (1.0 + vol * 0.20)
        al[bn] = _soft_cap_alpha(1.0 + raw_deviation, cap)

    # Step 3: Posterior → Henery
    pr = {k: tmp.get(k, 1/6) * max(.05, al[k]) for k in range(1, 7)}
    tp = sum(pr.values()); pr = {k: v / tp for k, v in pr.items()}
    pd = _henery_prob(pr, gamma=P["henery_gamma"])

    # [ML Override] MLモデルが提供されている場合、確率をMLの予測で上書き
    if ml_model is not None:
        ml_probs = ml_model.predict_proba(race_data)
        if ml_probs:
            # MLの確率とHarvilleの確率をブレンド (ML 70%, Harville 30%)
            for k in range(1, 7):
                pd[k] = ml_probs.get(k, 1/6) * 0.7 + pd[k] * 0.3
            tp = sum(pd.values())
            pd = {k: v / tp for k, v in pd.items()}

    boats = [{"boat": k, "name": rl[str(k)].get("name", "").strip(),
              "tmp": tmp.get(k, 0), "alpha": al[k], "post_prob": pd[k],
              "wd": wd[k], "reasons": rsn[k]} for k in range(1, 7)]

    # Step 4: Harville + Cond Dep
    harv = _cond_dep_adjust(_harville(pd), pd, venue)

    # インデックス構築（2連単/2連複/拡連複/3連複全対応）
    exacta_idx, quinella_idx, wide_idx, trifecta_combo_idx = _build_prob_index(harv)

    # Step 5: Bet extraction
    nc = sum(len(od.get(t, {})) for t in ["2連単","2連複","拡連複","3連単","3連複"])
    th2 = _adj_ev_th(P["ev_threshold_2ren"], nc, P)
    th3 = _adj_ev_th(P["ev_threshold_3ren"], nc, P)
    thw = _adj_ev_th(P["ev_threshold_wide"], nc, P)
    targets = []

    max_odds = P.get("max_odds", 80.0)
    max_ev = P.get("max_ev", 8.0)

    for k, odds in od.get("2連単", {}).items():
        pts = k.split('-')
        if len(pts) != 2: continue
        try: f, s = int(pts[0]), int(pts[1])
        except ValueError: continue
        o = float(odds)
        if o > max_odds: continue
        ep = exacta_idx.get((f, s), 0)
        ev = ep * o
        if ev >= th2 and ev <= max_ev and ep > .05:
            targets.append({"type":"2連単","combo":k,"prob":ep,"odds":o,"ev":ev})

    for k, odds in od.get("2連複", {}).items():
        pts = k.split('=')
        if len(pts) != 2: continue
        try: f, s = int(pts[0]), int(pts[1])
        except ValueError: continue
        o = float(odds)
        if o > max_odds: continue
        ep = quinella_idx.get(frozenset({f, s}), 0)
        ev = ep * o
        if ev >= th2 and ev <= max_ev and ep > .05:
            targets.append({"type":"2連複","combo":k,"prob":ep,"odds":o,"ev":ev})

    for k, os_str in od.get("拡連複", {}).items():
        pts = k.split('=')
        if len(pts) != 2: continue
        try:
            f, s = int(pts[0]), int(pts[1])
            ep = wide_idx.get(frozenset({f, s}), 0)
            mo = float(str(os_str).split('-')[0])
            if mo > max_odds: continue
            ev = ep * mo
            if ev >= thw and ev <= max_ev and ep > .15:
                targets.append({"type":"拡連複","combo":k,"prob":ep,"odds":mo,"ev":ev})
        except (ValueError, IndexError): pass

    # [v7.5_fixed] 3連単を除外: 90日間のバックテストで41件全て不的中(ROI -100%)
    # for k, odds in od.get("3連単", {}).items(): ... removed

    # [Issue#13] 3連複: インデックスから直接取得
    for k, odds in od.get("3連複", {}).items():
        try:
            bc = list(map(int, k.split('=')))
            if len(bc) != 3: continue
            o = float(odds)
            if o > max_odds: continue
            ep = trifecta_combo_idx.get(frozenset(bc), 0)
            ev = ep * o
            if ev >= th3 and ev <= max_ev and ep >= P["trifecta_min_prob_combo"]:
                targets.append({"type":"3連複","combo":k,"prob":ep,"odds":o,"ev":ev})
        except (ValueError, KeyError): pass

    targets.sort(key=lambda x: x["ev"], reverse=True)
    targets = targets[:P["max_targets"]]

    # === [Bug#11修正] Kelly: 正規化なし。kelly_quarterをそのまま比率として使用 ===
    corr_f = _hhi_correlation_penalty(targets)

    for t in targets:
        bv = t["odds"] - 1.
        kf = max(0., (t["prob"] * bv - (1 - t["prob"])) / bv) if bv > 0 else 0
        t["kelly_full"] = kf
        t["kelly_quarter"] = kf * P["kelly_fraction"] * corr_f

    # 各ベットの kelly_quarter がそのまま bankroll に対する比率
    # 個別キャップ: max_bet_ratio (20%)
    # 合計キャップ: max_total_bet_ratio (80%)
    # [Bug#32] MIN_BET_YENフロアによるオーバーベット防止:
    #   kelly_quarter が bankroll の 1% 未満の買い目は除外
    min_kelly_threshold = MIN_BET_YEN / max(bankroll, 1)  # bankroll=10000 → 0.01
    targets = [t for t in targets if t["kelly_quarter"] >= min_kelly_threshold]

    max_total = P.get("max_total_bet_ratio", 0.80)

    for t in targets:
        pct = min(t["kelly_quarter"], P["max_bet_ratio"])  # 個別キャップ
        t["kelly_pct"] = pct * 100
        raw_yen = bankroll * pct
        t["recommended_yen"] = max(MIN_BET_YEN, round(raw_yen / 100) * 100)
        t["concentration"] = False

    # 合計キャップ
    total_rec = sum(t["recommended_yen"] for t in targets)
    max_total_yen = bankroll * max_total
    if total_rec > max_total_yen and targets:
        sc = max_total_yen / total_rec
        for t in targets:
            t["recommended_yen"] = max(MIN_BET_YEN, round(t["recommended_yen"] * sc / 100) * 100)

    cal = {"n_candidates": nc,
           "ev_thresholds": {"2連": round(th2, 3), "3連": round(th3, 3), "拡連複": round(thw, 3)},
           "selection_bias_applied": nc > P["selection_bias_base"],
           "params_source": "custom" if params_override else ("file" if os.path.exists(PARAMS_FILE) else "default"),
           "correlation_penalty": round(corr_f, 3),
           "shrinkage_used": round(shrinkage, 3),
           "n_market_sources": n_sources}

    if targets:
        summary = {"count": len(targets), "avg_ev": sum(t["ev"] for t in targets) / len(targets),
                   "max_ev": targets[0]["ev"], "max_ev_combo": targets[0]["combo"],
                   "total_investment": sum(t["recommended_yen"] for t in targets),
                   "verdict": "投資実行", "concentration_mode": False, "top_kelly_ratio": 0,
                   "venue_volatility": vol, "motor_trust": mt, "correlation_factor": corr_f,
                   "tide_condition": tide, "alpha_sources_active": sum(1 for b in boats if b["reasons"]),
                   "calibration": cal}
    else:
        summary = {"count": 0, "avg_ev": 0, "max_ev": 0, "max_ev_combo": "",
                   "total_investment": 0, "verdict": "見（ケン）", "concentration_mode": False,
                   "top_kelly_ratio": 0, "venue_volatility": vol, "motor_trust": mt,
                   "correlation_factor": 1., "tide_condition": tide, "alpha_sources_active": 0,
                   "calibration": cal}

    return {"error": None, "boats": boats, "targets": targets, "summary": summary, "warnings": warns}
