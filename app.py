if execute:
    target_jcd = JCD_MAP[input_jcd]
    race_data = {
        "metadata": {"date": target_date, "stadium": input_jcd, "race_number": f"{target_rno}R"},
        "environment": {}, "racelist": {str(i): {} for i in range(1, 7)},
        "odds": {"3連単": {}, "3連複": {}, "2連単": {}, "2連複": {}, "拡連複": {}, "単勝": {}, "複勝": {}}
    }

    with st.status("同期中...", expanded=True) as status:
        get_racelist(target_jcd, target_rno, target_date, race_data)
        get_beforeinfo(target_jcd, target_rno, target_date, race_data)
        fetch_all_odds(target_jcd, target_rno, target_date, race_data)
        status.update(label="解析準備完了", state="complete")

    # --- JSONダウンロードボタンを最上部に移動 ---
    json_export = json.dumps(race_data, ensure_ascii=False, indent=2)
    st.download_button(
        label="📥 AI解析用JSONをダウンロード",
        data=json_export,
        file_name=f"{target_date}_{input_jcd}_{target_rno}R_AIデータ.json",
        mime="application/json"
    )

    # --- 物理レポート ---
    st.header("🛡️ Physics Analysis Report")
    
    b1 = race_data["racelist"]["1"]
    if b1.get('exhibition_time', 0) > 0:
        ex_times = [race_data["racelist"][str(i)].get('exhibition_time', 0) for i in range(1,7) if race_data["racelist"][str(i)].get('exhibition_time', 0) > 0]
        if ex_times and b1.get('exhibition_time', 0) == max(ex_times):
            st.error("📉 Conditional Renormalization: 1号艇に物理的欠陥を探知。確率空間を再計算してください。")

    cols = st.columns(6)
    for i in range(1, 7):
        b = race_data["racelist"][str(i)]
        with cols[i-1]:
            ex_time = b.get('exhibition_time', 0)
            st.metric(f"{i}号艇", f"{ex_time}s")
            
            # --- 展示タイム0.0のアラート ---
            if ex_time == 0 or ex_time == 0.0:
                st.warning("⚠️ 計測不能")
            
            st.write(f"展示進入: {b.get('start_course', '-')}コース")
            st.write(f"展示ST: {b.get('start_exhibition_st', '-')}")
            st.caption(f"{b.get('name')} ({b.get('class', '-')}) / {b.get('weight', 0.0)}kg")
            
            if i < 6:
                next_b = race_data["racelist"][str(i+1)]
                if abs(b.get('avg_st', 0) - next_b.get('avg_st', 0)) >= 0.08:
                    st.warning("⚠️ Void")
            
            if i > 1:
                prev_b = race_data["racelist"][str(i-1)]
                diff = prev_b.get('exhibition_time', 0) - b.get('exhibition_time', 0)
                if diff >= 0.07: st.error("🌊 Wake Rejection")
                elif diff <= 0.06 and b.get('class') == 'A1': st.success("⚡ Skill Offset")

    st.subheader("Raw AI Data")
    st.json(race_data)
