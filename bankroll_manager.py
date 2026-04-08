"""
bankroll_manager.py — Daily Risk Management + Auto Circuit Breaker
===================================================================
機能:
  1. セッション（1日）を通じたバンクロール追跡
  2. ドローダウン自動停止（日次20%、全体30%）
  3. 連敗検知と自動ベットサイズ縮小
  4. レースごとの最適バンクロール割当
  5. PowerShellアラート連携用のステータス出力
"""
import json
import os
from datetime import datetime, date
from typing import Optional

MIN_BET_YEN = 100  # 最低賭金（円）

# ============================================================
# 定数
# ============================================================
DAILY_DD_LIMIT = 0.20         # 日次ドローダウン上限（当日開始資金の20%）
TOTAL_DD_LIMIT = 0.30         # 全体ドローダウン上限（初期資金の30%）
LOSING_STREAK_THRESHOLD = 5   # 連敗数この数を超えたらベットサイズ半減
LOSING_STREAK_REDUCTION = 0.5 # 連敗時のベットサイズ乗数
MAX_DAILY_RACES = 12          # 1日に賭ける最大レース数
MIN_BANKROLL_RATIO = 0.05     # 1レースに使うバンクロール最低比率
MAX_BANKROLL_RATIO = 0.30     # 1レースに使うバンクロール最大比率
STATE_FILE = "bankroll_state.json"


