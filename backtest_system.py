"""
backtest_system.py — Result Reconciliation + Walk-Forward Backtest + Calibration
==================================================================================
機能:
  1. boatrace.jpからレース結果を自動スクレイピング
  2. predictions_log.csvとの自動照合（的中/不的中/払戻額の書き戻し）
  3. Walk-Forward バックテスト（N日訓練 → M日テスト → ロールフォワード）
  4. Calibration検証（推定確率 vs 実際の的中率の一致度）
  5. αパラメータの最適化（グリッドサーチ）
  6. パフォーマンス指標（ROI, Sharpe, MaxDD, 的中率, 回収率）

使い方:
  # 結果照合
  python backtest_system.py reconcile --log predictions_log.csv

  # バックテスト（過去90日、30日訓練/7日テスト）
  python backtest_system.py backtest --days 90 --train 30 --test 7

  # Calibration検証
  python backtest_system.py calibrate --log predictions_log.csv
"""
import requests
import re
import csv
import os
import json
import math
import time
from datetime import datetime, timedelta, date
from bs4 import BeautifulSoup
from collections import defaultdict

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

JCD_MAP = {
    "桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05",
    "浜名湖": "06", "蒲郡": "07", "常滑": "08", "津": "09", "三国": "10",
    "びわこ": "11", "住之江": "12", "尼崎": "13", "鳴門": "14", "丸亀": "15",
    "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20",
    "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24"
}
JCD_REVERSE = {v: k for k, v in JCD_MAP.items()}


# ============================================================
# 1. レース結果スクレイピング
# ============================================================
class ResultScraper:
    """boatrace.jpからレース結果を取得"""

    BASE = "https://www.boatrace.jp/owpc/pc/race"

    def fetch_result(self, date_str, jcd, rno, session=None):
        """
        1レースの結果を取得。
        Returns: {
            "1st": int, "2nd": int, "3rd": int,
            "payouts": {"3連単": {"combo": "x-y-z", "payout": int}, ...}
        } or None
        """
        s = session or requests.Session()
        s.headers.update(HEADERS)
        url = f"{self.BASE}/raceresult?rno={rno}&jcd={jcd}&hd={date_str}"
        try:
            res = s.get(url, timeout=10)
            res.raise_for_status()
            res.encoding = 'utf-8'
        except Exception:
            return None

        soup = BeautifulSoup(res.text, 'html.parser')
        result = {"1st": 0, "2nd": 0, "3rd": 0, "payouts": {}}

        # 着順取得
        result_table = soup.select_one('.is-w495')
        if not result_table:
            return None

        rows = result_table.select('tbody tr')
        for row in rows:
            tds = row.find_all('td')
            if len(tds) < 2:
                continue
            rank_text = tds[0].text.strip()
            boat_match = re.search(r'[1-6]', tds[1].text)
            if not boat_match:
                continue
            boat_no = int(boat_match.group())
            if rank_text == '1':
                result["1st"] = boat_no
            elif rank_text == '2':
                result["2nd"] = boat_no
            elif rank_text == '3':
                result["3rd"] = boat_no

        # 払戻金取得
        # [Bug#31修正] 拡連複/複勝は複数行をリストで保存
        # 払戻金テーブルの行は "組番 + 金額" の2セル以上で、
        # 組番は 数字[-=]数字 のパターン（単勝/複勝は1桁数字）
        combo_pattern = re.compile(r'^[1-6](?:[-=][1-6]){0,2}$')
        for table in soup.select('.table1'):
            text = table.get_text()
            for bet_type in ["3連単", "3連複", "2連単", "2連複", "拡連複", "単勝", "複勝"]:
                if bet_type in text:
                    rows = table.select('tr')
                    for row in rows:
                        tds = row.find_all('td')
                        if len(tds) < 2: continue
                        combo_text = tds[0].text.strip()
                        # 組番が有効なパターンか検証（非払戻テーブルの誤取得を防止）
                        if not combo_pattern.match(combo_text):
                            continue
                        payout_text = tds[-1].text.strip()
                        payout_match = re.search(r'[\d]+', payout_text.replace(',', '').replace('¥', ''))
                        if payout_match:
                            try:
                                payout_val = int(payout_match.group())
                                entry = {"combo": combo_text, "payout": payout_val}
                                if bet_type in ("拡連複", "複勝"):
                                    if bet_type not in result["payouts"]:
                                        result["payouts"][bet_type] = []
                                    if isinstance(result["payouts"][bet_type], list):
                                        result["payouts"][bet_type].append(entry)
                                    else:
                                        result["payouts"][bet_type] = [result["payouts"][bet_type], entry]
                                else:
                                    result["payouts"][bet_type] = entry
                            except ValueError:
                                pass

        if result["1st"] == 0:
            return None
        return result

    def fetch_day_results(self, date_str):
        """1日分の全場全レースの結果を取得"""
        results = {}
        session = requests.Session()
        session.headers.update(HEADERS)

        for venue_name, jcd in JCD_MAP.items():
            for rno in range(1, 13):
                result = self.fetch_result(date_str, jcd, rno, session)
                if result:
                    key = f"{date_str}_{venue_name}_{rno}R"
                    results[key] = result
                time.sleep(0.3)  # サーバー負荷軽減

        return results


