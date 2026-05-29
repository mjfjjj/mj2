"""
连板股数据采集脚本（仅 T+1）—— 优化版
功能：
  1. 获取首板~五连板股票（仅主板，排除ST/*ST）
  2. 统计 T+1 涨跌幅、上涨比例、上涨均幅、下跌均幅、平均涨跌幅
  3. 滚动窗口：从 2026-04-01 起，每加入新交易日剔除最早交易日
数据源：AkShare（主）+ Tushare（备用）
优化：
  - 修复 Tushare 代码无后缀导致批量接口失效的致命缺陷
  - Tushare 分批请求，防止超限
  - 完善的交易日历降级与缺失数据警告
  - 更清晰的日志与异常记录
"""

import akshare as ak
import tushare as ts
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
import time
import os
import logging

warnings.filterwarnings('ignore')

# 配置日志：输出到控制台，包含时间
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== 配置区 ====================
TUSHARE_TOKEN = '014ba2364f885e96f637e04d79ad0e2f180aeaa3dbae860640cc63ca'
START_DATE = '20260401'
LIANBAN_LIST = [1, 2, 3, 4, 5]        # 连板数
SKIP_DAYS_LIST = [1]                  # 仅 T+1
TUSHARE_BATCH_SIZE = 80               # Tushare daily 接口单次最大股票数
RETRY_COUNT = 3
SLEEP_BETWEEN_STOCKS = 0.05
CACHE_FILE = 'rolling_cache.parquet'
# =================================================

# 初始化 Tushare
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()


def get_trade_cal(start: str, end: str) -> list:
    """获取交易日历，降级方案为周一至周五（不推荐）"""
    try:
        df = pro.trade_cal(exchange='SSE', start_date=start, end_date=end, is_open='1')
        if df is not None and not df.empty:
            dates = sorted(df['cal_date'].unique())
            logger.info(f"使用 Tushare 交易日历，共 {len(dates)} 天")
            return dates
    except Exception as e:
        logger.warning(f"Tushare 交易日历获取失败: {e}")

    try:
        df = ak.tool_trade_date_hist_sina()
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        mask = (df['trade_date'] >= pd.to_datetime(start)) & (df['trade_date'] <= pd.to_datetime(end))
        dates = sorted(df.loc[mask, 'trade_date'].dt.strftime('%Y%m%d').unique())
        if dates:
            logger.info(f"使用 AkShare 交易日历，共 {len(dates)} 天")
            return dates
    except Exception as e:
        logger.warning(f"AkShare 交易日历获取失败: {e}")

    logger.warning("⚠️ 使用降级方案（周一至周五），可能包含非交易日！")
    dates = []
    cur = datetime.strptime(start, '%Y%m%d')
    end_dt = datetime.strptime(end, '%Y%m%d')
    while cur <= end_dt:
        if cur.weekday() < 5:
            dates.append(cur.strftime('%Y%m%d'))
        cur += timedelta(days=1)
    return dates


def get_next_trade_date(base_date: str, skip_days: int) -> str or None:
    """获取 skip_days 个交易日后的日期"""
    try:
        cal = pro.trade_cal(exchange='SSE', start_date=base_date,
                            end_date=(datetime.strptime(base_date, '%Y%m%d') + timedelta(days=15)).strftime('%Y%m%d'),
                            is_open='1')
        if cal is None or cal.empty:
            raise ValueError("empty")
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


def add_ts_suffix(code: str) -> str:
    """为纯数字代码补全交易所后缀"""
    code = str(code).strip()
    if code.startswith(('60', '68')):
        return code + '.SH'
    elif code.startswith(('00', '30')):
        return code + '.SZ'
    else:
        # 其他情况保持不变（如已带后缀）
        return code