class BankrollManager:
    """
    日次セッションを管理するバンクロールマネージャ。
    app.pyから呼ばれ、各レースの投下資金上限を返す。
    """

    def __init__(self, initial_bankroll: float, state_file: str = STATE_FILE):
        self.state_file = state_file
        self.state = self._load_state(initial_bankroll)

    def _load_state(self, initial_bankroll: float) -> dict:
        """永続化された状態をロード。当日でなければリセット"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                # 日付が今日なら継続、違えば日次リセット
                if state.get("date") == date.today().isoformat():
                    return state
                else:
                    # 前日の最終バンクロールを引き継ぎ
                    carried = state.get("current_bankroll", initial_bankroll)
                    return self._new_day_state(carried)
            except (json.JSONDecodeError, IOError):
                pass
        return self._new_day_state(initial_bankroll)

    def _new_day_state(self, bankroll: float) -> dict:
        return {
            "date": date.today().isoformat(),
            "initial_bankroll": bankroll,       # 総初期資金（全体DD計算用）
            "day_start_bankroll": bankroll,      # 当日開始時の資金
            "current_bankroll": bankroll,        # 現在の資金
            "daily_invested": 0,                 # 当日投下総額
            "daily_payout": 0,                   # 当日回収総額
            "daily_pnl": 0,                      # 当日損益
            "races_today": 0,                    # 当日レース数
            "losing_streak": 0,                  # 連敗数
            "winning_streak": 0,                 # 連勝数
            "bets_log": [],                      # 当日のベット履歴
            "circuit_breaker": False,            # 停止フラグ
            "circuit_reason": "",                # 停止理由
        }

    def _save_state(self):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    # ============================================================
    # メイン API
    # ============================================================

    def get_race_budget(self, remaining_races_today: int = 6) -> dict:
        """
        次のレースに投下可能なバンクロール上限を返す。
        
        Returns: {
            "allowed": bool,
            "budget": float,
            "reason": str,
            "risk_level": "LOW" | "MEDIUM" | "HIGH" | "BLOCKED",
            "stats": dict
        }
        """
        s = self.state

        # Circuit Breaker チェック
        if s["circuit_breaker"]:
            return {
                "allowed": False,
                "budget": 0,
                "reason": f"🛑 自動停止中: {s['circuit_reason']}",
                "risk_level": "BLOCKED",
                "stats": self._stats(),
            }

        # [Bug#22修正] バンクロールが最低ベット額以下なら停止
        if s["current_bankroll"] < MIN_BET_YEN:
            return {
                "allowed": False,
                "budget": 0,
                "reason": f"🛑 残高不足（¥{s['current_bankroll']:,.0f} < 最低賭金¥{MIN_BET_YEN}）",
                "risk_level": "BLOCKED",
                "stats": self._stats(),
            }

        # 日次ドローダウンチェック
        daily_dd = -s["daily_pnl"] / max(s["day_start_bankroll"], 1)
        if daily_dd >= DAILY_DD_LIMIT:
            s["circuit_breaker"] = True
            s["circuit_reason"] = f"日次DD {daily_dd*100:.1f}% ≥ {DAILY_DD_LIMIT*100:.0f}%"
            self._save_state()
            return {
                "allowed": False,
                "budget": 0,
                "reason": f"🛑 日次ドローダウン上限到達 ({daily_dd*100:.1f}%)",
                "risk_level": "BLOCKED",
                "stats": self._stats(),
            }

        # 全体ドローダウンチェック
        total_dd = (s["initial_bankroll"] - s["current_bankroll"]) / max(s["initial_bankroll"], 1)
        if total_dd >= TOTAL_DD_LIMIT:
            s["circuit_breaker"] = True
            s["circuit_reason"] = f"全体DD {total_dd*100:.1f}% ≥ {TOTAL_DD_LIMIT*100:.0f}%"
            self._save_state()
            return {
                "allowed": False,
                "budget": 0,
                "reason": f"🛑 全体ドローダウン上限到達 ({total_dd*100:.1f}%)",
                "risk_level": "BLOCKED",
                "stats": self._stats(),
            }

        # 日次レース数上限
        if s["races_today"] >= MAX_DAILY_RACES:
            return {
                "allowed": False,
                "budget": 0,
                "reason": f"🛑 本日の最大レース数 ({MAX_DAILY_RACES}) に到達",
                "risk_level": "BLOCKED",
                "stats": self._stats(),
            }

        # バンクロール配分計算
        base_budget = s["current_bankroll"] * MAX_BANKROLL_RATIO

        # 残りレース数に応じた調整（均等配分ベース）
        if remaining_races_today > 0:
            even_split = s["current_bankroll"] / remaining_races_today
            base_budget = min(base_budget, even_split * 1.5)  # 均等の1.5倍まで

        # 連敗ペナルティ
        streak_mult = 1.0
        if s["losing_streak"] >= LOSING_STREAK_THRESHOLD:
            streak_mult = LOSING_STREAK_REDUCTION
            # さらに連敗が続くほど縮小
            extra = s["losing_streak"] - LOSING_STREAK_THRESHOLD
            streak_mult *= max(0.25, 1.0 - extra * 0.1)

        budget = max(
            s["current_bankroll"] * MIN_BANKROLL_RATIO,
            base_budget * streak_mult
        )
        budget = min(budget, s["current_bankroll"])  # 残高以上は賭けない
        budget = round(budget / 100) * 100  # 100円単位
        budget = max(100, budget)

        # リスクレベル判定
        if daily_dd >= DAILY_DD_LIMIT * 0.7:
            risk = "HIGH"
        elif daily_dd >= DAILY_DD_LIMIT * 0.4:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        reason_parts = []
        if s["losing_streak"] >= 3:
            reason_parts.append(f"⚠️ {s['losing_streak']}連敗中")
        if streak_mult < 1.0:
            reason_parts.append(f"ベットサイズ {streak_mult*100:.0f}%に縮小")
        if daily_dd > 0.1:
            reason_parts.append(f"日次DD {daily_dd*100:.1f}%")

        return {
            "allowed": True,
            "budget": budget,
            "reason": " / ".join(reason_parts) if reason_parts else "✅ 通常運用",
            "risk_level": risk,
            "stats": self._stats(),
        }

    def record_result(self, invested: float, payout: float, race_info: str = ""):
        """レース結果を記録"""
        s = self.state
        pnl = payout - invested

        s["daily_invested"] += invested
        s["daily_payout"] += payout
        s["daily_pnl"] += pnl
        s["current_bankroll"] += pnl
        # [Bug#14修正] バンクロールが負にならないよう下限を設定
        s["current_bankroll"] = max(0, s["current_bankroll"])
        s["races_today"] += 1

        if payout > invested:  # 純利益がある場合のみ勝ちとカウント
            s["losing_streak"] = 0
            s["winning_streak"] += 1
        else:
            s["winning_streak"] = 0
            s["losing_streak"] += 1

        s["bets_log"].append({
            "time": datetime.now().isoformat(),
            "race": race_info,
            "invested": invested,
            "payout": payout,
            "pnl": pnl,
            "bankroll_after": s["current_bankroll"],
        })

        self._save_state()

    def force_reset(self, new_bankroll: Optional[float] = None):
        """Circuit Breakerを手動リセット"""
        br = new_bankroll or self.state.get("current_bankroll", 10000)
        self.state = self._new_day_state(br)
        self.state["initial_bankroll"] = br
        self._save_state()

    def get_powershell_status(self) -> str:
        """PowerShellアラート連携用のステータス文字列"""
        s = self.state
        stats = self._stats()

        if s["circuit_breaker"]:
            return f"[BLOCKED] {s['circuit_reason']} | 残高: ¥{s['current_bankroll']:,.0f}"

        daily_dd = stats["daily_dd_pct"]
        risk = stats["risk_level"]

        icon = {"LOW": "✅", "MEDIUM": "⚠️", "HIGH": "🔴", "BLOCKED": "🛑"}.get(risk, "")

        return (
            f"{icon} [{risk}] "
            f"残高: ¥{s['current_bankroll']:,.0f} | "
            f"日次PnL: ¥{s['daily_pnl']:+,.0f} | "
            f"DD: {daily_dd:.1f}% | "
            f"連敗: {s['losing_streak']} | "
            f"本日{s['races_today']}R消化"
        )

    def _stats(self) -> dict:
        s = self.state
        daily_dd = -s["daily_pnl"] / max(s["day_start_bankroll"], 1) * 100
        total_dd = (s["initial_bankroll"] - s["current_bankroll"]) / max(s["initial_bankroll"], 1) * 100

        daily_dd_pct = max(0, daily_dd)
        total_dd_pct = max(0, total_dd)

        risk = "LOW"
        if daily_dd_pct >= DAILY_DD_LIMIT * 100 * 0.7:
            risk = "HIGH"
        elif daily_dd_pct >= DAILY_DD_LIMIT * 100 * 0.4:
            risk = "MEDIUM"
        if s["circuit_breaker"]:
            risk = "BLOCKED"

        return {
            "current_bankroll": s["current_bankroll"],
            "day_start": s["day_start_bankroll"],
            "daily_pnl": s["daily_pnl"],
            "daily_dd_pct": round(daily_dd_pct, 1),
            "total_dd_pct": round(total_dd_pct, 1),
            "races_today": s["races_today"],
            "losing_streak": s["losing_streak"],
            "winning_streak": s["winning_streak"],
            "risk_level": risk,
        }


# ============================================================
# CLI / テスト
# ============================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        bm = BankrollManager(initial_bankroll=10000)
        print(bm.get_powershell_status())
    elif len(sys.argv) > 1 and sys.argv[1] == "reset":
        br = float(sys.argv[2]) if len(sys.argv) > 2 else 10000
        bm = BankrollManager(initial_bankroll=br)
        bm.force_reset(br)
        print(f"リセット完了: ¥{br:,.0f}")
    else:
        # デモ
        bm = BankrollManager(initial_bankroll=10000)
        print("=== セッション開始 ===")
        print(bm.get_powershell_status())

        for i in range(8):
            budget = bm.get_race_budget(remaining_races_today=12 - i)
            print(f"\nRace {i+1}: {budget['reason']}")
            if budget["allowed"]:
                print(f"  予算: ¥{budget['budget']:,.0f} (リスク: {budget['risk_level']})")
                # シミュレート: 30%的中、平均3倍
                import random
                invested = min(500, budget["budget"])
                hit = random.random() < 0.3
                payout = invested * 3 if hit else 0
                bm.record_result(invested, payout, f"Demo {i+1}R")
                print(f"  結果: {'的中' if hit else 'ハズレ'} → PnL ¥{payout - invested:+,.0f}")
            else:
                print(f"  → 停止")
                break

        print(f"\n=== 最終状態 ===")
        print(bm.get_powershell_status())