# ============================================================
# 2. 予想ログ照合
# ============================================================
class Reconciler:
    """predictions_log.csvとレース結果を照合"""

    def reconcile(self, log_path, results_cache_dir="results_cache"):
        """
        CSVの未照合行にレース結果を書き戻す。
        results_cache_dirに日付ごとのJSON結果をキャッシュ。
        """
        os.makedirs(results_cache_dir, exist_ok=True)

        rows = []
        with open(log_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            if not fieldnames:
                return 0  # 空のCSVファイル
            rows = list(reader)

        scraper = ResultScraper()
        updated = 0
        dates_needed = set()

        # まず必要な日付を洗い出し
        for row in rows:
            if not row.get("hit"):  # 未照合
                dates_needed.add(row["date"])

        # 日付ごとに結果を取得（キャッシュ活用）
        results_by_date = {}
        for d in dates_needed:
            cache_file = os.path.join(results_cache_dir, f"{d}.json")
            if os.path.exists(cache_file):
                with open(cache_file, 'r', encoding='utf-8') as f:
                    results_by_date[d] = json.load(f)
            else:
                print(f"  結果取得中: {d}")
                day_results = scraper.fetch_day_results(d)
                results_by_date[d] = day_results
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(day_results, f, ensure_ascii=False, indent=2)
                time.sleep(1)

        # 照合
        for row in rows:
            if row.get("hit"):
                continue

            d = row["date"]
            venue = row["stadium"]
            race = row["race"]  # "3R"
            rno = race.replace("R", "")
            key = f"{d}_{venue}_{race}"

            day_res = results_by_date.get(d, {})
            result = day_res.get(key)
            if not result:
                continue

            row["result_1st"] = str(result["1st"])
            row["result_2nd"] = str(result["2nd"])
            row["result_3rd"] = str(result["3rd"])

            # 的中判定
            combo = row["combo"]
            bet_type = row["type"]
            hit = self._check_hit(combo, bet_type, result)
            row["hit"] = "1" if hit else "0"

            if hit:
                # [Bug#31修正] 的中した買い目に一致する払戻金を検索
                payout_info = result["payouts"].get(bet_type, {})
                payout_val = 0
                target_combo = row["combo"]

                if isinstance(payout_info, list):
                    # 拡連複/複勝: 複数組の中から一致するcomboを探す
                    for pi in payout_info:
                        if isinstance(pi, dict):
                            # comboの正規化比較（"1=2" と "2=1" を一致させる）
                            stored = pi.get("combo", "")
                            if self._normalize_combo(stored) == self._normalize_combo(target_combo):
                                payout_val = pi.get("payout", 0)
                                break
                    # 見つからなかった場合はフォールバック（最小値を使用）
                    if payout_val == 0 and payout_info:
                        positive_payouts = [
                            pi.get("payout", 0) for pi in payout_info
                            if isinstance(pi, dict) and pi.get("payout", 0) > 0
                        ]
                        if positive_payouts:
                            payout_val = min(positive_payouts)
                elif isinstance(payout_info, dict):
                    payout_val = payout_info.get("payout", 0)

                row["payout"] = str(payout_val)
            else:
                row["payout"] = "0"

            updated += 1

        # 書き戻し
        with open(log_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        return updated

    def _check_hit(self, combo, bet_type, result):
        """買い目が的中したかを判定"""
        r1, r2, r3 = result["1st"], result["2nd"], result["3rd"]

        if bet_type == "3連単":
            parts = combo.split('-')
            if len(parts) == 3:
                return [int(p) for p in parts] == [r1, r2, r3]

        elif bet_type == "3連複":
            parts = sorted(int(p) for p in combo.split('='))
            return parts == sorted([r1, r2, r3])

        elif bet_type == "2連単":
            parts = combo.split('-')
            if len(parts) == 2:
                return [int(p) for p in parts] == [r1, r2]

        elif bet_type == "2連複":
            parts = sorted(int(p) for p in combo.split('='))
            return parts == sorted([r1, r2])

        elif bet_type == "拡連複":
            parts = set(int(p) for p in combo.split('='))
            top3 = {r1, r2, r3}
            return parts.issubset(top3)

        elif bet_type == "単勝":
            return int(combo) == r1

        elif bet_type == "複勝":
            return int(combo) in [r1, r2, r3]

        return False

    @staticmethod
    def _normalize_combo(combo_str: str) -> str:
        """[Bug#31] 買い目文字列を正規化して比較可能にする"""
        # "1=2" と "2=1" を一致させる。全角数字も処理。
        import re
        combo_str = combo_str.translate(str.maketrans('１２３４５６＝－', '123456=-'))
        combo_str = combo_str.strip()
        if '=' in combo_str:
            parts = sorted(combo_str.split('='))
            return '='.join(parts)
        elif '-' in combo_str:
            return combo_str  # 単式は順序が意味を持つのでそのまま
        return combo_str


# ============================================================
# 3. パフォーマンス分析
# ============================================================
class PerformanceAnalyzer:
    """照合済みログからパフォーマンス指標を計算"""

    def analyze(self, log_path):
        """Returns: dict of performance metrics"""
        rows = []
        with open(log_path, 'r', encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))

        reconciled = [r for r in rows if r.get("hit") in ("0", "1")]
        if not reconciled:
            return {"error": "照合済みデータなし"}

        total_bets = len(reconciled)
        total_invested = sum(int(r.get("recommended_yen", 100)) for r in reconciled)
        # payout on boatrace.jp is per 100-yen ticket; scale by actual bet size
        total_payout = sum(
            int(r.get("payout", 0)) * int(r.get("recommended_yen", 100)) // 100
            for r in reconciled if r["hit"] == "1"
        )
        hits = sum(1 for r in reconciled if r["hit"] == "1")

        # 日別P&L
        daily_pnl = defaultdict(float)
        daily_invested = defaultdict(float)
        for r in reconciled:
            d = r["date"]
            invested = int(r.get("recommended_yen", 100))
            raw_payout = int(r.get("payout", 0)) if r["hit"] == "1" else 0
            payout = raw_payout * invested // 100  # scale per-100-yen payout to actual bet
            daily_pnl[d] += (payout - invested)
            daily_invested[d] += invested

        # 累積リターン
        sorted_dates = sorted(daily_pnl.keys())
        cumulative = []
        running = 0
        for d in sorted_dates:
            running += daily_pnl[d]
            cumulative.append(running)

        # Max Drawdown
        peak = 0
        max_dd = 0
        for c in cumulative:
            if c > peak:
                peak = c
            dd = peak - c
            if dd > max_dd:
                max_dd = dd

        # Sharpe Ratio (日次)
        daily_returns = [daily_pnl[d] / max(daily_invested[d], 1) for d in sorted_dates]
        if len(daily_returns) > 1:
            mean_r = sum(daily_returns) / len(daily_returns)
            std_r = max(0.001, (sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)) ** 0.5)
            sharpe = mean_r / std_r * (252 ** 0.5)  # 年率換算
        else:
            sharpe = 0

        # 券種別成績
        by_type = defaultdict(lambda: {"bets": 0, "hits": 0, "invested": 0, "payout": 0})
        for r in reconciled:
            bt = r["type"]
            by_type[bt]["bets"] += 1
            by_type[bt]["invested"] += int(r.get("recommended_yen", 100))
            if r["hit"] == "1":
                by_type[bt]["hits"] += 1
                bet_yen = int(r.get("recommended_yen", 100))
                by_type[bt]["payout"] += int(r.get("payout", 0)) * bet_yen // 100

        return {
            "total_bets": total_bets,
            "total_invested": total_invested,
            "total_payout": total_payout,
            "roi": (total_payout - total_invested) / max(total_invested, 1) * 100,
            "hit_rate": hits / max(total_bets, 1) * 100,
            "hits": hits,
            "max_drawdown": max_dd,
            "sharpe_ratio": round(sharpe, 2),
            "n_days": len(sorted_dates),
            "daily_avg_pnl": sum(daily_pnl.values()) / max(len(daily_pnl), 1),
            "by_type": dict(by_type),
            "cumulative_pnl": list(zip(sorted_dates, cumulative)),
        }


