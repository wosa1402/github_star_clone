"""
配置管理模块

负责加载和验证配置文件，提供配置项的类型安全访问。
"""

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


class GitHubConfig(BaseModel):
    """GitHub 配置"""
    token: str = Field(..., description="GitHub Personal Access Token")
    users: list[str] = Field(default_factory=list, description="要备份的用户列表")
    api_timeout: int = Field(default=30, description="API 超时时间（秒）")
    
    @field_validator('token')
    @classmethod
    def validate_token(cls, v: str) -> str:
        """验证 Token 格式"""
        if not v or v == "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx":
            raise ValueError("请配置有效的 GitHub Token")
        return v
    
    @field_validator('users')
    @classmethod
    def validate_users(cls, v: list[str]) -> list[str]:
        """验证用户列表"""
        if not v or v == ["example_user1", "example_user2"]:
            raise ValueError("请配置要备份的 GitHub 用户名")
        return v


class WebDAVConfig(BaseModel):
    """WebDAV 配置"""
    url: str = Field(..., description="WebDAV 服务器地址")
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")
    base_path: str = Field(default="/github-backup", description="基础存储路径")
    
    @field_validator('url')
    @classmethod
    def validate_url(cls, v: str) -> str:
        """验证 URL 格式"""
        if not v or "your-alist-server" in v:
            raise ValueError("请配置有效的 WebDAV 服务器地址")
        return v.rstrip('/')


class TelegramConfig(BaseModel):
    """Telegram 配置"""
    bot_token: str = Field(..., description="Bot Token")
    chat_id: str = Field(..., description="Chat ID")
    enabled: bool = Field(default=True, description="是否启用通知")
    
    @field_validator('bot_token')
    @classmethod
    def validate_bot_token(cls, v: str) -> str:
        """验证 Bot Token"""
        if not v or v == "123456789:ABCdefGHIjklMNOpqrsTUVwxyz":
            raise ValueError("请配置有效的 Telegram Bot Token")
        return v


class BackupConfig(BaseModel):
    """备份配置"""
    temp_dir: str = Field(default="./temp", description="临时文件目录")
    db_path: str = Field(default="./data/backup.db", description="数据库路径")
    log_dir: str = Field(default="./logs", description="日志目录")
    schedule: str = Field(default="0 6 * * *", description="定时备份 cron 表达式")
    cleanup_temp: bool = Field(default=True, description="是否清理临时文件")
    max_retries: int = Field(default=3, description="最大重试次数")
    retry_delay: int = Field(default=10, description="重试间隔（秒）")
    # 跳过仓库列表（格式：owner/repo）
    skip_repos: list[str] = Field(default_factory=list, description="要跳过的仓库列表")
    # 断点续传：从上次中断的位置继续
    resume_from_last: bool = Field(default=True, description="是否从上次中断处继续")
    # 挂载模式：直接将仓库镜像备份到 WebDAV 挂载路径
    # 注意：挂载模式对 Git 操作不稳定，建议使用上传模式
    use_mount_mode: bool = Field(default=False, description="使用挂载模式（不推荐）")
    # 挂载点路径（仅 Linux）
    mount_point: str = Field(default="/tmp/github-backup-mount", description="WebDAV 挂载点")


class AppConfig(BaseModel):
    """应用总配置"""
    github: GitHubConfig
    webdav: WebDAVConfig
    telegram: TelegramConfig
    backup: BackupConfig
    
    @classmethod
    def load(cls, config_path: str = "config.yaml") -> "AppConfig":
        """
        从 YAML 文件加载配置
        
        Args:
            config_path: 配置文件路径
            
        Returns:
            AppConfig 实例
            
        Raises:
            FileNotFoundError: 配置文件不存在
            ValueError: 配置验证失败
        """
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        return cls(**data)
    
    def ensure_directories(self) -> None:
        """确保必要的目录存在"""
        dirs = [
            self.backup.temp_dir,
            self.backup.log_dir,
            Path(self.backup.db_path).parent,
        ]
        for dir_path in dirs:
            Path(dir_path).mkdir(parents=True, exist_ok=True)


# 全局配置实例
_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """获取全局配置实例"""
    global _config
    if _config is None:
        raise RuntimeError("配置未初始化，请先调用 init_config()")
    return _config


def init_config(config_path: str = "config.yaml") -> AppConfig:
    """
    初始化全局配置
    
    Args:
        config_path: 配置文件路径
        
    Returns:
        AppConfig 实例
    """
    global _config
    _config = AppConfig.load(config_path)
    _config.ensure_directories()
    return _config
