"""
data_quality.py — Data Quality Monitor, Odds Validator, Timestamp Tracker
==========================================================================
機能:
  1. オッズデータの整合性検証（サイレント破損検知）
  2. スクレイピング品質スコア算出
  3. オッズ取得タイムスタンプの記録
  4. 異常検知アラート生成
  5. HTMLスナップショットの保存（デバッグ・テスト用）
"""
import os
import json
import hashlib
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("rtpt.data_quality")

# ============================================================
# 1. オッズ整合性バリデーター
# ============================================================
class OddsValidator:
    """
    スクレイピングしたオッズの整合性を検証。
    「サイレントに壊れている」状態を検知する。
    """

    def validate(self, odds_data: dict) -> dict:
        """
        Returns: {
            "valid": bool,
            "score": float (0.0~1.0),
            "errors": list of str,
            "warnings": list of str,
        }
        """
        errors = []
        warnings = []
        checks_passed = 0
        checks_total = 0

        # --- Check 1: 単勝オッズが6艇分あるか ---
        checks_total += 1
        win = odds_data.get("単勝", {})
        if len(win) == 6:
            checks_passed += 1
        elif len(win) == 0:
            errors.append("単勝オッズが0件（スクレイピング完全失敗の可能性）")
        else:
            warnings.append(f"単勝オッズが{len(win)}件（6件期待）")
            checks_passed += 0.5

        # --- Check 2: 単勝オッズの合理性（合計オーバーラウンド） ---
        checks_total += 1
        if win:
            implied_total = sum(1.0 / max(float(v), 1.0) for v in win.values())
            # 正常: 1.15~1.40（控除率15~40%）
            if 1.05 <= implied_total <= 1.50:
                checks_passed += 1
            elif 0.80 <= implied_total <= 1.80:
                warnings.append(f"単勝オーバーラウンド異常: {implied_total:.2f}（通常1.15~1.40）")
                checks_passed += 0.5
            else:
                errors.append(f"単勝オーバーラウンド致命的異常: {implied_total:.2f}（パーサー破損の疑い）")

        # --- Check 3: 3連単が120通り近くあるか ---
        checks_total += 1
        trifecta = odds_data.get("3連単", {})
        if len(trifecta) >= 100:  # 一部不成立はありうるが100は欲しい
            checks_passed += 1
        elif len(trifecta) >= 50:
            warnings.append(f"3連単が{len(trifecta)}件（120件期待）")
            checks_passed += 0.5
        elif len(trifecta) > 0:
            warnings.append(f"3連単が{len(trifecta)}件のみ（大幅欠損）")
        else:
            errors.append("3連単オッズが0件")

        # --- Check 4: 3連単の買い目フォーマット検証 ---
        checks_total += 1
        format_ok = True
        for k, v in list(trifecta.items())[:20]:  # 先頭20件をサンプルチェック
            parts = k.split('-')
            if len(parts) != 3:
                format_ok = False
                errors.append(f"3連単フォーマット異常: '{k}'")
                break
            try:
                nums = [int(p) for p in parts]
                if not all(1 <= n <= 6 for n in nums):
                    format_ok = False
                    errors.append(f"3連単の艇番異常: '{k}' (1-6の範囲外)")
                    break
                if len(set(nums)) != 3:
                    format_ok = False
                    errors.append(f"3連単に重複艇番: '{k}'")
                    break
            except ValueError:
                format_ok = False
                errors.append(f"3連単の数値パースエラー: '{k}'")
                break
        if format_ok:
            checks_passed += 1

        # --- Check 5: オッズ値の範囲チェック ---
        checks_total += 1
        odds_ok = True
        all_odds_values = []
        for bet_type in ["3連単", "3連複", "2連単", "2連複"]:
            for k, v in odds_data.get(bet_type, {}).items():
                try:
                    val = float(v)
                    all_odds_values.append(val)
                    if val <= 0:
                        odds_ok = False
                        errors.append(f"{bet_type} '{k}' のオッズが0以下: {val}")
                        break
                    if val > 100000:
                        warnings.append(f"{bet_type} '{k}' のオッズが異常に高い: {val}")
                except (ValueError, TypeError):
                    odds_ok = False
                    errors.append(f"{bet_type} '{k}' のオッズが数値でない: {v}")
                    break
        if odds_ok:
            checks_passed += 1

        # --- Check 6: 2連単/2連複の件数チェック ---
        checks_total += 1
        nitan = len(odds_data.get("2連単", {}))
        nifuku = len(odds_data.get("2連複", {}))
        if nitan >= 25 and nifuku >= 10:
            checks_passed += 1
        elif nitan >= 15 or nifuku >= 5:
            warnings.append(f"2連単{nitan}件/2連複{nifuku}件（やや少ない）")
            checks_passed += 0.5
        else:
            errors.append(f"2連系オッズ大幅欠損: 2連単{nitan}件/2連複{nifuku}件")

        # --- Check 7: 最低オッズの合理性 ---
        checks_total += 1
        if all_odds_values:
            min_odds = min(all_odds_values)
            max_odds = max(all_odds_values)
            if min_odds >= 1.0 and max_odds <= 100000:
                checks_passed += 1
            else:
                warnings.append(f"オッズ範囲: {min_odds}~{max_odds}")
                checks_passed += 0.5

        # --- Check 8: 3連複と3連単の整合性 ---
        checks_total += 1
        sanfuku = odds_data.get("3連複", {})
        if trifecta and sanfuku:
            # 3連複のオッズは必ず対応する3連単の最低オッズ以下であるべき
            sample_ok = True
            for kf, vf in list(sanfuku.items())[:10]:
                parts_f = sorted(kf.split('='))
                min_exacta = float('inf')
                for perm_key in [
                    f"{a}-{b}-{c}"
                    for a in parts_f for b in parts_f for c in parts_f
                    if a != b and b != c and a != c
                ]:
                    if perm_key in trifecta:
                        min_exacta = min(min_exacta, float(trifecta[perm_key]))
                if min_exacta < float('inf') and float(vf) > min_exacta * 1.5:
                    # 3連複が3連単の1.5倍以上はおかしい（通常は6分の1程度）
                    sample_ok = False
                    break
            if sample_ok:
                checks_passed += 1
            else:
                warnings.append("3連複と3連単のオッズ整合性に疑問あり")
                checks_passed += 0.5
        else:
            checks_passed += 0.5  # チェック不能

        score = checks_passed / max(checks_total, 1)
        valid = len(errors) == 0 and score >= 0.6

        return {
            "valid": valid,
            "score": round(score, 2),
            "checks_passed": checks_passed,
            "checks_total": checks_total,
            "errors": errors,
            "warnings": warnings,
        }


