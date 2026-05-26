import tushare as ts
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
import time
import os

warnings.filterwarnings('ignore')

# ================= 配置区 =================
TUSHARE_TOKEN = '014ba2364f885e96f637e04d79ad0e2f180aeaa3dbae860640cc63ca'
START_DATE = '20260401'
LIANBAN_LIST = [2, 3, 4, 5]
SKIP_DAYS_LIST = [1, 2]   # T+1 和 T+2
# =========================================

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

def get_trade_cal(start_date, end_date):
    try:
        cal_df = pro.trade_cal(exchange='SSE', start_date=start_date, end_date=end_date, is_open='1')
        if cal_df is not None and not cal_df.empty:
            return sorted(cal_df['cal_date'].unique())
    except:
        pass
    try:
        cal_df = ak.tool_trade_date_hist_sina()
        cal_df['trade_date'] = pd.to_datetime(cal_df['trade_date'])
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        mask = (cal_df['trade_date'] >= start_dt) & (cal_df['trade_date'] <= end_dt)
        return sorted(cal_df[mask]['trade_date'].dt.strftime('%Y%m%d').unique())
    except:
        pass
    print("⚠️ 无法获取交易日历，使用降级方案。")
    dates = []
    current = datetime.strptime(start_date, '%Y%m%d')
    end = datetime.strptime(end_date, '%Y%m%d')
    while current <= end:
        if current.weekday() < 5:
            dates.append(current.strftime('%Y%m%d'))
        current += timedelta(days=1)
    return dates

def enrich_stock_info(stocks_df, date_str):
    """为连板股补充当日行情：一字板判断、换手率、最高价、最低价"""
    if stocks_df.empty:
        return stocks_df
    enriched_rows = []
    for _, row in stocks_df.iterrows():
        code = row['ts_code']
        name = row['name']
        pure_code = code.replace('.SZ','').replace('.SH','').replace('.BJ','')
        try:
            time.sleep(0.05)
            hist = ak.stock_zh_a_hist(symbol=pure_code, period="daily",
                                      start_date=date_str, end_date=date_str,
                                      adjust="qfq")
            if hist is not None and not hist.empty:
                h = hist.iloc[0]
                open_ = h['开盘']
                close_ = h['收盘']
                high_ = h['最高']
                low_ = h['最低']
                turnover = h['换手率']
                pct_chg = h['涨跌幅']
                is_yiziban = (pct_chg > 9.5) and (open_ == close_ == high_ == low_)
                enriched_rows.append({
                    'ts_code': code,
                    'name': name,
                    'is_yiziban': is_yiziban,
                    'turnover_rate': turnover,
                    'high': high_,
                    'low': low_
                })
                continue
        except:
            pass
        # 获取失败则填 None
        enriched_rows.append({
            'ts_code': code,
            'name': name,
            'is_yiziban': None,
            'turnover_rate': None,
            'high': None,
            'low': None
        })
    return pd.DataFrame(enriched_rows)

def get_lianban_stocks_main(date_str, lianban_num):
    stocks = pd.DataFrame()
    try:
        df = pro.limit_step(trade_date=date_str, nums=str(lianban_num))
        if df is not None and not df.empty:
            stocks = df[['ts_code', 'name']].copy()
    except:
        pass
    if stocks.empty:
        try:
            df = ak.stock_zt_pool_em(date=date_str)
            if df is not None and not df.empty:
                df['连板数'] = pd.to_numeric(df['连板数'], errors='coerce')
                df = df[df['连板数'] == lianban_num].copy()
                if not df.empty:
                    stocks = pd.DataFrame({
                        'ts_code': df['代码'].astype(str),
                        'name': df['名称'].astype(str)
                    })
        except Exception as e:
            print(f"   ❌ 双源获取失败: {e}")
    if stocks.empty:
        return stocks
    if 'name' in stocks.columns:
        stocks = stocks[~stocks['name'].str.contains('ST', case=False, na=False)]
    def is_main_board(code):
        code_str = str(code).replace('.SZ','').replace('.SH','').replace('.BJ','')
        return code_str.startswith(('60','00'))
    stocks = stocks[stocks['ts_code'].apply(is_main_board)]
    # 补充一字板、换手率、最高最低
    stocks = enrich_stock_info(stocks, date_str)
    return stocks

