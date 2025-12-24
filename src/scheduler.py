"""
定时任务调度器模块

负责配置和运行定时备份任务。
"""

import asyncio
import signal
import sys
from datetime import datetime
from typing import Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from .backup_manager import BackupManager
from .config import AppConfig


class BackupScheduler:
    """备份任务调度器"""
    
    def __init__(self, config: AppConfig):
        """
        初始化调度器
        
        Args:
            config: 应用配置
        """
        self.config = config
        self.scheduler = AsyncIOScheduler()
        self.backup_manager = BackupManager(config)
        self._running = False
    
    def _parse_cron(self, cron_expr: str) -> dict:
        """
        解析 cron 表达式
        
        Args:
            cron_expr: cron 表达式 (分 时 日 月 周)
            
        Returns:
            APScheduler 需要的参数字典
        """
        parts = cron_expr.split()
        
        if len(parts) != 5:
            raise ValueError(f"无效的 cron 表达式: {cron_expr}")
        
        return {
            'minute': parts[0],
            'hour': parts[1],
            'day': parts[2],
            'month': parts[3],
            'day_of_week': parts[4]
        }
    
    async def _backup_job(self) -> None:
        """定时备份任务"""
        logger.info("=" * 50)
        logger.info("定时备份任务开始执行")
        logger.info("=" * 50)
        
        try:
            summary = await self.backup_manager.run_backup()
            logger.info(
                f"定时备份完成: 成功 {summary.success_count}, "
                f"跳过 {summary.skipped_count}, "
                f"失败 {summary.failed_count}"
            )
        except Exception as e:
            logger.error(f"定时备份任务异常: {e}")
    
    def add_backup_job(self) -> None:
        """添加备份任务到调度器"""
        cron_params = self._parse_cron(self.config.backup.schedule)
        
        self.scheduler.add_job(
            self._backup_job,
            trigger=CronTrigger(**cron_params),
            id='backup_job',
            name='GitHub Star 备份任务',
            replace_existing=True
        )
        
        # 计算下次执行时间
        trigger = CronTrigger(**cron_params)
        next_run = trigger.get_next_fire_time(None, datetime.now())
        
        logger.info(f"定时任务已配置: {self.config.backup.schedule}")
        logger.info(f"下次执行时间: {next_run}")
    
    async def start(self, run_immediately: bool = False) -> None:
        """
        启动调度器
        
        Args:
            run_immediately: 是否立即执行一次备份
        """
        self._running = True
        
        # 添加定时任务
        self.add_backup_job()
        
        # 启动调度器
        self.scheduler.start()
        logger.info("调度器已启动")
        
        # 如果需要立即执行
        if run_immediately:
            logger.info("立即执行一次备份...")
            await self._backup_job()
        
        # 保持运行
        try:
            while self._running:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            logger.info("收到退出信号")
            self.stop()
    
    def stop(self) -> None:
        """停止调度器"""
        self._running = False
        self.scheduler.shutdown(wait=False)
        logger.info("调度器已停止")
    
    async def run_once(self) -> None:
        """立即执行一次备份（不启动定时任务）"""
        logger.info("执行单次备份...")
        await self._backup_job()


async def run_scheduler(config: AppConfig, run_immediately: bool = False) -> None:
    """
    运行调度器
    
    Args:
        config: 应用配置
        run_immediately: 是否立即执行一次备份
    """
    scheduler = BackupScheduler(config)
    
    # 设置信号处理
    def signal_handler(signum, frame):
        logger.info(f"收到信号 {signum}，正在关闭...")
        scheduler.stop()
    
    if sys.platform != 'win32':
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    
    await scheduler.start(run_immediately=run_immediately)


async def run_once(config: AppConfig) -> None:
    """
    执行单次备份
    
    Args:
        config: 应用配置
    """
    scheduler = BackupScheduler(config)
    await scheduler.run_once()
