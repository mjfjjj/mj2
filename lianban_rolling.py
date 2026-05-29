"""
连板股数据采集脚本（仅 T+1）—— 滚动窗口修正版
功能：
  1. 获取首板~五连板股票（仅主板，排除ST/*ST）
  2. 统计 T+1 涨跌幅、上涨比例、上涨均幅、下跌均幅、平均涨跌幅
  3. 真正滚动窗口（固定长度，每加入新交易日剔除最早交易日）
数据源：AkShare（主）+ Tushare（备用，分批次）
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
START_DATE = '20260401'          # 起始日期
WINDOW_SIZE = 20                 # 滚动窗口大小（交易日个数）
LIANBAN_LIST = [1, 2, 3, 4, 5]   # 连板数
SKIP_DAYS_LIST = [1]              # 仅 T+1
RETRY_COUNT = 3                   # AkShare 单只股票重试次数
SLEEP_BETWEEN_STOCKS = 0.5        # 逐只查询间隔（秒），避免被禁
TUSHARE_BATCH_SIZE = 80           # Tushare 批量查询每批最多股票数
# =================================================

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()


def get_trade_cal(start: str, end: str):
    """获取交易日历：优先 AkShare，若失败则返回空列表（不再使用降级方案）"""
    try:
        df = ak.tool_trade_date_hist_sina()
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        mask = (df['trade_date'] >= pd.to_datetime(start)) & (df['trade_date'] <= pd.to_datetime(end))
        dates = sorted(df.loc[mask, 'trade_date'].dt.strftime('%Y%m%d').unique())
        if dates:
            print(f"✅ 使用 AkShare 交易日历，共 {len(dates)} 天")
            return dates
    except Exception as e:
        print(f"⚠️ AkShare 交易日历失败: {e}")

    # 备用：Tushare 交易日历
    try:
        df = pro.trade_cal(exchange='SSE', start_date=start, end_date=end, is_open='1')
        if df is not None and not df.empty:
            dates = sorted(df['cal_date'].unique())
            print(f"✅ 使用 Tushare 交易日历，共 {len(dates)} 天")
            return dates
    except Exception as e:
        print(f"⚠️ Tushare 交易日历失败: {e}")

    print("❌ 无法获取交易日历，程序退出")
    return []


def get_next_trade_date(base_date: str, skip_days: int, all_dates: list) -> str or None:
    """基于已有交易日历列表快速获取未来第 skip_days 个交易日"""
    try:
        idx = all_dates.index(base_date)
    except ValueError:
        return None
    target_idx = idx + skip_days
    if target_idx >= len(all_dates):
        return None
    target = all_dates[target_idx]
    # 只允许历史日期（不含当日，因为当日未收盘）
    if target >= datetime.now().strftime('%Y%m%d'):
        return None
    return target


def add_ts_suffix(code: str) -> str:
    """为纯数字代码补全交易所后缀"""
    code = str(code).strip()
    if code.startswith(('60', '688')):
        return code + '.SH'
    elif code.startswith(('00', '30')):
        return code + '.SZ'
    else:
        return code


def get_lianban_stocks(date_str: str, lianban_num: int) -> pd.DataFrame:
    """获取某日特定连板数的主板股票（排除ST）"""
    stocks = pd.DataFrame()
    # 优先 AkShare
    try:
        raw = ak.stock_zt_pool_em(date=date_str)
        if raw is not None and not raw.empty:
            raw['连板数'] = pd.to_numeric(raw['连板数'], errors='coerce')
            sub = raw[raw['连板数'] == lianban_num].copy()
            if not sub.empty:
                stocks = pd.DataFrame({
                    'ts_code': sub['代码'].astype(str).apply(add_ts_suffix),
                    'name': sub['名称'].astype(str),
                })
    except Exception:
        pass

    # 备用 Tushare
    if stocks.empty:
        try:
            df = pro.limit_step(trade_date=date_str, nums=str(lianban_num))
            if df is not None and not df.empty:
                stocks = pd.DataFrame({
                    'ts_code': df['ts_code'].astype(str),
                    'name': df['name'].astype(str),
                })
        except Exception:
            pass

    if stocks.empty:
        return stocks

    # 排除 ST / *ST
    stocks = stocks[~stocks['name'].str.contains('ST', case=False, na=False)]

    # 筛选主板（60、68、00 开头）
    def is_main(code):
        pure = code.replace('.SH', '').replace('.SZ', '')
        return pure.startswith(('60', '68', '00'))
    stocks = stocks[stocks['ts_code'].apply(is_main)]
    return stocks.reset_index(drop=True)


def batch_fetch_daily(ts_codes: list, target_date: str) -> pd.DataFrame or None:
    """分批次从 Tushare 获取日涨跌幅，避免字符串过长"""
    results = []
    for i in range(0, len(ts_codes), TUSHARE_BATCH_SIZE):
        batch = ts_codes[i:i + TUSHARE_BATCH_SIZE]
        code_str = ','.join(batch)
        try:
            daily = pro.daily(ts_code=code_str, trade_date=target_date, fields='ts_code,pct_chg')
            if daily is not None and not daily.empty:
                results.append(daily)
        except Exception as e:
            print(f"   Tushare 批次 {i//TUSHARE_BATCH_SIZE+1} 失败: {e}")
            # 继续下一批
    if results:
        return pd.concat(results, ignore_index=True)
    return None


def get_pct_from_akshare(code: str, target_date: str) -> float or None:
    """使用 AkShare 获取单只股票涨跌幅（带重试）"""
    pure = code.replace('.SH', '').replace('.SZ', '')
    for attempt in range(RETRY_COUNT):
        try:
            hist = ak.stock_zh_a_hist(symbol=pure, period='daily',
                                      start_date=target_date, end_date=target_date,
                                      adjust='qfq')
            if hist is not None and not hist.empty:
                return float(hist.iloc[0]['涨跌幅'])
            else:
                # 空数据，可能停牌，稍等后重试
                if attempt < RETRY_COUNT - 1:
                    time.sleep(0.5)
        except Exception as e:
            if attempt < RETRY_COUNT - 1:
                time.sleep(0.5)
            else:
                print(f"      AkShare 失败 {code}: {e}")
    return None


def get_future_pct(codes: list, base_date: str, skip_days: int, all_dates: list) -> pd.DataFrame or None:
    """
    获取 T+skip_days 的涨跌幅
    降级链路: Tushare批量（分批次） -> AkShare逐只
    """
    target_date = get_next_trade_date(base_date, skip_days, all_dates)
    if target_date is None:
        return None

    # 1. Tushare 批量接口
    batch_df = batch_fetch_daily(codes, target_date)
    if batch_df is not None and not batch_df.empty:
        batch_df['target_date'] = target_date
        return batch_df

    # 2. 降级到 AkShare 逐只
    print(f"   降级使用 AkShare 逐只查询 {len(codes)} 只股票 (间隔{SLEEP_BETWEEN_STOCKS}s)")
    all_pct = []
    for code in codes:
        pct = get_pct_from_akshare(code, target_date)
        if pct is not None:
            all_pct.append({'ts_code': code, 'pct_chg': pct})
        else:
            print(f"      ⚠️ {code} 无数据")
        time.sleep(SLEEP_BETWEEN_STOCKS)

    if all_pct:
        res = pd.DataFrame(all_pct)
        res['target_date'] = target_date
        return res
    return None


def calc_stats(pct_series: pd.Series) -> dict or None:
    """计算涨跌幅统计指标"""
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


def is_data_healthy(df: pd.DataFrame) -> bool:
    """简单健康度检查：是否有非零涨跌幅"""
    if df is None or df.empty:
        return False
    pct = df['pct_chg'].dropna()
    return (pct != 0).any()


def run_rolling_analysis():
    today = datetime.now().strftime('%Y%m%d')
    print(f"🚀 滚动分析：{START_DATE} → {today}，窗口大小：{WINDOW_SIZE}个交易日")

    # 获取交易日历
    all_dates = get_trade_cal(START_DATE, today)
    if not all_dates:
        return
    print(f"📅 共 {len(all_dates)} 个交易日")

    # 缓存（只存储 skip_days > 0 的数据）
    cache_file = 'rolling_cache.parquet'
    data_cache = {}   # key: (date, lianban, skip_days) -> DataFrame with columns ts_code, name, pct_chg, target_date

    if os.path.exists(cache_file):
        try:
            old = pd.read_parquet(cache_file)
            print("✅ 加载缓存成功")
            for (dt, lb, sk), grp in old.groupby(['trade_date', '连板数', 'skip_days']):
                data_cache[(dt, lb, sk)] = grp.drop(columns=['trade_date', '连板数', 'skip_days'])
        except Exception as e:
            print(f"⚠️ 缓存加载失败: {e}，将重新采集")

    # ========== 采集数据 ==========
    print("\n📊 开始采集数据...")
    for i, dt in enumerate(all_dates):
        print(f"\n[{i+1}/{len(all_dates)}] {dt}")
        for lb in LIANBAN_LIST:
            stocks = get_lianban_stocks(dt, lb)
            if stocks.empty:
                # 无连板股，跳过该日（不缓存）
                continue

            codes = stocks['ts_code'].unique().tolist()
            stocks_info = stocks[['ts_code', 'name']].copy()

            # 只采集 T+skip_days 数据
            for sk in SKIP_DAYS_LIST:
                key = (dt, lb, sk)
                # 检查缓存中是否有且健康
                if key in data_cache and is_data_healthy(data_cache[key]):
                    print(f"   ⏩ {lb}连板 T+{sk} 使用缓存")
                    continue

                pct_df = get_future_pct(codes, dt, sk, all_dates)
                if pct_df is not None and not pct_df.empty:
                    merged = pct_df.merge(stocks_info, on='ts_code', how='inner')
                    # 健康度检查
                    if not is_data_healthy(merged):
                        print(f"   ⚠️ {lb}连板 T+{sk} 数据健康度不佳（涨跌幅全为零）")
                    data_cache[key] = merged
                    s = calc_stats(merged['pct_chg'])
                    if s:
                        print(f"   📈 {lb}连板 T+{sk}: {s['样本数']}只 | "
                              f"上涨{s['上涨比例(%)']}% | "
                              f"上涨均幅{s['上涨均幅(%)']}% | 下跌均幅{s['下跌均幅(%)']}% | "
                              f"平均涨跌幅{s['平均涨跌幅(%)']}%")
                else:
                    print(f"   ❌ {lb}连板 T+{sk} 无未来数据")
                    data_cache[key] = pd.DataFrame()  # 记录空值避免重复尝试
            time.sleep(0.2)  # 交易日之间短暂停顿

    # ========== 保存缓存（仅含 T+1 数据） ==========
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
        print(f"\n💾 缓存已保存: {cache_file} (共 {len(final)} 条记录)")
    else:
        print("\n⚠️ 无数据可保存")

    # ========== 滚动窗口统计 ==========
    if len(all_dates) < WINDOW_SIZE:
        print(f"\n⚠️ 交易日数量({len(all_dates)})小于窗口大小({WINDOW_SIZE})，无法进行滚动统计")
        return

    print("\n📈 滚动统计（固定窗口滑动）")
    print("=" * 80)
    for sk in SKIP_DAYS_LIST:
        print(f"\n📌 观察日: T+{sk}")
        # 存储滚动结果用于后续分析
        rolling_results = []
        for end_idx in range(WINDOW_SIZE, len(all_dates) + 1):
            win_dates = all_dates[end_idx - WINDOW_SIZE : end_idx]
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
                    rolling_results.append({
                        '窗口起始': w_start,
                        '窗口结束': w_end,
                        '连板数': lb,
                        'skip_days': sk,
                        '样本数': s['样本数'],
                        '上涨比例(%)': s['上涨比例(%)'],
                        '上涨均幅(%)': s['上涨均幅(%)'],
                        '下跌均幅(%)': s['下跌均幅(%)'],
                        '平均涨跌幅(%)': s['平均涨跌幅(%)'],
                    })
                    # 简要打印
                    print(f"{lb}连板 [{w_start}~{w_end}] 样本{s['样本数']} | "
                          f"上涨比例{s['上涨比例(%)']}% | 平均涨跌幅{s['平均涨跌幅(%)']}%")
        # 可选：将滚动结果保存为 CSV
        if rolling_results:
            df_res = pd.DataFrame(rolling_results)
            csv_file = f'rolling_stats_Tplus{sk}.csv'
            df_res.to_csv(csv_file, index=False, encoding='utf-8-sig')
            print(f"\n💾 滚动统计结果已保存至 {csv_file}")

    print("\n✅ 完成！")


if __name__ == '__main__':
    run_rolling_analysis()
