import streamlit as st
import pandas as pd
import os

st.set_page_config(page_title="连板晋级率（分组+T日）", page_icon="📈", layout="wide")
st.title("📈 A股主板连板股·滚动统计（2~5连板）")
st.caption("排除ST/*ST | 包含一字板/换手板区分 | 起始：2026-04-01 | 可切换观察日")

DATA_FILE = "rolling_cache.parquet"
if not os.path.exists(DATA_FILE):
    st.error("❌ 找不到数据文件，请先运行 lianban_rolling.py")
    st.stop()

df = pd.read_parquet(DATA_FILE)
df['trade_date'] = pd.to_datetime(df['trade_date'].astype(str))
df = df.sort_values('trade_date')

all_dates = sorted(df['trade_date'].unique())
min_date = all_dates[0]
max_date = all_dates[-1]

st.sidebar.header("⚙️ 筛选条件")
end_date = st.sidebar.date_input("截止日期", value=max_date, min_value=min_date, max_value=max_date)
start_date = pd.Timestamp('2026-04-01')

available_lianban = sorted(df['连板数'].unique())
selected_lianban = st.sidebar.multiselect("连板数", available_lianban, default=available_lianban)

# 观察日选项：T日(0) + T+1/T+2
skip_opts = [0] + sorted([s for s in df['skip_days'].unique() if s != 0])
selected_skip = st.sidebar.selectbox("观察日", skip_opts, format_func=lambda x: "T日" if x==0 else f"T+{x}")

mask = (df['trade_date'] >= pd.Timestamp(start_date)) & (df['trade_date'] <= pd.Timestamp(end_date))
mask = mask & (df['连板数'].isin(selected_lianban)) & (df['skip_days'] == selected_skip)
window_df = df.loc[mask].copy()

if window_df.empty:
    st.warning("无符合条件的数据")
    st.stop()

trade_days = window_df['trade_date'].nunique()
st.sidebar.metric("窗口起始", str(start_date.date()))
st.sidebar.metric("窗口截止", str(end_date))
st.sidebar.metric("交易日数", trade_days)

st.subheader(f"📊 各连板梯队滚动统计 (观察日: {'T日' if selected_skip==0 else f'T+{selected_skip}'})")

if selected_skip == 0:
    # T日视图：展示股票名单及一字板、换手率、最高最低
    for lb in selected_lianban:
        lb_df = window_df[window_df['连板数'] == lb]
        st.markdown(f"### {lb} 连板")
        show_cols = ['ts_code', 'name', 'is_yiziban', 'turnover_rate', 'high', 'low']
        show_df = lb_df[show_cols].copy()
        show_df.columns = ['代码', '名称', '一字板', '换手率(%)', '最高价', '最低价']
        show_df['一字板'] = show_df['一字板'].apply(lambda x: '一字板' if x==True else ('换手板' if x==False else '未知'))
        show_df['换手率(%)'] = show_df['换手率(%)'].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "-")
        show_df['最高价'] = show_df['最高价'].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "-")
        show_df['最低价'] = show_df['最低价'].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "-")
        st.dataframe(show_df, use_container_width=True, hide_index=True)
        st.caption(f"共 {len(show_df)} 只")
        st.divider()
else:
    # T+1 / T+2 视图：KPI 卡片 + 每日明细（含一字板等补充信息）
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

    # 每日明细表格（含一字板信息）
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
                '股票': '、'.join(day_data['name'].dropna().unique()) if 'name' in day_data.columns else ''
            })
    daily_df = pd.DataFrame(daily_stats).sort_values(['交易日', '连板数'], ascending=[False, True])
    st.dataframe(daily_df, use_container_width=True, hide_index=True)

    st.subheader("📈 上涨比例趋势")
    chart_data = daily_df.pivot_table(index='交易日', columns='连板数', values='上涨比例%', aggfunc='mean')
    st.line_chart(chart_data)

st.caption(f"数据更新至：{max_date.strftime('%Y-%m-%d')}")