# ============================================================
# 2. レース選手データバリデーター
# ============================================================
class RacelistValidator:
    """racelist（選手・展示データ）の整合性検証"""

    def validate(self, racelist: dict) -> dict:
        errors = []
        warnings = []

        if len(racelist) != 6:
            errors.append(f"艇数が{len(racelist)}（6艇期待）")

        for bn in range(1, 7):
            b = racelist.get(str(bn), {})
            if not b:
                errors.append(f"{bn}号艇のデータが空")
                continue

            # 名前
            if not b.get("name"):
                warnings.append(f"{bn}号艇の名前が未取得")

            # 級別
            cls = b.get("class", "")
            if cls not in ("A1", "A2", "B1", "B2", ""):
                warnings.append(f"{bn}号艇の級別が異常: '{cls}'")

            # 展示タイム
            et = b.get("exhibition_time", 0)
            if et > 0:
                if not (6.20 <= et <= 7.20):
                    warnings.append(f"{bn}号艇の展示タイム異常: {et}（通常6.30~7.00）")
            # et == 0 は展示前なのでエラーではない

            # 体重
            w = b.get("weight", 0)
            if w > 0 and not (40 <= w <= 70):
                warnings.append(f"{bn}号艇の体重異常: {w}kg")

            # チルト
            tilt = b.get("tilt")
            if tilt is not None and not (-0.5 <= tilt <= 3.0):
                warnings.append(f"{bn}号艇のチルト異常: {tilt}")

            # モーター2連率
            m2r = b.get("motor_2ren", 30.0)
            if not (10 <= m2r <= 80):
                warnings.append(f"{bn}号艇のモーター2連率異常: {m2r}%")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
        }


# ============================================================
# 3. オッズ取得タイムスタンプ + スナップショット
# ============================================================
class OddsTimestamp:
    """オッズ取得時刻の記録・管理"""

    SNAPSHOT_DIR = "odds_snapshots"

    def __init__(self):
        os.makedirs(self.SNAPSHOT_DIR, exist_ok=True)

    def record(self, race_data: dict, html_data: dict = None) -> dict:
        """
        タイムスタンプを記録し、オプションでHTMLスナップショットを保存。
        race_dataのmetadataに書き込む。
        """
        now = datetime.now()
        ts_info = {
            "odds_fetched_at": now.isoformat(),
            "odds_fetched_unix": now.timestamp(),
        }

        # メタデータに注入
        if "metadata" not in race_data:
            race_data["metadata"] = {}
        race_data["metadata"]["odds_timestamp"] = ts_info

        # HTMLスナップショット保存（デバッグ用）
        if html_data:
            meta = race_data.get("metadata", {})
            prefix = f"{meta.get('date', 'unknown')}_{meta.get('stadium', 'unknown')}_{meta.get('race_number', '0R')}"
            snapshot_dir = os.path.join(self.SNAPSHOT_DIR, prefix)
            os.makedirs(snapshot_dir, exist_ok=True)

            for key, html in html_data.items():
                if html:
                    fpath = os.path.join(snapshot_dir, f"{key}.html")
                    with open(fpath, 'w', encoding='utf-8') as f:
                        f.write(html)

            ts_info["snapshot_path"] = snapshot_dir

        return ts_info

    def get_age_seconds(self, race_data: dict) -> float:
        """オッズ取得からの経過秒数"""
        ts = race_data.get("metadata", {}).get("odds_timestamp", {})
        fetched_unix = ts.get("odds_fetched_unix", 0)
        if fetched_unix == 0:
            return float('inf')
        return datetime.now().timestamp() - fetched_unix


