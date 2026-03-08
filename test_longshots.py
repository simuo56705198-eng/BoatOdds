import os
import glob
import json
from rtpt_engine import analyze

def main():
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest")
    # Test on the last full day of data
    files = glob.glob(os.path.join(base_dir, "20260307", "*.json"))
    
    total_races = len(files)
    print(f"Testing the new Longshot Engine on {total_races} races (20260307).")
    
    total_inv = 0
    total_pay = 0
    
    huge_hits = []
    
    for fpath in files:
        with open(fpath, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        result = analyze(data, bankroll=10000)
        targets = result.get("targets", [])
        
        if not targets:
            continue
            
        a1, a2, a3 = data.get("result", {}).get("1st"), data.get("result", {}).get("2nd"), data.get("result", {}).get("3rd")
        if not (a1 and a2 and a3):
            continue
            
        for t in targets:
            btype = t["type"]
            combo = str(t["combo"])
            odds = float(t["odds"])
            rec_yen = t["recommended_yen"]
            
            if rec_yen < 100: continue
            
            hit = False
            if btype == "2連単": hit = (a1 == int(combo.split('-')[0]) and a2 == int(combo.split('-')[1]))
            elif btype == "2連複": hit = ({a1, a2} == set(map(int, combo.split('='))))
            elif btype == "拡連複": hit = (int(combo.split('=')[0]) in {a1,a2,a3} and int(combo.split('=')[1]) in {a1,a2,a3})
            elif btype == "3連単": hit = (a1 == int(combo.split('-')[0]) and a2 == int(combo.split('-')[1]) and a3 == int(combo.split('-')[2]))
            elif btype == "3連複": hit = ({a1, a2, a3} == set(map(int, combo.split('='))))
            elif btype == "複勝": hit = (int(combo) in {a1, a2})
            elif btype == "単勝": hit = (a1 == int(combo))
            
            total_inv += rec_yen
            if hit:
                payout = int(rec_yen * odds)
                total_pay += payout
                if odds >= 30.0:  # Define "大荒れ" as > 30x
                    stadium = data["predata"]["metadata"]["stadium"]
                    rno = data["predata"]["metadata"]["race_number"]
                    huge_hits.append(f"{stadium} {rno}: {btype} {combo} - {odds}倍 (投資 {rec_yen}円 -> 回収 {payout}円)")

    print("\n" + "="*50)
    print("🎯 Longshot Update Test Result")
    print("="*50)
    print(f"Total Investment: {total_inv:,} 円")
    print(f"Total Payout    : {total_pay:,} 円")
    print(f"Return on Invest: {(total_pay/total_inv)*100:.1f}%")
    print(f"\nCaught {len(huge_hits)} MASSIVE Upsets (>30x Odds):")
    for h in huge_hits:
        print("  " + h)

if __name__ == "__main__":
    main()
