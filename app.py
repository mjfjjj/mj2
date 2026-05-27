"""
连板复盘与滚动统计 — 手机看板 (Streamlit)
"""
import streamlit as st
import pandas as pd
import os

st.set_page_config(page_title="连板复盘与统计", page_icon="📈", layout="wide")

DATA_FILE = "rolling_cache.parquet"

@st.cache_data
def load_data():
    if not os.path.exists(DATA_FILE):
        st.error("❌ 找不到 rolling_cache.parquet，请先运行 lianban_rolling.py")
        st.stop()
    df = pd.read_parquet(DATA_FILE)
    df['trade_date'] = pd.to_datetime(df['trade_date'].astype(str))
    return df.sort_values('trade_date')

df = load_data()
all_dates = sorted(df['trade_date'].unique())
min_date, max_date = all_dates[0], all_dates[-1]

tab1, tab2 = st.tabs(["📋 今日涨停板", "📈 滚动统计"])

# ==================== 标签1：今日涨停板 ====================
with tab1:
    st.title("📋 最新交易日涨停板梯队")
    st.caption("仅主板 | 排除ST/*ST | 数据来源: AkShare + Tushare")

    latest = max_date
    t0 = df[(df['trade_date'] == latest) & (df['skip_days'] == 0)]

    if t0.empty:
        st.warning(f"暂无 {latest.strftime('%Y-%m-%d')} 的涨停数据，请等待收盘后数据采集完成。")
    else:
        st.success(f"📅 数据日期：{latest.strftime('%Y-%m-%d')}")
        for lb in sorted(t0['连板数'].unique()):
            sub = t0[t0['连板数'] == lb]
            st.subheader(f"🔥 {lb}连板（{len(sub)}只）")
            show = sub[['ts_code', 'name']].copy()
            show.columns = ['代码', '名称']
            show['代码'] = show['代码'].astype(str)
            st.dataframe(show, use_container_width=True, hide_index=True)
            st.divider()

# ==================== 标签2：滚动统计 ====================
with tab2:
    st.title("📈 A股主板连板股 · 滚动统计")
    st.caption("起始：2026-04-01 | 每新增一个交易日自动剔除最早交易日 | T+1 / T+2 分开统计")

    st.sidebar.header("⚙️ 筛选条件")
    end_dt = st.sidebar.date_input("截止日期", value=max_date, min_value=min_date, max_value=max_date)
    start_dt = pd.Timestamp('2026-04-01')

    av_lb = sorted(df['连板数'].unique())
    default_lb = [lb for lb in av_lb if lb != 1]   # 默认不选首板
    sel_lb = st.sidebar.multiselect("连板数", av_lb, default=default_lb)

    av_sk = sorted([s for s in df['skip_days'].unique() if s > 0])
    sel_sk = st.sidebar.selectbox("观察日", av_sk, format_func=lambda x: f"T+{x}")

    mask = (df['trade_date'] >= start_dt) & (df['trade_date'] <= pd.Timestamp(end_dt))
    mask &= df['连板数'].isin(sel_lb) & (df['skip_days'] == sel_sk)
    win = df.loc[mask]

    if win.empty:
        st.warning("无符合条件的数据")
    else:
        td_cnt = win['trade_date'].nunique()
        st.sidebar.metric("窗口起始", str(start_dt.date()))
        st.sidebar.metric("窗口截止", str(end_dt))
        st.sidebar.metric("交易日数", td_cnt)

        # KPI 卡片
        st.subheader(f"📊 各连板梯队滚动统计 (T+{sel_sk})")
        for lb in sel_lb:
            lb_df = win[win['连板数'] == lb]
            total = len(lb_df)
            up_df = lb_df[lb_df['pct_chg'] > 0]
            down_df = lb_df[lb_df['pct_chg'] < 0]
            up_c = len(up_df)
            down_c = len(down_df)
            up_r = (up_c / total * 100) if total > 0 else 0
            up_avg = round(up_df['pct_chg'].mean(), 2) if up_c > 0 else 0
            down_avg = round(down_df['pct_chg'].mean(), 2) if down_c > 0 else 0
            mean_all = round(lb_df['pct_chg'].mean(), 2)

            st.markdown(f"### {lb} 连板")
            c1, c2 = st.columns(2)
            c1.metric("样本数", total)
            c2.metric("上涨", f"{up_c}只 ({up_r:.1f}%)")
            c3, c4, c5 = st.columns(3)
            c3.metric("上涨均幅", f"{up_avg}%")
            c4.metric("下跌均幅", f"{down_avg}%")
            c5.metric("平均涨跌幅", f"{mean_all}%")
            st.divider()

        # 每日明细
        st.subheader("📅 每日明细")
        daily = []
        for dt in sorted(win['trade_date'].unique()):
            for lb in sel_lb:
                dd = win[(win['trade_date'] == dt) & (win['连板数'] == lb)]
                if dd.empty:
                    continue
                up_d = len(dd[dd['pct_chg'] > 0])
                tot = len(dd)
                daily.append({
                    '交易日': dt.strftime('%Y-%m-%d'),
                    '连板数': lb,
                    '样本': tot,
                    '上涨': up_d,
                    '上涨比例%': round(up_d / tot * 100, 2) if tot > 0 else 0,
                    '上涨均幅%': round(dd[dd['pct_chg'] > 0]['pct_chg'].mean(), 2) if up_d > 0 else 0,
                    '下跌均幅%': round(dd[dd['pct_chg'] < 0]['pct_chg'].mean(), 2) if len(dd[dd['pct_chg'] < 0]) > 0 else 0,
                    '平均涨跌幅%': round(dd['pct_chg'].mean(), 2),
                    '股票': '、'.join(dd['name'].dropna().unique()),
                })
        daily_df = pd.DataFrame(daily).sort_values(['交易日', '连板数'], ascending=[False, True])
        st.dataframe(daily_df, use_container_width=True, hide_index=True)

        # 折线图
        st.subheader("📈 各连板梯队上涨比例折线图")
        chart_data = daily_df.pivot_table(index='交易日', columns='连板数', values='上涨比例%', aggfunc='mean')
        ordered = [c for c in [2, 3, 4, 5] if c in chart_data.columns]
        chart_data = chart_data[ordered]
        st.line_chart(chart_data)

    st.caption(f"数据更新至：{max_date.strftime('%Y-%m-%d')}")