# ============================================================
# 4. 総合データ品質スコア
# ============================================================
class DataQualityMonitor:
    """
    全データソースの品質を統合評価。
    品質が低い場合は警告を出し、エンジンに「信頼度」を渡す。
    """

    def __init__(self):
        self.odds_validator = OddsValidator()
        self.racelist_validator = RacelistValidator()
        self.timestamp = OddsTimestamp()

    def assess(self, race_data: dict, html_data: dict = None) -> dict:
        """
        Returns: {
            "overall_score": float (0.0~1.0),
            "tradeable": bool,      # True = 賭けてよい品質
            "odds_quality": dict,
            "racelist_quality": dict,
            "timestamp": dict,
            "all_warnings": list,
            "all_errors": list,
            "recommendation": str,
        }
        """
        # タイムスタンプ記録
        ts = self.timestamp.record(race_data, html_data)

        # オッズ検証
        odds_q = self.odds_validator.validate(race_data.get("odds", {}))

        # 選手データ検証
        rl_q = self.racelist_validator.validate(race_data.get("racelist", {}))

        # 統合スコア
        scores = [odds_q["score"]]
        if rl_q["valid"]:
            scores.append(1.0)
        elif rl_q["errors"]:
            scores.append(0.3)
        else:
            scores.append(0.7)

        overall = sum(scores) / len(scores)

        all_warnings = odds_q["warnings"] + rl_q["warnings"]
        all_errors = odds_q["errors"] + rl_q["errors"]

        # 判定
        if overall >= 0.8 and not all_errors:
            tradeable = True
            rec = "✅ データ品質良好。通常運用可能。"
        elif overall >= 0.5 and len(all_errors) <= 1:
            tradeable = True
            rec = f"⚠️ データ品質やや低下（スコア{overall:.0%}）。注意して運用。"
        else:
            tradeable = False
            rec = f"🛑 データ品質不足（スコア{overall:.0%}）。このレースは見送り推奨。"

        return {
            "overall_score": round(overall, 2),
            "tradeable": tradeable,
            "odds_quality": odds_q,
            "racelist_quality": rl_q,
            "timestamp": ts,
            "all_warnings": all_warnings,
            "all_errors": all_errors,
            "recommendation": rec,
        }


# ============================================================
# 5. HTMLスナップショットからのリグレッションテスト用
# ============================================================
class OddsParserTester:
    """
    保存済みHTMLスナップショットを使ってオッズパーサーをテスト。
    HTML構造が変わった時の検知に使う。

    使い方:
      python data_quality.py test_parser --snapshot odds_snapshots/20260101_住之江_3R/
    """

    def test_snapshot(self, snapshot_dir: str, parse_func, race_data_template: dict) -> dict:
        """
        保存済みHTMLでパーサーを実行し、結果を検証。
        parse_func: parse_all_odds関数
        """
        html_data = {}
        for fname in os.listdir(snapshot_dir):
            if fname.endswith('.html'):
                key = fname.replace('.html', '')
                with open(os.path.join(snapshot_dir, fname), 'r', encoding='utf-8') as f:
                    html_data[key] = f.read()

        # パース実行
        import copy
        rd = copy.deepcopy(race_data_template)
        parse_func(html_data, rd)

        # 検証
        validator = OddsValidator()
        result = validator.validate(rd.get("odds", {}))
        result["snapshot_dir"] = snapshot_dir
        result["odds_counts"] = {k: len(v) for k, v in rd.get("odds", {}).items()}

        return result


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test_parser":
        if len(sys.argv) > 3:
            snapshot = sys.argv[3]
        else:
            print("Usage: python data_quality.py test_parser --snapshot <dir>")
            sys.exit(1)

        # app.pyはStreamlit環境が必要なため、完全なmockで全st.*呼び出しを無効化
        import sys
        import types

        class _StreamlitMock(types.ModuleType):
            """app.pyのモジュールレベルst.*呼び出しを全て吸収するモック"""
            def __getattr__(self, name):
                # 任意の属性アクセスにcallable mockを返す
                def _noop(*a, **kw):
                    return lambda f: f  # デコレータ対応
                return _noop

        mock_st = _StreamlitMock("streamlit")
        orig_st = sys.modules.get("streamlit")
        sys.modules["streamlit"] = mock_st
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("app_module", "app.py")
            app_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(app_module)
            parse_all_odds = app_module.parse_all_odds
        finally:
            # 元のstreamlitモジュールを復元
            if orig_st is not None:
                sys.modules["streamlit"] = orig_st
            else:
                sys.modules.pop("streamlit", None)
        tester = OddsParserTester()
        template = {"odds": {"3連単": {}, "3連複": {}, "2連単": {}, "2連複": {}, "拡連複": {}, "単勝": {}, "複勝": {}}}
        result = tester.test_snapshot(snapshot, parse_all_odds, template)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Usage:")
        print("  python data_quality.py test_parser --snapshot <dir>")
