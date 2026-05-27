"""
连板股数据采集脚本（精简版 · 无最大涨跌幅）
功能：
  1. 获取首板~五连板股票（仅主板，排除ST/*ST）
  2. 统计 T+1 / T+2 涨跌幅、上涨比例、上涨均幅、下跌均幅、平均涨跌幅
  3. 滚动窗口：从 2026-04-01 起，每加入新交易日剔除最早交易日
数据源：AkShare（主）+ Tushare（备用）
"""

import akshare as ak
import tushare as ts
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
import time
import os

warnings.filterwarnings('ignore')

# ==================== 配置区 ====================
TUSHARE_TOKEN = '014ba2364f885e96f637e04d79ad0e2f180aeaa3dbae860640cc63ca'
START_DATE = '20260401'
LIANBAN_LIST = [1, 2, 3, 4, 5]
SKIP_DAYS_LIST = [1, 2]
RETRY_COUNT = 3
SLEEP_BETWEEN_STOCKS = 0.05
# =================================================

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

def get_trade_cal(start: str, end: str):
    try:
        df = pro.trade_cal(exchange='SSE', start_date=start, end_date=end, is_open='1')
        if df is not None and not df.empty:
            return sorted(df['cal_date'].unique())
    except Exception:
        pass
    try:
        df = ak.tool_trade_date_hist_sina()
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        mask = (df['trade_date'] >= pd.to_datetime(start)) & (df['trade_date'] <= pd.to_datetime(end))
        return sorted(df.loc[mask, 'trade_date'].dt.strftime('%Y%m%d').unique())
    except Exception:
        pass
    print("⚠️ 无法获取交易日历，使用降级方案（周一至周五）。")
    dates = []
    cur = datetime.strptime(start, '%Y%m%d')
    end_dt = datetime.strptime(end, '%Y%m%d')
    while cur <= end_dt:
        if cur.weekday() < 5:
            dates.append(cur.strftime('%Y%m%d'))
        cur += timedelta(days=1)
    return dates

def get_next_trade_date(base_date: str, skip_days: int):
    try:
        cal = pro.trade_cal(exchange='SSE', start_date=base_date,
                            end_date=(datetime.strptime(base_date, '%Y%m%d') + timedelta(days=15)).strftime('%Y%m%d'),
                            is_open='1')
        if cal is None or cal.empty:
            raise ValueError()
        dates = sorted(cal['cal_date'].unique())
    except Exception:
        try:
            cal = ak.tool_trade_date_hist_sina()
            cal['trade_date'] = pd.to_datetime(cal['trade_date'])
            dates = sorted(cal['trade_date'].dt.strftime('%Y%m%d').unique())
        except Exception:
            return None
    try:
        idx = dates.index(base_date)
    except ValueError:
        return None
    if idx + skip_days >= len(dates):
        return None
    target = dates[idx + skip_days]
    if target >= datetime.now().strftime('%Y%m%d'):
        return None
    return target

def get_lianban_stocks(date_str: str, lianban_num: int) -> pd.DataFrame:
    stocks = pd.DataFrame()
    try:
        raw = ak.stock_zt_pool_em(date=date_str)
        if raw is not None and not raw.empty:
            raw['连板数'] = pd.to_numeric(raw['连板数'], errors='coerce')
            sub = raw[raw['连板数'] == lianban_num].copy()
            if not sub.empty:
                stocks = pd.DataFrame({
                    'ts_code': sub['代码'].astype(str),
                    'name': sub['名称'].astype(str),
                })
    except Exception:
        pass
    if stocks.empty:
        try:
            df = pro.limit_step(trade_date=date_str, nums=str(lianban_num))
            if df is not None and not df.empty:
                stocks = df[['ts_code', 'name']].copy()
        except Exception:
            pass
    if stocks.empty:
        return stocks
    if 'name' in stocks.columns:
        stocks = stocks[~stocks['name'].str.contains('ST', case=False, na=False)]
    def is_main(code):
        c = str(code).replace('.SZ', '').replace('.SH', '').replace('.BJ', '')
        return c.startswith(('60', '00'))
    stocks = stocks[stocks['ts_code'].apply(is_main)]
    return stocks

def get_future_pct(codes: list, base_date: str, skip_days: int):
    target_date = get_next_trade_date(base_date, skip_days)
    if target_date is None:
        return None
    try:
        ts_codes = ','.join(codes)
        daily = pro.daily(ts_code=ts_codes, trade_date=target_date, fields='ts_code,pct_chg')
        if daily is not None and not daily.empty:
            daily['target_date'] = target_date
            return daily
    except Exception:
        pass
    all_pct = []
    for code in codes:
        pure = code.replace('.SZ', '').replace('.SH', '').replace('.BJ', '')
        for attempt in range(RETRY_COUNT):
            try:
                hist = ak.stock_zh_a_hist(symbol=pure, period='daily',
                                          start_date=target_date, end_date=target_date,
                                          adjust='qfq')
                if hist is not None and not hist.empty:
                    pct = float(hist.iloc[0]['涨跌幅'])
                    all_pct.append({'ts_code': code, 'pct_chg': pct})
                    break
                time.sleep(SLEEP_BETWEEN_STOCKS)
            except Exception:
                if attempt < RETRY_COUNT - 1:
                    time.sleep(0.5)
                else:
                    print(f"   ⚠️ {code} T+{skip_days} 获取失败")
    if all_pct:
        res = pd.DataFrame(all_pct)
        res['target_date'] = target_date
        return res
    return None

