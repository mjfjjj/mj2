import streamlit as st
import pandas as pd
import os

st.set_page_config(page_title="连板复盘与统计", page_icon="📈", layout="wide")

DATA_FILE = "rolling_cache.parquet"

@st.cache_data
def load_data():
    if not os.path.exists(DATA_FILE):
        st.error("❌ 找不到数据文件，请先运行 lianban_rolling.py")
        st.stop()
    df = pd.read_parquet(DATA_FILE)
    df['trade_date'] = pd.to_datetime(df['trade_date'].astype(str))
    return df.sort_values('trade_date')

df = load_data()
all_dates = sorted(df['trade_date'].unique())
min_date = all_dates[0]
max_date = all_dates[-1]

tab1, tab2 = st.tabs(["📋 今日连板", "📈 滚动统计"])

# ==================== 今日连板 ====================
with tab1:
    st.title("📋 最新交易日连板梯队")
    st.caption("自动获取最新一个交易日数据 | 仅主板 | 排除ST/*ST")

    latest_date = max_date
    t0_data = df[(df['trade_date'] == latest_date) & (df['skip_days'] == 0)]

    if t0_data.empty:
        st.warning(f"暂无 {latest_date.strftime('%Y-%m-%d')} 的连板数据，请等待数据采集完成。")
    else:
        st.success(f"数据日期：{latest_date.strftime('%Y-%m-%d')}")

        for lb in sorted(t0_data['连板数'].unique()):
            lb_df = t0_data[t0_data['连板数'] == lb].copy()
            st.subheader(f"🔥 {lb}连板（{len(lb_df)}只）")
            if lb_df.empty:
                st.write("暂无")
                continue

            display = lb_df[['ts_code', 'name']].copy()
            display.columns = ['代码', '名称']
            display['代码'] = display['代码'].astype(str)

            st.dataframe(display, use_container_width=True, hide_index=True)
            st.divider()

# ==================== 滚动统计 ====================
with tab2:
    st.title("📈 A股主板连板股·滚动统计")
    st.caption("排除ST/*ST | 起始：2026-04-01 | 可切换观察日（T+1/T+2）")

    st.sidebar.header("⚙️ 滚动统计筛选")
    end_date = st.sidebar.date_input("截止日期", value=max_date, min_value=min_date, max_value=max_date)
    start_date = pd.Timestamp('2026-04-01')

    available_lianban = sorted(df['连板数'].unique())
    selected_lianban = st.sidebar.multiselect("连板数", available_lianban, default=available_lianban)

    skip_opts = sorted([s for s in df['skip_days'].unique() if s != 0])
    selected_skip = st.sidebar.selectbox("观察日", skip_opts, format_func=lambda x: f"T+{x}")

    mask = (df['trade_date'] >= pd.Timestamp(start_date)) & (df['trade_date'] <= pd.Timestamp(end_date))
    mask = mask & (df['连板数'].isin(selected_lianban)) & (df['skip_days'] == selected_skip)
    window_df = df.loc[mask]

    if window_df.empty:
        st.warning("无符合条件的数据")
    else:
        trade_days = window_df['trade_date'].nunique()
        st.sidebar.metric("窗口起始", str(start_date.date()))
        st.sidebar.metric("窗口截止", str(end_date))
        st.sidebar.metric("交易日数", trade_days)

        st.subheader(f"📊 各连板梯队滚动统计 (T+{selected_skip})")
        for lb in selected_lianban:
            lb_df = window_df[window_df['连板数'] == lb]
            total = len(lb_df)
            up_df = lb_df[lb_df['pct_chg'] > 0]
            down_df = lb_df[lb_df['pct_chg'] < 0]
            up_count = len(up_df)
            down_count = len(down_df)
            up_ratio = (up_count / total * 100) if total > 0 else 0
            up_avg = up_df['pct_chg'].mean() if up_count > 0 else 0
            down_avg = down_df['pct_chg'].mean() if down_count > 0 else 0
            st.markdown(f"### {lb} 连板")
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("样本数", total)
            col2.metric("上涨", f"{up_count} 只")
            col3.metric("上涨比例", f"{up_ratio:.2f}%")
            col4.metric("上涨均幅", f"{up_avg:.2f}%")
            col1, col2 = st.columns(2)
            col1.metric("下跌均幅", f"{down_avg:.2f}%")
            st.divider()

        # 每日明细
        st.subheader("📅 每日明细")
        daily_stats = []
        for date in sorted(window_df['trade_date'].unique()):
            for lb in selected_lianban:
                day_data = window_df[(window_df['trade_date'] == date) & (window_df['连板数'] == lb)]
                if day_data.empty:
                    continue
                day_up = len(day_data[day_data['pct_chg'] > 0])
                day_total = len(day_data)
                daily_stats.append({
                    '交易日': date.strftime('%Y-%m-%d'),
                    '连板数': lb,
                    '样本': day_total,
                    '上涨': day_up,
                    '上涨比例%': round(day_up / day_total * 100, 2) if day_total > 0 else 0,
                    '上涨均幅%': round(day_data[day_data['pct_chg'] > 0]['pct_chg'].mean(), 2) if day_up > 0 else 0,
                    '下跌均幅%': round(day_data[day_data['pct_chg'] < 0]['pct_chg'].mean(), 2) if len(day_data[day_data['pct_chg'] < 0]) > 0 else 0,
                    '股票': '、'.join(day_data['name'].dropna().unique())
                })
        daily_df = pd.DataFrame(daily_stats).sort_values(['交易日', '连板数'], ascending=[False, True])
        st.dataframe(daily_df, use_container_width=True, hide_index=True)

    st.caption(f"数据更新至：{max_date.strftime('%Y-%m-%d')}")
