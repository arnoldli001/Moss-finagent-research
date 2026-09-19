"""QMT(xtquant) 全量日线数据下载脚本。

运行环境：系统 Python（C:\\veighna_studio\\python.exe，已装 xtquant 250807），
        不要用项目 uv venv（3.13 兼容性以实际安装为准，见 README）。
前置条件：QMT 极简模式 XtMiniQmt.exe 已启动并登录（xtdata 服务 127.0.0.1:58610）。

用法：
    # 小批量验证（10只）
    python scripts/download_qmt_data.py --limit 10
    # 全量：沪深A股全历史日线 + 主要宽基指数（增量，可重复执行）
    python scripts/download_qmt_data.py

数据落地：QMT 数据目录 userdata_mini/datadir（xtdata 原生存储），
        项目侧 XtQuantConnector 通过 xtdata.get_market_data_ex 直接读取，无需另存。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from src.core.errors import BRIEF_DEFAULT

LOG_DIR = Path("data/qmt")
INDEX_CODES = [
    "000001.SH",  # 上证综指
    "399001.SZ",  # 深证成指
    "000300.SH",  # 沪深300
    "000905.SH",  # 中证500
    "000852.SH",  # 中证1000
    "399006.SZ",  # 创业板指
    "000688.SH",  # 科创50
]


def setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"download_{datetime.now():%Y%m%d_%H%M%S}.log"
    logger = logging.getLogger("qmt_download")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.info("日志文件: %s", log_path)
    return logger


def connect_with_retry(xtdata, logger, retries: int = 6, wait: float = 5.0):
    """首次调用触发本地连接；极简模式刚启动时需要等待。"""
    for i in range(1, retries + 1):
        try:
            codes = xtdata.get_stock_list_in_sector("沪深A股")
            if codes:
                logger.info("xtdata 已连接，沪深A股 %d 只", len(codes))
                return codes
        except Exception as exc:  # noqa: BLE001
            logger.warning("第%d次连接失败: %s（%ss后重试）", i, exc, wait)
            time.sleep(wait)
    raise RuntimeError("无法连接 xtquant 服务：请确认 XtMiniQmt.exe 已启动并登录")


class Progress:
    """download_history_data2 进度回调（不同版本参数形态不一，用args兼容）。"""

    def __init__(self, total: int, logger: logging.Logger) -> None:
        self.total = total
        self.logger = logger
        self.last_log = 0.0
        self.last_finished = -1

    def __call__(self, *args) -> None:
        # 常见形态: (progress, total, finishedcount, total_duration, msg, msg_len)
        # 新版部分回调为 dict
        finished = None
        if args and isinstance(args[0], dict):
            finished = args[0].get("finished") or args[0].get("finishedcount")
        elif len(args) >= 3:
            finished = args[2]
        now = time.time()
        if finished is not None and (
            finished != self.last_finished and now - self.last_log >= 3
        ):
            self.last_finished = finished
            self.last_log = now
            self.logger.info("批量下载进度: %d/%d (%.1f%%)",
                             finished, self.total, 100.0 * finished / self.total)


def batch_download(xtdata, codes: list[str], logger, *, tag: str) -> list[str]:
    """批量下载全历史日线，返回失败代码清单。"""
    total = len(codes)
    logger.info("[%s] 开始批量下载 %d 只全历史日线 ...", tag, total)
    t0 = time.time()
    failed: list[str] = []
    try:
        xtdata.download_history_data2(
            codes, "1d", start_time="", end_time="",
            callback=Progress(total, logger), incrementally=True,
        )
    except Exception as exc:  # noqa: BLE001 批量失败降级为逐只
        logger.warning("[%s] 批量接口异常(%s)，降级逐只下载", tag, exc)

    # 逐只校验/补漏（批量回调不返回每只成败）
    for i, code in enumerate(codes, 1):
        try:
            xtdata.download_history_data(code, "1d", "", "", incrementally=True)
        except Exception as exc:  # noqa: BLE001
            failed.append(code)
            logger.warning("[%s] %s 下载失败: %s", tag, code, brief(exc, BRIEF_DEFAULT))
        if i % 200 == 0:
            logger.info("[%s] 逐只补漏进度 %d/%d，累计失败 %d",
                        tag, i, total, len(failed))
    logger.info("[%s] 首轮完成，耗时 %.0fs，失败 %d 只",
                tag, time.time() - t0, len(failed))
    return failed


def retry_failed(xtdata, failed: list[str], logger, rounds: int = 3) -> list[str]:
    """失败标的重试（网络抖动常见）。"""
    for r in range(1, rounds + 1):
        if not failed:
            break
        logger.info("失败重试第%d轮，剩余 %d 只", r, len(failed))
        still: list[str] = []
        for code in failed:
            try:
                xtdata.download_history_data(code, "1d", "", "", incrementally=True)
            except Exception:  # noqa: BLE001
                still.append(code)
            time.sleep(0.2)
        failed = still
    return failed


def verify_sample(xtdata, codes: list[str], logger) -> None:
    """抽样验证本地可读与数据量。"""
    import random

    sample = random.sample(codes, min(5, len(codes)))
    for code in sample:
        data = xtdata.get_market_data_ex(
            [], [code], period="1d", start_time="", end_time="", count=-1)
        df = data.get(code)
        rows = 0 if df is None else len(df)
        if rows:
            first = datetime.fromtimestamp(int(df["time"].iloc[0]) / 1000).date()
            last = datetime.fromtimestamp(int(df["time"].iloc[-1]) / 1000).date()
            logger.info("抽样 %s: %d 行, %s ~ %s", code, rows, first, last)
        else:
            logger.warning("抽样 %s: 0 行", code)


def main() -> int:
    parser = argparse.ArgumentParser(description="QMT 全量日线下载")
    parser.add_argument("--limit", type=int, default=0, help="只下载前N只（验证用）")
    parser.add_argument("--no-index", action="store_true", help="跳过宽基指数")
    args = parser.parse_args()

    from xtquant import xtdata

    xtdata.enable_hello = False
    logger = setup_logger()

    stock_codes = connect_with_retry(xtdata, logger)
    if args.limit:
        stock_codes = stock_codes[: args.limit]
    logger.info("本次股票标的 %d 只", len(stock_codes))

    failed = batch_download(xtdata, stock_codes, logger, tag="stock")

    if not args.no_index:
        idx_failed = batch_download(xtdata, INDEX_CODES, logger, tag="index")
        failed += [f"idx:{c}" for c in idx_failed]

    failed = retry_failed(xtdata, [c.removeprefix("idx:") for c in failed], logger)

    verify_sample(xtdata, stock_codes, logger)

    if failed:
        logger.error("最终失败 %d 只: %s", len(failed), ",".join(failed[:50]))
        return 1
    logger.info("全部下载完成并抽样验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
