"""
alpha_adapter.py — Online Alpha Reliability Tracker
=====================================================
問題: 10個のαソースが全て同じ信頼度で適用されているが、
      実際には「効いているα」と「ノイズなα」がある。

解決: 各αの予測精度を追跡し、実績に基づいて自動的に
      信頼度（重み）を調整するオンライン学習システム。

手法: Exponential Weighted Moving Average (EWMA) による
      各αの「発火時の的中率」追跡。
      的中率が高いαほど重みを増やし、低いαは減衰させる。
"""
import json
import os
import math
from datetime import datetime, date
from typing import Dict, List, Optional
from collections import defaultdict

STATE_FILE = "alpha_reliability.json"

# αソースの識別子リスト（rtpt_engine.pyのreasonsから抽出）
ALPHA_SOURCES = [
    "VoidExploit",    # Wall Decay (ΔST) — 外側有利
    "WallDecay",      # Wall Decay — 内側不利（大）
    "WallHalf",       # [Bug#23修正] Wall Decay — 内側不利（小）
    "ExT",            # 展示タイム × チルト
    "Mot",            # モーター2連率
    "STRev",          # ST回帰（展示が平均より速すぎ）
    "STSlow",         # ST遅延（展示が平均より遅い）
    "Wt",             # 体重差
    "Class",          # [v7.5対応] 級別補正（旧"Vol"）
    "CBias",          # コースバイアス
    "Wind",           # 風 × 潮汐 × 場（Wind×Tideにもマッチ）
]

# EWMA の減衰係数（新しいデータほど重み大）
EWMA_ALPHA = 0.05  # 直近20レース相当の半減期
MIN_SAMPLES = 10    # 最低サンプル数（これ未満は信頼度1.0固定）
DEFAULT_RELIABILITY = 1.0