# ============================================================
# 4. Calibration検証
# ============================================================
class CalibrationChecker:
    """推定確率 vs 実的中率の一致度を検証"""

    BUCKETS = [(0, 5), (5, 10), (10, 20), (20, 30), (30, 50), (50, 100)]

    def check(self, log_path):
        rows = []
        with open(log_path, 'r', encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))

        reconciled = [r for r in rows if r.get("hit") in ("0", "1")]
        if not reconciled:
            return {"error": "照合済みデータなし"}

        buckets = {}
        for lo, hi in self.BUCKETS:
            label = f"{lo}-{hi}%"
            bucket_rows = [
                r for r in reconciled
                if lo <= float(r.get("prob_pct", 0)) < hi
            ]
            if bucket_rows:
                n = len(bucket_rows)
                hits = sum(1 for r in bucket_rows if r["hit"] == "1")
                actual_rate = hits / n * 100
                expected_rate = sum(float(r.get("prob_pct", 0)) for r in bucket_rows) / n
                buckets[label] = {
                    "n": n,
                    "hits": hits,
                    "actual_rate_pct": round(actual_rate, 1),
                    "expected_rate_pct": round(expected_rate, 1),
                    "gap": round(actual_rate - expected_rate, 1),
                    "calibrated": abs(actual_rate - expected_rate) < 5.0,
                }

        # Brier Score
        brier = 0
        for r in reconciled:
            p = float(r.get("prob_pct", 0)) / 100
            outcome = 1.0 if r["hit"] == "1" else 0.0
            brier += (p - outcome) ** 2
        brier /= max(len(reconciled), 1)

        return {
            "buckets": buckets,
            "brier_score": round(brier, 4),
            "brier_interpretation": (
                "優秀 (<0.15)" if brier < 0.15 else
                "普通 (0.15-0.25)" if brier < 0.25 else
                "要改善 (>0.25)"
            ),
            "total_evaluated": len(reconciled),
        }


