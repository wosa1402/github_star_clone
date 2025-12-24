"""
Telegram 通知模块

负责发送备份进度和结果通知。
"""

import asyncio
from typing import Optional

from loguru import logger
from telegram import Bot
from telegram.error import TelegramError

from .config import TelegramConfig
from .models import BackupResult, BackupSummary, Repository


class TelegramNotifier:
    """Telegram 通知类"""
    
    def __init__(self, config: TelegramConfig):
        """
        初始化 Telegram 通知器
        
        Args:
            config: Telegram 配置
        """
        self.config = config
        self.enabled = config.enabled
        
        if self.enabled:
            self.bot = Bot(token=config.bot_token)
        else:
            self.bot = None
    
    async def _send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        发送消息
        
        Args:
            text: 消息内容
            parse_mode: 解析模式
            
        Returns:
            是否发送成功
        """
        if not self.enabled or not self.bot:
            logger.debug(f"Telegram 通知已禁用，跳过: {text[:50]}...")
            return True
        
        try:
            await self.bot.send_message(
                chat_id=self.config.chat_id,
                text=text,
                parse_mode=parse_mode
            )
            logger.debug("Telegram 消息发送成功")
            return True
        except TelegramError as e:
            logger.error(f"Telegram 消息发送失败: {e}")
            return False
        except Exception as e:
            logger.error(f"Telegram 发送异常: {e}")
            return False
    
    async def send_start_notification(self, total_repos: int, users: list[str]) -> bool:
        """
        发送备份开始通知
        
        Args:
            total_repos: 待检查的仓库总数
            users: 用户列表
            
        Returns:
            是否发送成功
        """
        users_str = ", ".join(users)
        message = (
            "🚀 <b>GitHub Star 备份开始</b>\n\n"
            f"📋 用户: {users_str}\n"
            f"📦 仓库数量: {total_repos} 个\n"
            f"⏰ 开始时间: {self._get_current_time()}\n\n"
            "正在检查更新..."
        )
        return await self._send_message(message)
    
    async def send_complete_notification(self, summary: BackupSummary) -> bool:
        """
        发送备份完成通知
        
        Args:
            summary: 备份汇总
            
        Returns:
            是否发送成功
        """
        # 构建状态统计
        status_items = []
        if summary.success_count > 0:
            status_items.append(f"✅ 成功: {summary.success_count}")
        if summary.skipped_count > 0:
            status_items.append(f"⏭️ 跳过: {summary.skipped_count}")
        if summary.failed_count > 0:
            status_items.append(f"❌ 失败: {summary.failed_count}")
        if summary.deleted_count > 0:
            status_items.append(f"⚠️ 已删除: {summary.deleted_count}")
        
        status_str = "\n".join(status_items) if status_items else "无任务执行"
        
        message = (
            "✅ <b>GitHub Star 备份完成</b>\n\n"
            f"📦 总仓库数: {summary.total_repos}\n"
            f"{status_str}\n"
            f"⏱️ 耗时: {summary.duration_str}\n"
            f"⏰ 完成时间: {self._get_current_time()}"
        )
        return await self._send_message(message)
    
    async def send_deleted_warning(self, repo: Repository) -> bool:
        """
        发送仓库删除警告
        
        Args:
            repo: 被删除的仓库
            
        Returns:
            是否发送成功
        """
        message = (
            "⚠️ <b>仓库已删除警告</b>\n\n"
            f"📦 仓库: <code>{repo.full_name}</code>\n"
            f"📝 描述: {repo.description or '无描述'}\n"
            f"🔗 原链接: {repo.html_url}\n\n"
            "💾 本地备份已保留，不会删除。"
        )
        return await self._send_message(message)
    
    async def send_error_notification(self, error_message: str, repo: Repository = None) -> bool:
        """
        发送错误通知
        
        Args:
            error_message: 错误信息
            repo: 相关仓库（可选）
            
        Returns:
            是否发送成功
        """
        if repo:
            message = (
                "❌ <b>备份错误</b>\n\n"
                f"📦 仓库: <code>{repo.full_name}</code>\n"
                f"❗ 错误: {error_message}\n"
                f"⏰ 时间: {self._get_current_time()}"
            )
        else:
            message = (
                "❌ <b>备份错误</b>\n\n"
                f"❗ 错误: {error_message}\n"
                f"⏰ 时间: {self._get_current_time()}"
            )
        return await self._send_message(message)
    
    async def send_progress_notification(
        self, 
        current: int, 
        total: int, 
        repo_name: str
    ) -> bool:
        """
        发送进度通知（可选，用于长时间备份时）
        
        Args:
            current: 当前进度
            total: 总数
            repo_name: 当前仓库名
            
        Returns:
            是否发送成功
        """
        progress = (current / total) * 100 if total > 0 else 0
        message = (
            "📊 <b>备份进度</b>\n\n"
            f"进度: {current}/{total} ({progress:.1f}%)\n"
            f"当前: {repo_name}"
        )
        return await self._send_message(message)
    
    async def test_connection(self) -> bool:
        """
        测试 Telegram 连接
        
        Returns:
            连接是否成功
        """
        if not self.enabled:
            logger.info("Telegram 通知已禁用")
            return True
        
        try:
            me = await self.bot.get_me()
            logger.info(f"Telegram Bot 连接成功: @{me.username}")
            
            # 发送测试消息
            test_message = (
                "🔔 <b>GitHub Star 备份工具</b>\n\n"
                "✅ 连接测试成功！\n"
                f"⏰ 时间: {self._get_current_time()}"
            )
            return await self._send_message(test_message)
            
        except TelegramError as e:
            logger.error(f"Telegram 连接失败: {e}")
            return False
    
    def _get_current_time(self) -> str:
        """获取当前时间字符串"""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DummyNotifier:
    """空通知器（用于禁用通知时）"""
    
    async def send_start_notification(self, *args, **kwargs) -> bool:
        return True
    
    async def send_complete_notification(self, *args, **kwargs) -> bool:
        return True
    
    async def send_deleted_warning(self, *args, **kwargs) -> bool:
        return True
    
    async def send_error_notification(self, *args, **kwargs) -> bool:
        return True
    
    async def send_progress_notification(self, *args, **kwargs) -> bool:
        return True
    
    async def test_connection(self) -> bool:
        return True
