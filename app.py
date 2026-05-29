"""
连板复盘与滚动统计 — 手机看板（仅 T+1）
依赖：rolling_cache.parquet（由修正后的采集脚本生成）
"""

import streamlit as st
import pandas as pd
import os
import akshare as ak
from datetime import datetime

st.set_page_config(page_title="连板复盘与统计", page_icon="📈", layout="wide")

DATA_FILE = "rolling_cache.parquet"

# ==================== 数据加载 ====================
@st.cache_data(ttl=3600)  # 缓存1小时
def load_rolling_data():
    if not os.path.exists(DATA_FILE):
        st.error("❌ 找不到 rolling_cache.parquet，请先运行 lianban_rolling.py")
        st.stop()
    df = pd.read_parquet(DATA_FILE)
    df['trade_date'] = pd.to_datetime(df['trade_date'].astype(str))
    return df.sort_values('trade_date')

@st.cache_data(ttl=300)  # 5分钟实时更新
def get_today_lianban():
    """实时获取最新交易日的连板梯队（仅主板，非ST）"""
    today = datetime.today().strftime('%Y%m%d')
    # 尝试获取今日数据，若今日非交易日则自动回退到最近交易日
    for offset in range(5):
        date = (datetime.today() - pd.Timedelta(days=offset)).strftime('%Y%m%d')
        try:
            df = ak.stock_zt_pool_em(date=date)
            if df is not None and not df.empty:
                # 排除ST、北交所、创业板、科创板（仅主板）
                df = df[~df['名称'].str.contains('ST', case=False, na=False)]
                df = df[df['代码'].str.startswith(('60', '00'))]
                # 保留必要字段
                df = df[['代码', '名称', '连板数']].copy()
                df['连板数'] = pd.to_numeric(df['连板数'], errors='coerce')
                df = df.dropna(subset=['连板数'])
                df['连板数'] = df['连板数'].astype(int)
                return date, df
        except Exception:
            continue
    return None, pd.DataFrame()

# ==================== 主界面 ====================
df = load_rolling_data()
all_dates = sorted(df['trade_date'].unique())
min_date, max_date = all_dates[0], all_dates[-1]

tab1, tab2 = st.tabs(["📋 今日涨停板", "📈 滚动统计（T+1）"])

# ==================== 标签1：今日涨停板（实时） ====================
with tab1:
    st.title("📋 最新交易日涨停板梯队")
    st.caption("仅主板 | 排除ST/*ST | 数据实时获取 (AkShare)")

    trade_date, lianban_df = get_today_lianban()
    if trade_date is None or lianban_df.empty:
        st.warning("未获取到涨停板数据（可能今日非交易日或数据源问题）")
    else:
        st.success(f"📅 数据日期：{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}")
        for lb in sorted(lianban_df['连板数'].unique()):
            sub = lianban_df[lianban_df['连板数'] == lb]
            st.subheader(f"🔥 {lb}连板（{len(sub)}只）")
            show = sub[['代码', '名称']].copy()
            show.columns = ['代码', '名称']
            st.dataframe(show, use_container_width=True, hide_index=True)
            st.divider()

# ==================== 标签2：滚动统计 ====================
with tab2:
    st.title("📈 A股主板连板股 · 滚动统计（T+1）")
    st.caption("起始：缓存中最早交易日 | 每新增一个交易日自动剔除最早交易日 | 仅统计 T+1 表现")

    st.sidebar.header("⚙️ 筛选条件")
    # 使用缓存中的日期范围
    start_dt = st.sidebar.date_input("起始日期", value=min_date, min_value=min_date, max_value=max_date)
    end_dt = st.sidebar.date_input("截止日期", value=max_date, min_value=min_date, max_value=max_date)

    all_lb = sorted(df['连板数'].unique())
    default_lb = [lb for lb in all_lb if lb != 1]  # 默认排除首板，可自行调整
    sel_lb = st.sidebar.multiselect("连板数", all_lb, default=default_lb)

    # 筛选数据：仅 T+1，且日期在范围内
    mask = (df['trade_date'] >= pd.Timestamp(start_dt)) & (df['trade_date'] <= pd.Timestamp(end_dt))
    mask &= df['连板数'].isin(sel_lb) & (df['skip_days'] == 1)
    win = df.loc[mask]

    if win.empty:
        st.warning("无符合条件的数据，请检查日期范围或连板数选择")
    else:
        td_cnt = win['trade_date'].nunique()
        st.sidebar.metric("窗口起始", start_dt.strftime('%Y-%m-%d'))
        st.sidebar.metric("窗口截止", end_dt.strftime('%Y-%m-%d'))
        st.sidebar.metric("交易日数", td_cnt)

        # KPI 卡片
        st.subheader(f"📊 各连板梯队滚动统计")
        for lb in sel_lb:
            lb_df = win[win['连板数'] == lb]
            total = len(lb_df)
            if total == 0:
                continue
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
        if not daily_df.empty:
            st.subheader("📈 各连板梯队上涨比例折线图")
            chart_data = daily_df.pivot_table(index='交易日', columns='连板数', values='上涨比例%', aggfunc='mean')
            # 按数值排序列
            cols = sorted([c for c in chart_data.columns if isinstance(c, int)])
            chart_data = chart_data[cols]
            st.line_chart(chart_data)

    st.caption(f"数据更新至：{max_date.strftime('%Y-%m-%d')}")