# ============================================================
# 5. Walk-Forward バックテスト
# ============================================================
class WalkForwardBacktester:
    """
    Walk-Forward検証: 訓練期間のデータでαを最適化し、
    テスト期間で未知データに対する性能を評価する。

    注意: この実装には boatrace.jp の過去データ(展示情報含む)が必要。
    過去の展示情報は公式サイトからは取得困難なため、
    racelist + beforeinfo のJSON保存が前提。
    """

    def __init__(self, data_dir="race_data_archive"):
        """
        data_dir: 日付ごとのレースデータJSONが保存されているディレクトリ
        ファイル形式: {date}_{venue}_{rno}R.json (analyze()に渡す形式)
        """
        self.data_dir = data_dir

    def run(self, total_days=90, train_days=30, test_days=7, bankroll=10000):
        """
        Walk-Forward実行。
        Returns: list of window results
        """
        today = date.today()
        start_date = today - timedelta(days=total_days)

        windows = []
        current_start = start_date

        while current_start + timedelta(days=train_days + test_days) <= today:
            train_end = current_start + timedelta(days=train_days)
            test_end = train_end + timedelta(days=test_days)

            # 訓練期間: αパラメータを最適化
            train_data = self._load_period(current_start, train_end)
            if not train_data:
                current_start += timedelta(days=test_days)
                continue

            optimized_params = self._optimize_alpha(train_data)

            # テスト期間: 最適化済みパラメータで予測実行
            test_data = self._load_period(train_end, test_end)
            if not test_data:
                current_start += timedelta(days=test_days)
                continue

            test_results = self._evaluate(test_data, optimized_params, bankroll)

            windows.append({
                "train_period": f"{current_start} ~ {train_end}",
                "test_period": f"{train_end} ~ {test_end}",
                "train_races": len(train_data),
                "test_races": len(test_data),
                "optimized_params": optimized_params,
                "test_roi": test_results["roi"],
                "test_hits": test_results["hits"],
                "test_bets": test_results["total_bets"],
                "test_pnl": test_results["pnl"],
            })

            current_start += timedelta(days=test_days)

        # サマリー
        if windows:
            avg_roi = sum(w["test_roi"] for w in windows) / len(windows)
            total_pnl = sum(w["test_pnl"] for w in windows)
            consistent = sum(1 for w in windows if w["test_roi"] > 0)
            return {
                "windows": windows,
                "avg_test_roi": round(avg_roi, 2),
                "total_pnl": total_pnl,
                "profitable_windows": f"{consistent}/{len(windows)}",
                "consistency_rate": round(consistent / len(windows) * 100, 1),
            }
        return {"windows": [], "error": "データ不足"}

    def _load_period(self, start, end):
        """指定期間のレースデータをロード"""
        data = []
        current = start
        while current < end:
            date_str = current.strftime("%Y%m%d")
            pattern = f"{date_str}_"
            if os.path.isdir(self.data_dir):
                for fname in os.listdir(self.data_dir):
                    if fname.startswith(pattern) and fname.endswith('.json'):
                        fpath = os.path.join(self.data_dir, fname)
                        try:
                            with open(fpath, 'r', encoding='utf-8') as f:
                                archive = json.load(f)
                            # [Bug#20修正] アーカイブ構造を正規化
                            # historical_scraper形式: {"race_data": {...}, "actual_result": {...}}
                            # 旧形式: レースデータが直接トップレベル
                            if "race_data" in archive:
                                entry = archive["race_data"]
                                entry["actual_result"] = archive.get("actual_result")
                            else:
                                entry = archive
                            data.append(entry)
                        except (json.JSONDecodeError, IOError):
                            pass
            current += timedelta(days=1)
        return data

    def _optimize_alpha(self, train_data):
        """
        訓練データでαパラメータを最適化。
        グリッドサーチ（各パラメータ±20%の範囲）でBrier Scoreを最小化。
        """
        # 最適化対象のパラメータ
        # [wall_decay_strong, wall_decay_weak, ex_time_coeff,
        #  motor_coeff, weight_coeff, henery_gamma]
        # [Bug#27修正] henery_gamma を v7.5 のデフォルト 0.91 に合わせる
        initial = [1.30, 1.12, 0.06, 0.12, 0.008, 0.91]

        best_params = initial
        best_score = self._brier_score(train_data, initial)

        # 簡易グリッドサーチ + 局所探索
        # 各パラメータを±20%の範囲で5点探索
        for param_idx in range(len(initial)):
            base = initial[param_idx]
            for mult in [0.8, 0.9, 1.0, 1.1, 1.2]:
                trial = list(best_params)
                trial[param_idx] = base * mult
                score = self._brier_score(train_data, trial)
                if score < best_score:
                    best_score = score
                    best_params = trial

        return {
            "wall_decay_strong": round(best_params[0], 3),
            "wall_decay_weak": round(best_params[1], 3),
            "ex_time_coeff": round(best_params[2], 4),
            "motor_coeff": round(best_params[3], 4),
            "weight_coeff": round(best_params[4], 4),
            "henery_gamma": round(best_params[5], 3),
            "train_brier": round(best_score, 4),
        }

    def _brier_score(self, data, params):
        """パラメータセットに対するBrier Scoreを計算"""
        from rtpt_engine import analyze as engine_analyze

        # [Bug#2修正] params を engine の params_override に変換して渡す
        engine_params = {
            "wall_decay_strong": params[0],
            "wall_decay_weak": params[1],
            "ex_time_coeff": params[2],
            "motor_coeff": params[3],
            "weight_coeff": params[4],
            "henery_gamma": params[5],
        }

        total_brier = 0
        n = 0

        for race_data in data:
            result = race_data.get("actual_result")
            if not result:
                continue

            try:
                analysis = engine_analyze(race_data, bankroll=1000, params_override=engine_params)
            except Exception:
                continue

            if analysis.get("error"):
                continue

            # 単勝の calibration で Brier を計算
            actual_winner = result.get("1st", 0)
            for boat in analysis["boats"]:
                p = boat["post_prob"]
                outcome = 1.0 if boat["boat"] == actual_winner else 0.0
                total_brier += (p - outcome) ** 2
                n += 1

        return total_brier / max(n, 1)

    def _evaluate(self, test_data, params, bankroll):
        """テストデータでパフォーマンスを評価"""
        from rtpt_engine import analyze as engine_analyze

        total_invested = 0
        total_payout = 0
        total_bets = 0
        hits = 0

        for race_data in test_data:
            result = race_data.get("actual_result")
            if not result:
                continue

            try:
                analysis = engine_analyze(race_data, bankroll, params_override=params)
            except Exception:
                continue

            if analysis.get("error") or not analysis.get("targets"):
                continue

            for target in analysis["targets"]:
                bet_amount = target["recommended_yen"]
                total_invested += bet_amount
                total_bets += 1

                reconciler = Reconciler()
                is_hit = reconciler._check_hit(
                    target["combo"], target["type"], result
                )
                if is_hit:
                    hits += 1
                    # [Bug#18修正] オッズは倍率（5.0 = 5倍）。
                    # 賭金 × オッズ = 払戻金。100円あたりではなく賭金あたり。
                    # boatrace.jpの表示オッズは「100円あたり」の配当を100で割った倍率。
                    # extract_float("5.0") → 5.0 は倍率として正しい。
                    payout = bet_amount * target["odds"]
                    total_payout += payout

        roi = ((total_payout - total_invested) / max(total_invested, 1)) * 100
        return {
            "total_invested": total_invested,
            "total_payout": round(total_payout),
            "roi": round(roi, 2),
            "hits": hits,
            "total_bets": total_bets,
            "pnl": round(total_payout - total_invested),
        }


