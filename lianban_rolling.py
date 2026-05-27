"""
连板股数据采集脚本（完整版）
功能：
  1. 获取首板~五连板股票（仅主板，排除ST/*ST）
  2. 统计 T+1 / T+2 涨跌幅、最大跌幅、上涨比例
  3. 获取当日上证指数、深证成指、沪深300行情
  4. 滚动窗口：从 2026-04-01 起，每加入新交易日剔除最早交易日
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
START_DATE = '20260401'                  # 滚动窗口起始日
LIANBAN_LIST = [1, 2, 3, 4, 5]          # 需要统计的连板数（含首板）
SKIP_DAYS_LIST = [1, 2]                 # T+1 和 T+2
RETRY_COUNT = 3                          # 接口重试次数
SLEEP_BETWEEN_STOCKS = 0.05             # 单只股票间休眠秒数
# =================================================

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

# ------------------- 交易日历 -------------------
def get_trade_cal(start: str, end: str):
    """获取交易日历，Tushare 为主，AkShare 备用"""
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
    """获取 base_date 之后第 skip_days 个交易日，若日期≥今天则返回 None"""
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


# ------------------- 连板股获取 -------------------
def get_lianban_stocks(date_str: str, lianban_num: int) -> pd.DataFrame:
    """
    获取指定日期、指定连板数的股票（仅主板、非ST）
    优先 AkShare，失败时降级 Tushare
    """
    stocks = pd.DataFrame()

    # ① AkShare
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

    # ② Tushare 备用
    if stocks.empty:
        try:
            df = pro.limit_step(trade_date=date_str, nums=str(lianban_num))
            if df is not None and not df.empty:
                stocks = df[['ts_code', 'name']].copy()
        except Exception:
            pass

    if stocks.empty:
        return stocks

    # 过滤 ST
    if 'name' in stocks.columns:
        stocks = stocks[~stocks['name'].str.contains('ST', case=False, na=False)]

    # 过滤主板（60xxxx / 00xxxx）
    def is_main(code):
        c = str(code).replace('.SZ', '').replace('.SH', '').replace('.BJ', '')
        return c.startswith(('60', '00'))
    stocks = stocks[stocks['ts_code'].apply(is_main)]

    return stocks


# ------------------- T+1 / T+2 涨跌幅获取 -------------------
def get_future_pct(codes: list, base_date: str, skip_days: int):
    """获取 base_date 后第 skip_days 个交易日的涨跌幅"""
    target_date = get_next_trade_date(base_date, skip_days)
    if target_date is None:
        return None

    # ① Tushare
    try:
        ts_codes = ','.join(codes)
        daily = pro.daily(ts_code=ts_codes, trade_date=target_date, fields='ts_code,pct_chg')
        if daily is not None and not daily.empty:
            daily['target_date'] = target_date
            return daily
    except Exception:
        pass

    # ② AkShare 备用
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


# ------------------- 大盘指数获取 -------------------
def get_index_snapshot(date_str: str) -> dict:
    """
    获取上证指数(000001)、深证成指(399001)、沪深300(000300)的当日涨跌幅
    优先 Tushare index_daily，失败则尝试 AkShare
    """
    result = {'上证指数': None, '深证成指': None, '沪深300': None}
    index_map = {
        '000001.SH': '上证指数',
        '399001.SZ': '深证成指',
        '000300.SH': '沪深300',
    }

    # ① Tushare
    try:
        df = pro.index_daily(ts_code=','.join(index_map.keys()),
                             start_date=date_str, end_date=date_str,
                             fields='ts_code,pct_chg')
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = index_map.get(row['ts_code'])
                if name:
                    result[name] = round(float(row['pct_chg']), 2)
            if all(v is not None for v in result.values()):
                return result
    except Exception:
        pass

    # ② AkShare 备用（stock_zh_index_daily_em）
    try:
        df = ak.stock_zh_index_daily_em(symbol="sh000001")
        if df is not None and not df.empty:
            row = df[df['date'] == date_str]
            if not row.empty:
                result['上证指数'] = round(float(row.iloc[0]['pct_chg']), 2)
    except Exception:
        pass
    try:
        df = ak.stock_zh_index_daily_em(symbol="sz399001")
        if df is not None and not df.empty:
            row = df[df['date'] == date_str]
            if not row.empty:
                result['深证成指'] = round(float(row.iloc[0]['pct_chg']), 2)
    except Exception:
        pass
    try:
        df = ak.stock_zh_index_daily_em(symbol="sh000300")
        if df is not None and not df.empty:
            row = df[df['date'] == date_str]
            if not row.empty:
                result['沪深300'] = round(float(row.iloc[0]['pct_chg']), 2)
    except Exception:
        pass

    return result


# ------------------- 统计函数 -------------------
def calc_stats(pct_series: pd.Series):
    """输入一列涨跌幅，返回统计字典"""
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
        '最大涨幅(%)': round(pct_series.max(), 2),
        '最大跌幅(%)': round(pct_series.min(), 2),
        '平均涨跌幅(%)': round(pct_series.mean(), 2),
    }


# ------------------- 主流程 -------------------
def run_rolling_analysis():
    today = datetime.now().strftime('%Y%m%d')
    print(f"🚀 滚动分析：{START_DATE} → {today}")
    all_dates = get_trade_cal(START_DATE, today)
    if not all_dates:
        print("❌ 无交易日。")
        return
    print(f"📅 共 {len(all_dates)} 个交易日")

    cache_file = 'rolling_cache.parquet'
    data_cache = {}          # key: (date, lianban, skip) -> DataFrame
    index_cache = {}         # key: date -> index dict

    # --- 加载已有缓存 ---
    if os.path.exists(cache_file):
        try:
            old = pd.read_parquet(cache_file)
            print("✅ 加载缓存。")
            for (dt, lb, sk), grp in old.groupby(['trade_date', '连板数', 'skip_days']):
                data_cache[(dt, lb, sk)] = grp.copy()
            # 加载指数缓存
            if 'index_name' in old.columns:
                idx_df = old[['trade_date', 'index_name', 'index_pct']].drop_duplicates()
                for _, r in idx_df.iterrows():
                    index_cache.setdefault(r['trade_date'], {})[r['index_name']] = r['index_pct']
        except Exception:
            print("⚠️ 缓存加载失败，重新采集。")

    # --- 逐日采集 ---
    print("\n📊 采集数据...")
    for i, dt in enumerate(all_dates):
        print(f"\n[{i+1}/{len(all_dates)}] {dt}")

        # 大盘指数（仅当天未缓存时获取）
        if dt not in index_cache:
            idx_val = get_index_snapshot(dt)
            index_cache[dt] = idx_val
            print(f"   📊 大盘: 上证{idx_val['上证指数']}%  深证{idx_val['深证成指']}%  沪深300{idx_val['沪深300']}%")
        else:
            print(f"   ⏩ 大盘: 使用缓存")

        for lb in LIANBAN_LIST:
            # 获取连板股
            stocks = get_lianban_stocks(dt, lb)
            if stocks.empty:
                for sk in [0] + SKIP_DAYS_LIST:
                    data_cache[(dt, lb, sk)] = pd.DataFrame()
                continue

            codes = stocks['ts_code'].unique().tolist()
            stocks_info = stocks[['ts_code', 'name']].copy()

            # T 日列表 (skip=0)
            key_t0 = (dt, lb, 0)
            if key_t0 not in data_cache:
                t0_df = stocks_info.copy()
                t0_df['pct_chg'] = None
                data_cache[key_t0] = t0_df
                print(f"   📋 {lb}连板 T日: {len(stocks)}只")

            # T+1 / T+2
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
                              f"上涨{s['上涨比例(%)']}% | 最大涨幅{s['最大涨幅(%)']}% | 最大跌幅{s['最大跌幅(%)']}%")
                else:
                    data_cache[key] = pd.DataFrame()
            time.sleep(0.1)

    # --- 保存缓存 ---
    all_rows = []
    for (dt, lb, sk), sub in data_cache.items():
        if sub.empty:
            continue
        sub = sub.copy()
        sub['trade_date'] = dt
        sub['连板数'] = lb
        sub['skip_days'] = sk
        all_rows.append(sub)

    # 追加指数数据
    idx_rows = []
    for dt, vals in index_cache.items():
        for name in ['上证指数', '深证成指', '沪深300']:
            if vals.get(name) is not None:
                idx_rows.append({
                    'trade_date': dt, '连板数': -1, 'skip_days': -1,
                    'ts_code': '', 'name': '', 'pct_chg': None,
                    'index_name': name, 'index_pct': vals[name],
                })
    if idx_rows:
        all_rows.append(pd.DataFrame(idx_rows))

    if all_rows:
        final = pd.concat(all_rows, ignore_index=True)
        final.to_parquet(cache_file, index=False)
        print(f"\n💾 缓存已保存: {cache_file}")

    # --- 打印滚动统计 ---
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
                    print(f"   最大涨幅: {s['最大涨幅(%)']}% | 最大跌幅: {s['最大跌幅(%)']}% | 平均涨跌幅: {s['平均涨跌幅(%)']}%")
    print("\n✅ 完成！")


if __name__ == '__main__':
    run_rolling_analysis()