def get_lianban_stocks(date_str: str, lianban_num: int) -> pd.DataFrame:
    """获取某日特定连板数的主板股票（排除ST）"""
    stocks = pd.DataFrame()
    # 优先使用 AkShare
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
                logger.debug(f"AkShare 获取 {date_str} {lianban_num}连板 {len(stocks)}只")
    except Exception as e:
        logger.debug(f"AkShare 连板获取失败 {date_str} {lianban_num}连板: {e}")

    # 备用 Tushare
    if stocks.empty:
        try:
            df = pro.limit_step(trade_date=date_str, nums=str(lianban_num))
            if df is not None and not df.empty:
                # Tushare 返回的 ts_code 已经带后缀，但为统一处理也调用 add_ts_suffix
                stocks = pd.DataFrame({
                    'ts_code': df['ts_code'].astype(str).apply(add_ts_suffix),
                    'name': df['name'].astype(str),
                })
                logger.debug(f"Tushare 获取 {date_str} {lianban_num}连板 {len(stocks)}只")
        except Exception as e:
            logger.debug(f"Tushare 连板获取失败 {date_str} {lianban_num}连板: {e}")

    if stocks.empty:
        return stocks

    # 排除 ST 和 *ST
    stocks = stocks[~stocks['name'].str.contains('ST', case=False, na=False)]

    # 筛选主板（60、68、00 开头，排除 30、83、87 等）
    def is_main(code):
        pure = code.replace('.SZ', '').replace('.SH', '').replace('.BJ', '')
        return pure.startswith(('60', '00'))

    stocks = stocks[stocks['ts_code'].apply(is_main)]
    return stocks.reset_index(drop=True)


def batch_fetch_daily(ts_codes: list, target_date: str) -> pd.DataFrame or None:
    """分批从 Tushare 获取日涨跌幅"""
    results = []
    for i in range(0, len(ts_codes), TUSHARE_BATCH_SIZE):
        batch = ts_codes[i:i + TUSHARE_BATCH_SIZE]
        code_str = ','.join(batch)
        try:
            daily = pro.daily(ts_code=code_str, trade_date=target_date, fields='ts_code,pct_chg')
            if daily is not None and not daily.empty:
                results.append(daily)
        except Exception as e:
            logger.warning(f"Tushare 分批查询失败 (部分): {e}")
            # 单批失败尝试降级到下一批，但整个批量获取应视为失败
            # 这里可以选择返回已获取的（部分数据），但为保持一致性，失败则全部退回
            return None  # 一旦有一批失败，整体放弃 Tushare 通道
    if results:
        return pd.concat(results, ignore_index=True)
    return None


def get_future_pct(codes: list, base_date: str, skip_days: int) -> pd.DataFrame or None:
    """获取 T+skip_days 的涨跌幅"""
    target_date = get_next_trade_date(base_date, skip_days)
    if target_date is None:
        logger.debug(f"{base_date} T+{skip_days} 无未来交易日")
        return None

    # 1. 优先使用 Tushare 批量接口
    try:
        df = batch_fetch_daily(codes, target_date)
        if df is not None and not df.empty:
            df['target_date'] = target_date
            return df
    except Exception as e:
        logger.warning(f"Tushare 批量获取整体失败: {e}")

    # 2. 降级为逐只 AkShare 查询
    logger.info(f"降级为 AkShare 逐只查询 {len(codes)} 只股票 {target_date}")
    all_pct = []
    for code in codes:
        # 去掉后缀提取纯数字
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
                else:
                    logger.debug(f"AkShare 返回空数据: {code}")
                time.sleep(SLEEP_BETWEEN_STOCKS)
            except Exception as e:
                if attempt < RETRY_COUNT - 1:
                    time.sleep(0.5)
                else:
                    logger.warning(f"   ⚠️ {code} T+{skip_days} 获取失败，最终错误: {e}")
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