# ============================================================
# 6. レースデータ・アーカイバ（バックテスト用データ蓄積）
# ============================================================
class RaceDataArchiver:
    """
    毎日の解析時にレースデータ + 結果をJSONで保存。
    Walk-Forwardバックテストのデータソースになる。
    """

    def __init__(self, archive_dir="race_data_archive"):
        self.archive_dir = archive_dir
        os.makedirs(archive_dir, exist_ok=True)

    def save(self, race_data, analysis_result=None):
        """レースデータと解析結果をアーカイブ"""
        meta = race_data.get("metadata", {})
        d = meta.get("date", "unknown")
        venue = meta.get("stadium", "unknown")
        race = meta.get("race_number", "0R")

        fname = f"{d}_{venue}_{race}.json"
        fpath = os.path.join(self.archive_dir, fname)

        archive = {
            "race_data": race_data,
            "analysis": analysis_result,
            "actual_result": None,
            "archived_at": datetime.now().isoformat(),
        }

        with open(fpath, 'w', encoding='utf-8') as f:
            json.dump(archive, f, ensure_ascii=False, indent=2)

    def attach_result(self, date_str, venue, rno):
        """アーカイブ済みデータにレース結果を付加"""
        fname = f"{date_str}_{venue}_{rno}R.json"
        fpath = os.path.join(self.archive_dir, fname)

        if not os.path.exists(fpath):
            return False

        scraper = ResultScraper()
        jcd = JCD_MAP.get(venue)
        if not jcd:
            return False

        result = scraper.fetch_result(date_str, jcd, rno)
        if not result:
            return False

        with open(fpath, 'r', encoding='utf-8') as f:
            archive = json.load(f)

        archive["actual_result"] = result

        with open(fpath, 'w', encoding='utf-8') as f:
            json.dump(archive, f, ensure_ascii=False, indent=2)

        return True


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python backtest_system.py reconcile --log predictions_log.csv")
        print("  python backtest_system.py calibrate --log predictions_log.csv")
        print("  python backtest_system.py performance --log predictions_log.csv")
        print("  python backtest_system.py backtest --days 90 --train 30 --test 7")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "reconcile":
        log = sys.argv[3] if len(sys.argv) > 3 else "predictions_log.csv"
        r = Reconciler()
        updated = r.reconcile(log)
        print(f"照合完了: {updated}件更新")

    elif cmd == "calibrate":
        log = sys.argv[3] if len(sys.argv) > 3 else "predictions_log.csv"
        cc = CalibrationChecker()
        result = cc.check(log)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "performance":
        log = sys.argv[3] if len(sys.argv) > 3 else "predictions_log.csv"
        pa = PerformanceAnalyzer()
        result = pa.analyze(log)
        # 累積PnLは長いので省略表示
        display = {k: v for k, v in result.items() if k != "cumulative_pnl"}
        print(json.dumps(display, ensure_ascii=False, indent=2, default=str))

    elif cmd == "backtest":
        days = 90
        train = 30
        test = 7
        for i, arg in enumerate(sys.argv):
            if arg == "--days" and i + 1 < len(sys.argv):
                days = int(sys.argv[i + 1])
            elif arg == "--train" and i + 1 < len(sys.argv):
                train = int(sys.argv[i + 1])
            elif arg == "--test" and i + 1 < len(sys.argv):
                test = int(sys.argv[i + 1])

        bt = WalkForwardBacktester()
        result = bt.run(total_days=days, train_days=train, test_days=test)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