def get_next_day_pct(codes, base_date, skip_days=1, retry=3):
    try:
        cal_df = pro.trade_cal(exchange='SSE', start_date=base_date,
                               end_date=(datetime.strptime(base_date,'%Y%m%d')+timedelta(days=15)).strftime('%Y%m%d'),
                               is_open='1')
        if cal_df is None or cal_df.empty:
            raise ValueError()
        cal_dates = sorted(cal_df['cal_date'].unique())
        idx = cal_dates.index(base_date) if base_date in cal_dates else -1
        if idx == -1 or idx + skip_days >= len(cal_dates):
            return None
        target_date = cal_dates[idx + skip_days]
        if target_date >= datetime.now().strftime('%Y%m%d'):
            return None
    except:
        try:
            cal_df = ak.tool_trade_date_hist_sina()
            cal_df['trade_date'] = pd.to_datetime(cal_df['trade_date'])
            cal_dates = sorted(cal_df['trade_date'].dt.strftime('%Y%m%d').unique())
            idx = cal_dates.index(base_date) if base_date in cal_dates else -1
            if idx == -1 or idx + skip_days >= len(cal_dates):
                return None
            target_date = cal_dates[idx + skip_days]
            if target_date >= datetime.now().strftime('%Y%m%d'):
                return None
        except:
            return None
    try:
        ts_codes_str = ','.join(codes)
        daily = pro.daily(ts_code=ts_codes_str, trade_date=target_date, fields='ts_code,pct_chg')
        if daily is not None and not daily.empty:
            daily['target_date'] = target_date
            return daily
    except:
        pass
    try:
        all_pct = []
        for code in codes:
            pure_code = code.replace('.SZ','').replace('.SH','').replace('.BJ','')
            for attempt in range(retry):
                try:
                    hist = ak.stock_zh_a_hist(symbol=pure_code, period="daily",
                                              start_date=target_date, end_date=target_date,
                                              adjust="qfq")
                    if hist is not None and not hist.empty:
                        pct = hist.iloc[0]['涨跌幅']
                        all_pct.append({'ts_code': code, 'pct_chg': pct})
                        break
                    time.sleep(0.1)
                except:
                    if attempt < retry - 1:
                        time.sleep(0.5)
                    else:
                        print(f"   ⚠️ {code} 获取失败")
        if all_pct:
            res = pd.DataFrame(all_pct)
            res['target_date'] = target_date
            return res
    except:
        pass
    return None

def calculate_stats(pct_df):
    if pct_df is None or pct_df.empty:
        return None
    total = len(pct_df)
    up_df = pct_df[pct_df['pct_chg'] > 0]
    down_df = pct_df[pct_df['pct_chg'] < 0]
    flat_df = pct_df[pct_df['pct_chg'] == 0]
    up_count = len(up_df)
    down_count = len(down_df)
    flat_count = len(flat_df)
    up_ratio = (up_count / total) * 100
    up_avg = up_df['pct_chg'].mean() if up_count > 0 else 0
    down_avg = down_df['pct_chg'].mean() if down_count > 0 else 0
    return {
        '总样本数': total,
        '上涨家数': up_count,
        '下跌家数': down_count,
        '平盘家数': flat_count,
        '上涨比例(%)': round(up_ratio, 2),
        '上涨均幅(%)': round(up_avg, 2),
        '下跌均幅(%)': round(down_avg, 2)
    }

