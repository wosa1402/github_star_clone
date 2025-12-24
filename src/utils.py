"""
工具函数模块

提供日志配置、通用工具函数。
"""

import sys
from datetime import datetime
from pathlib import Path

from loguru import logger


def setup_logger(log_dir: str = "./logs", log_level: str = "INFO") -> None:
    """
    配置日志系统
    
    Args:
        log_dir: 日志目录
        log_level: 日志级别
    """
    # 移除默认处理器
    logger.remove()
    
    # 确保日志目录存在
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    
    # 控制台输出
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
               "<level>{message}</level>",
    )
    
    # 文件输出 - 按日期轮转
    log_file = Path(log_dir) / "backup_{time:YYYY-MM-DD}.log"
    logger.add(
        str(log_file),
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation="00:00",       # 每天轮转
        retention="30 days",    # 保留 30 天
        compression="zip",      # 压缩旧日志
        encoding="utf-8",
    )
    
    # 错误日志单独记录
    error_log_file = Path(log_dir) / "error_{time:YYYY-MM-DD}.log"
    logger.add(
        str(error_log_file),
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation="00:00",
        retention="90 days",
        compression="zip",
        encoding="utf-8",
    )
    
    logger.info("日志系统初始化完成")


def format_size(size_bytes: int) -> str:
    """
    格式化文件大小
    
    Args:
        size_bytes: 字节数
        
    Returns:
        格式化的大小字符串
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def format_datetime(dt: datetime) -> str:
    """
    格式化日期时间
    
    Args:
        dt: datetime 对象
        
    Returns:
        格式化的日期时间字符串
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def safe_filename(name: str) -> str:
    """
    将字符串转换为安全的文件名
    
    Args:
        name: 原始名称
        
    Returns:
        安全的文件名
    """
    # 替换不安全的字符
    unsafe_chars = '<>:"/\\|?*'
    for char in unsafe_chars:
        name = name.replace(char, '_')
    return name


def get_bundle_filename(repo_full_name: str, bundle_type: str, commit_hash: str = None) -> str:
    """
    生成 Bundle 文件名
    
    Args:
        repo_full_name: 仓库完整名称
        bundle_type: Bundle 类型 (full/incremental)
        commit_hash: commit hash（增量备份时使用）
        
    Returns:
        Bundle 文件名
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    repo_name = repo_full_name.replace('/', '_')
    
    if bundle_type == "full":
        return f"{repo_name}_full_{timestamp}.bundle"
    else:
        short_hash = commit_hash[:8] if commit_hash else "unknown"
        return f"{repo_name}_incr_{timestamp}_{short_hash}.bundle"