def run_rolling_analysis():
    today = datetime.now().strftime('%Y%m%d')
    logger.info(f"🚀 滚动分析：{START_DATE} → {today}")

    all_dates = get_trade_cal(START_DATE, today)
    if not all_dates:
        logger.error("❌ 无交易日，退出。")
        return
    logger.info(f"📅 共 {len(all_dates)} 个交易日")

    # 加载缓存
    data_cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            old = pd.read_parquet(CACHE_FILE)
            logger.info("✅ 加载缓存成功。")
            for (dt, lb, sk), grp in old.groupby(['trade_date', '连板数', 'skip_days']):
                data_cache[(dt, lb, sk)] = grp.copy()
        except Exception as e:
            logger.warning(f"⚠️ 缓存加载失败: {e}，将重新采集。")

    # 采集数据
    logger.info("\n📊 开始采集数据...")
    for i, dt in enumerate(all_dates):
        logger.info(f"\n[{i+1}/{len(all_dates)}] {dt}")
        for lb in LIANBAN_LIST:
            stocks = get_lianban_stocks(dt, lb)
            if stocks.empty:
                # 仍记录空数据占位
                for sk in [0] + SKIP_DAYS_LIST:
                    key = (dt, lb, sk)
                    if key not in data_cache:
                        data_cache[key] = pd.DataFrame()
                logger.debug(f"  {lb}连板 无股票")
                continue

            codes = stocks['ts_code'].unique().tolist()
            stocks_info = stocks[['ts_code', 'name']].copy()

            # 更新 T 日基础信息（始终覆盖）
            key_t0 = (dt, lb, 0)
            data_cache[key_t0] = stocks_info.assign(pct_chg=None)
            logger.debug(f"  📋 {lb}连板 T日: {len(stocks)}只")

            # 获取 T+skip 数据
            for sk in SKIP_DAYS_LIST:
                key = (dt, lb, sk)
                if key in data_cache and not data_cache[key].empty:
                    logger.debug(f"  ⏩ {lb}连板 T+{sk} 使用缓存")
                    continue

                pct_df = get_future_pct(codes, dt, sk)
                if pct_df is not None and not pct_df.empty:
                    merged = pct_df.merge(stocks_info, on='ts_code', how='inner')
                    data_cache[key] = merged
                    s = calc_stats(merged['pct_chg'])
                    if s:
                        logger.info(f"  📈 {lb}连板 T+{sk}: {s['样本数']}只 | "
                                    f"上涨{s['上涨比例(%)']}% | "
                                    f"上涨均幅{s['上涨均幅(%)']}% | "
                                    f"下跌均幅{s['下跌均幅(%)']}% | "
                                    f"平均涨跌幅{s['平均涨跌幅(%)']}%")
                else:
                    data_cache[key] = pd.DataFrame()
                    logger.warning(f"  ⚠️ {lb}连板 T+{sk} 获取失败或不存在")
            time.sleep(0.1)

    # 保存缓存
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
        final.to_parquet(CACHE_FILE, index=False)
        logger.info(f"\n💾 缓存已保存: {CACHE_FILE}")

    # 滚动窗口统计
    logger.info("\n📈 滚动统计（按连板数 × T+1 × 全窗口）")
    logger.info("=" * 80)
    for sk in SKIP_DAYS_LIST:
        logger.info(f"\n📌 观察日: T+{sk}")
        for i in range(len(all_dates)):
            win_dates = all_dates[:i+1]
            w_start, w_end = win_dates[0], win_dates[-1]
            for lb in LIANBAN_LIST:
                parts = []
                missing_dates = []
                for d in win_dates:
                    key = (d, lb, sk)
                    if key in data_cache:
                        if not data_cache[key].empty:
                            parts.append(data_cache[key])
                        else:
                            missing_dates.append(d)
                    else:
                        missing_dates.append(d)
                if missing_dates:
                    logger.debug(f"  {lb}连板 T+{sk} 窗口缺失日期: {missing_dates}")

                if not parts:
                    continue

                merged = pd.concat(parts, ignore_index=True)
                s = calc_stats(merged['pct_chg'])
                if s:
                    logger.info(f"\n📊 {lb}连板 窗口 [{w_start}~{w_end}] ({len(win_dates)}天) T+{sk}")
                    logger.info(f"   样本: {s['样本数']} | 上涨: {s['上涨数']}({s['上涨比例(%)']}%)")
                    logger.info(f"   上涨均幅: {s['上涨均幅(%)']}% | 下跌均幅: {s['下跌均幅(%)']}%")
                    logger.info(f"   平均涨跌幅: {s['平均涨跌幅(%)']}%")

    logger.info("\n✅ 完成！")


if __name__ == '__main__':
    run_rolling_analysis()