def calc_stats(pct_series: pd.Series):
    total = len(pct_series)
    if total == 0:
        return None
    up = pct_series[pct_series > 0]
    down = pct_series[pct_series < 0]
    flat = pct_series[pct_series == 0]
    up_cnt = len(up)
    down_cnt = len(down)
    flat_cnt = len(flat)
    return {
        '样本数': total,
        '上涨数': up_cnt,
        '下跌数': down_cnt,
        '平盘数': flat_cnt,
        '上涨比例(%)': round(up_cnt / total * 100, 2),
        '上涨均幅(%)': round(up.mean(), 2) if up_cnt > 0 else 0,
        '下跌均幅(%)': round(down.mean(), 2) if down_cnt > 0 else 0,
        '平均涨跌幅(%)': round(pct_series.mean(), 2),
    }

def run_rolling_analysis():
    today = datetime.now().strftime('%Y%m%d')
    print(f"🚀 滚动分析：{START_DATE} → {today}")
    all_dates = get_trade_cal(START_DATE, today)
    if not all_dates:
        print("❌ 无交易日。")
        return
    print(f"📅 共 {len(all_dates)} 个交易日")

    cache_file = 'rolling_cache.parquet'
    data_cache = {}

    if os.path.exists(cache_file):
        try:
            old = pd.read_parquet(cache_file)
            print("✅ 加载缓存。")
            for (dt, lb, sk), grp in old.groupby(['trade_date', '连板数', 'skip_days']):
                data_cache[(dt, lb, sk)] = grp.copy()
        except Exception:
            print("⚠️ 缓存加载失败，重新采集。")

    print("\n📊 采集数据...")
    for i, dt in enumerate(all_dates):
        print(f"\n[{i+1}/{len(all_dates)}] {dt}")
        for lb in LIANBAN_LIST:
            stocks = get_lianban_stocks(dt, lb)
            if stocks.empty:
                for sk in [0] + SKIP_DAYS_LIST:
                    data_cache[(dt, lb, sk)] = pd.DataFrame()
                continue
            codes = stocks['ts_code'].unique().tolist()
            stocks_info = stocks[['ts_code', 'name']].copy()

            key_t0 = (dt, lb, 0)
            if key_t0 not in data_cache:
                t0_df = stocks_info.copy()
                t0_df['pct_chg'] = None
                data_cache[key_t0] = t0_df
                print(f"   📋 {lb}连板 T日: {len(stocks)}只")

            for sk in SKIP_DAYS_LIST:
                key = (dt, lb, sk)
                if key in data_cache and not data_cache[key].empty:
                    print(f"   ⏩ {lb}连板 T+{sk} 使用缓存")
                    continue
                pct_df = get_future_pct(codes, dt, sk)
                if pct_df is not None and not pct_df.empty:
                    merged = pct_df.merge(stocks_info, on='ts_code', how='inner')
                    data_cache[key] = merged
                    s = calc_stats(merged['pct_chg'])
                    if s:
                        print(f"   📈 {lb}连板 T+{sk}: {s['样本数']}只 | "
                              f"上涨{s['上涨比例(%)']}% | "
                              f"上涨均幅{s['上涨均幅(%)']}% | 下跌均幅{s['下跌均幅(%)']}% | 平均涨跌幅{s['平均涨跌幅(%)']}%")
                else:
                    data_cache[key] = pd.DataFrame()
            time.sleep(0.1)

    all_rows = []
    for (dt, lb, sk), sub in data_cache.items():
        if sub.empty:
            continue
        sub = sub.copy()
        sub['trade_date'] = dt
        sub['连板数'] = lb
        sub['skip_days'] = sk
        all_rows.append(sub)

    if all_rows:
        final = pd.concat(all_rows, ignore_index=True)
        final.to_parquet(cache_file, index=False)
        print(f"\n💾 缓存已保存: {cache_file}")

    print("\n📈 滚动统计（按连板数 × 观察日 × 全窗口）")
    print("=" * 80)
    for sk in SKIP_DAYS_LIST:
        print(f"\n{'='*60}")
        print(f"  📌 观察日: T+{sk}")
        print(f"{'='*60}")
        for i in range(len(all_dates)):
            win_dates = all_dates[:i+1]
            w_start, w_end = win_dates[0], win_dates[-1]
            for lb in LIANBAN_LIST:
                parts = []
                for d in win_dates:
                    key = (d, lb, sk)
                    if key in data_cache and not data_cache[key].empty:
                        parts.append(data_cache[key])
                if not parts:
                    continue
                merged = pd.concat(parts, ignore_index=True)
                s = calc_stats(merged['pct_chg'])
                if s:
                    print(f"\n📊 {lb}连板 窗口 [{w_start}~{w_end}] ({len(win_dates)}天) T+{sk}")
                    print(f"   样本: {s['样本数']} | 上涨: {s['上涨数']}({s['上涨比例(%)']}%)")
                    print(f"   上涨均幅: {s['上涨均幅(%)']}% | 下跌均幅: {s['下跌均幅(%)']}%")
                    print(f"   平均涨跌幅: {s['平均涨跌幅(%)']}%")
    print("\n✅ 完成！")

if __name__ == '__main__':
    run_rolling_analysis()