class AlphaReliabilityTracker:
    """
    各αソースの予測精度を追跡し、信頼度を更新する。

    概念:
      - αが「1号艇有利」と判断 → 実際に1号艇が1着 → 的中（αは正しかった）
      - αが「3号艇有利」と判断 → 実際に5号艇が1着 → 不的中（αは間違っていた）
      - 各αの的中率を追跡し、平均より高いαの重みを増やす

    state構造:
    {
      "alpha_name": {
        "total_fires": int,
        "hits": int,
        "ewma_hit_rate": float,  # EWMA的中率
        "reliability": float,     # 0.5~1.5 の範囲で調整
        "last_updated": str,
      }
    }
    """

    def __init__(self, state_file: str = STATE_FILE):
        self.state_file = state_file
        self.state = self._load_state()

    def _load_state(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {src: self._default_entry() for src in ALPHA_SOURCES}

    def _default_entry(self) -> dict:
        return {
            "total_fires": 0,
            "hits": 0,
            "ewma_hit_rate": 0.5,
            "reliability": DEFAULT_RELIABILITY,
            "last_updated": "",
        }

    def _save_state(self):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def get_reliability(self, alpha_source: str) -> float:
        """αソースの現在の信頼度を取得"""
        entry = self.state.get(alpha_source, self._default_entry())
        if entry["total_fires"] < MIN_SAMPLES:
            return DEFAULT_RELIABILITY
        return entry["reliability"]

    def get_all_reliabilities(self) -> Dict[str, float]:
        """全αの信頼度を一括取得"""
        return {
            src: self.get_reliability(src)
            for src in ALPHA_SOURCES
        }

    def update(self, analysis_result: dict, actual_result: dict):
        """
        レース結果を使って各αの信頼度を更新。

        analysis_result: rtpt_engine.analyze() の戻り値
        actual_result: {"1st": int, "2nd": int, "3rd": int}
        """
        if not analysis_result or not actual_result:
            return

        boats = analysis_result.get("boats", [])
        winner = actual_result.get("1st", 0)
        top3 = {actual_result.get("1st", 0),
                actual_result.get("2nd", 0),
                actual_result.get("3rd", 0)}

        for boat_info in boats:
            bn = boat_info["boat"]
            reasons = boat_info.get("reasons", [])

            for reason in reasons:
                # reasonからαソース名を抽出
                alpha_src = self._extract_source(reason)
                if not alpha_src:
                    continue

                if alpha_src not in self.state:
                    self.state[alpha_src] = self._default_entry()

                entry = self.state[alpha_src]

                # αが「この艇は有利」と言っていたか「不利」と言っていたか
                is_boost = self._is_boost(reason)

                # 的中判定
                if is_boost:
                    # ブーストα → この艇が3着以内なら的中
                    hit = bn in top3
                else:
                    # ペナルティα → この艇が3着以外なら的中
                    hit = bn not in top3

                # EWMA更新
                entry["total_fires"] += 1
                if hit:
                    entry["hits"] += 1

                old_rate = entry["ewma_hit_rate"]
                new_obs = 1.0 if hit else 0.0
                entry["ewma_hit_rate"] = (1 - EWMA_ALPHA) * old_rate + EWMA_ALPHA * new_obs

                # 信頼度更新
                if entry["total_fires"] >= MIN_SAMPLES:
                    # EWMA的中率が0.5（ランダム）より高ければ信頼度UP
                    # 0.5より低ければ信頼度DOWN
                    rate = entry["ewma_hit_rate"]
                    # 0.5をベースラインとして、乖離を信頼度に変換
                    # rate=0.7 → reliability=1.4, rate=0.3 → reliability=0.6
                    entry["reliability"] = max(0.5, min(1.5, rate * 2.0))

                entry["last_updated"] = datetime.now().isoformat()

        self._save_state()

    def _extract_source(self, reason: str) -> Optional[str]:
        """reason文字列からαソース名を抽出"""
        for src in ALPHA_SOURCES:
            if reason.startswith(src) or src + "(" in reason:
                return src
        return None

    def _is_boost(self, reason: str) -> bool:
        """このαが「有利」を示しているか（ブースト）を判定"""
        import re
        # v7.5: 加法ベースのα表記
        # 通常形式: "→+0.120" or "→-0.035"
        # Wind×Tide形式: "→C1-0.30" or "→C1+0.25"（コース番号が挟まる）
        add_match = re.search(r'→(?:C\d+)?([+-][\d.]+)', reason)
        if add_match:
            return float(add_match.group(1)) > 0
        # v7.4以前: 乗法ベースの表記 "×1.30"
        mult_match = re.search(r'×([\d.]+)', reason)
        if mult_match:
            return float(mult_match.group(1)) > 1.0
        return True  # デフォルトはブースト

    def get_report(self) -> dict:
        """全αソースの信頼度レポート"""
        report = {}
        for src in ALPHA_SOURCES:
            entry = self.state.get(src, self._default_entry())
            report[src] = {
                "fires": entry["total_fires"],
                "hits": entry["hits"],
                "hit_rate": entry["hits"] / max(entry["total_fires"], 1) * 100,
                "ewma_rate": round(entry["ewma_hit_rate"] * 100, 1),
                "reliability": round(entry["reliability"], 3),
                "status": (
                    "⏳ データ不足" if entry["total_fires"] < MIN_SAMPLES else
                    "🟢 効果あり" if entry["reliability"] >= 1.1 else
                    "🟡 中立" if entry["reliability"] >= 0.9 else
                    "🔴 要見直し"
                ),
            }
        return report

    def apply_to_alpha(self, alpha_dict: Dict[int, float],
                       reasons: Dict[int, List[str]]) -> Dict[int, float]:
        """
        信頼度をα値に適用する。
        rtpt_engine.py から呼ばれる。

        αの各成分に対応する信頼度を乗算することで、
        効いていないαを自動的に減衰させる。
        """
        adjusted = dict(alpha_dict)

        for bn in range(1, 7):
            reasons_bn = reasons.get(bn, [])
            if not reasons_bn:
                continue

            # 各reasonの信頼度を収集し、平均信頼度を1回だけ適用する。
            # 連鎖乗算（複数reasonで repeated scaling）を防ぐ。
            reliabilities = []
            for reason in reasons_bn:
                src = self._extract_source(reason)
                if src:
                    reliabilities.append(self.get_reliability(src))

            if not reliabilities:
                continue

            avg_reliability = sum(reliabilities) / len(reliabilities)
            if abs(avg_reliability - 1.0) > 0.01:
                # αの「1.0からの乖離」に平均信頼度を適用
                # avg_reliability=1.5なら乖離を50%増幅
                # avg_reliability=0.5なら乖離を50%縮小
                deviation = adjusted[bn] - 1.0
                adjusted[bn] = 1.0 + deviation * avg_reliability

        return adjusted


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    import sys

    tracker = AlphaReliabilityTracker()

    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report = tracker.get_report()
        print("=== Alpha Reliability Report ===")
        for src, data in report.items():
            print(f"  {src:20s} | 発火{data['fires']:4d}回 | "
                  f"的中{data['hit_rate']:.1f}% | "
                  f"EWMA{data['ewma_rate']:.1f}% | "
                  f"信頼度{data['reliability']:.3f} | {data['status']}")
    elif len(sys.argv) > 1 and sys.argv[1] == "reset":
        tracker.state = {src: tracker._default_entry() for src in ALPHA_SOURCES}
        tracker._save_state()
        print("リセット完了")
    else:
        print("Usage:")
        print("  python alpha_adapter.py report  — 信頼度レポート表示")
        print("  python alpha_adapter.py reset   — 信頼度リセット")