def run_rolling_analysis():
    today_str = datetime.now().strftime('%Y%m%d')
    print(f"🚀 启动滚动分析：{START_DATE} → {today_str}")
    all_dates = get_trade_cal(START_DATE, today_str)
    if not all_dates:
        print("❌ 未找到交易日。")
        return
    print(f"📅 共 {len(all_dates)} 个交易日")

    cache_file = 'rolling_cache.parquet'
    data_cache = {}  # key: (date, lb, skip_days)

    if os.path.exists(cache_file):
        try:
            cached_df = pd.read_parquet(cache_file)
            print("✅ 加载缓存。")
            for (date_str, lb, skip), grp in cached_df.groupby(['trade_date', '连板数', 'skip_days']):
                key = (date_str, lb, skip)
                # 提取 stocks 信息（唯一值）
                stocks_info = grp[['ts_code', 'name', 'is_yiziban', 'turnover_rate', 'high', 'low']].drop_duplicates()
                data_cache[key] = {
                    'pct_df': grp[['ts_code', 'pct_chg']].copy() if 'pct_chg' in grp else pd.DataFrame(),
                    'stocks': stocks_info.to_dict('records')
                }
        except:
            print("⚠️ 缓存加载失败，重新获取。")

    print("\n📊 处理数据...")
    for i, date_str in enumerate(all_dates):
        print(f"\n[{i+1}/{len(all_dates)}] {date_str}")
        for lb in LIANBAN_LIST:
            stocks = get_lianban_stocks_main(date_str, lb)
            if stocks.empty:
                print(f"   ⚠️ {lb}连板 无符合条件股票")
                for skip in [0] + SKIP_DAYS_LIST:
                    data_cache[(date_str, lb, skip)] = {'pct_df': pd.DataFrame(), 'stocks': []}
                continue
            stocks_list = stocks.to_dict('records')
            codes = stocks['ts_code'].unique().tolist()
            # 记录 T 日股票列表 (skip=0)
            key_t0 = (date_str, lb, 0)
            if key_t0 not in data_cache:
                data_cache[key_t0] = {'pct_df': pd.DataFrame(), 'stocks': stocks_list}
                print(f"   📋 {lb}连板 T日: {len(stocks)}只已记录")
            # 获取 T+1 / T+2 涨跌幅
            for skip in SKIP_DAYS_LIST:
                key = (date_str, lb, skip)
                if key in data_cache:
                    print(f"   ⏩ {lb}连板 T+{skip} 使用缓存")
                    continue
                pct_df = get_next_day_pct(codes, date_str, skip_days=skip)
                if pct_df is not None and not pct_df.empty:
                    pct_df = pct_df.merge(stocks[['ts_code']], on='ts_code', how='inner')
                    data_cache[key] = {'pct_df': pct_df, 'stocks': stocks_list}
                    stats = calculate_stats(pct_df)
                    if stats:
                        print(f"   📈 {lb}连板 T+{skip}: {stats['总样本数']}只 | "
                              f"上涨{stats['上涨比例(%)']}% | 上涨均幅{stats['上涨均幅(%)']}%")
                else:
                    data_cache[key] = {'pct_df': pd.DataFrame(), 'stocks': stocks_list}
            time.sleep(0.1)

    # 保存缓存
    all_records = []
    for (date_str, lb, skip), entry in data_cache.items():
        if not entry['stocks']:
            continue
        stocks_df = pd.DataFrame(entry['stocks'])
        if not entry['pct_df'].empty:
            pct_df = entry['pct_df'].copy()
            pct_df['trade_date'] = date_str
            pct_df['连板数'] = lb
            pct_df['skip_days'] = skip
            pct_df = pct_df.merge(stocks_df[['ts_code', 'is_yiziban', 'turnover_rate', 'high', 'low']], on='ts_code', how='left')
            all_records.append(pct_df)
        else:
            # T 日数据，pct_chg 为空
            t0_df = stocks_df.copy()
            t0_df['trade_date'] = date_str
            t0_df['连板数'] = lb
            t0_df['skip_days'] = skip
            t0_df['pct_chg'] = None
            all_records.append(t0_df)
    if all_records:
        final_df = pd.concat(all_records, ignore_index=True)
        final_df.to_parquet(cache_file, index=False)
        print(f"\n💾 缓存已保存: {cache_file}")

    # 滚动统计（原有逻辑）
    print("\n📈 滚动统计（按连板数、观察日分组）...")
    print("=" * 80)
    for skip in SKIP_DAYS_LIST:
        print(f"\n📌 观察日: T+{skip}")
        for i in range(len(all_dates)):
            window_dates = all_dates[0:i+1]
            window_start = window_dates[0]
            window_end = window_dates[-1]
            for lb in LIANBAN_LIST:
                all_window_data = []
                for d in window_dates:
                    key = (d, lb, skip)
                    if key in data_cache and not data_cache[key]['pct_df'].empty:
                        day_df = data_cache[key]['pct_df'].copy()
                        day_df['trade_date'] = d
                        all_window_data.append(day_df)
                if not all_window_data:
                    continue
                window_df = pd.concat(all_window_data, ignore_index=True)
                stats = calculate_stats(window_df)
                if stats:
                    print(f"📊 {lb}连板 窗口 [{window_start}~{window_end}] ({len(window_dates)}天) T+{skip}")
                    print(f"   样本: {stats['总样本数']} | 上涨: {stats['上涨家数']}({stats['上涨比例(%)']}%)")
                    print(f"   上涨均幅: {stats['上涨均幅(%)']}% | 下跌均幅: {stats['下跌均幅(%)']}%")
    print("\n✅ 完成！")

if __name__ == "__main__":
    run_rolling_analysis()