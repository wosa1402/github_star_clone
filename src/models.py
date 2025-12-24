"""
数据模型定义

定义仓库信息、备份记录等核心数据结构。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class BundleType(str, Enum):
    """Bundle 类型"""
    FULL = "full"           # 完整备份
    INCREMENTAL = "incremental"  # 增量备份


@dataclass
class Repository:
    """仓库信息"""
    owner: str                          # 仓库所有者
    name: str                           # 仓库名称
    full_name: str                      # 完整名称 (owner/name)
    description: Optional[str] = None   # 仓库描述
    html_url: Optional[str] = None      # GitHub 链接
    clone_url: Optional[str] = None     # 克隆地址
    pushed_at: Optional[datetime] = None  # 最后推送时间
    is_deleted: bool = False            # 是否已删除
    id: Optional[int] = None            # 数据库 ID
    created_at: Optional[datetime] = None   # 记录创建时间
    updated_at: Optional[datetime] = None   # 记录更新时间
    
    @classmethod
    def from_github_api(cls, data: dict) -> "Repository":
        """
        从 GitHub API 响应创建仓库对象
        
        Args:
            data: GitHub API 返回的仓库数据
            
        Returns:
            Repository 实例
        """
        pushed_at = None
        if data.get('pushed_at'):
            pushed_at = datetime.fromisoformat(data['pushed_at'].replace('Z', '+00:00'))
        
        return cls(
            owner=data['owner']['login'],
            name=data['name'],
            full_name=data['full_name'],
            description=data.get('description'),
            html_url=data.get('html_url'),
            clone_url=data.get('clone_url'),
            pushed_at=pushed_at,
        )


@dataclass
class BackupRecord:
    """备份记录"""
    repo_id: int                        # 关联仓库 ID
    bundle_name: str                    # Bundle 文件名
    bundle_type: BundleType             # Bundle 类型
    commit_hash: Optional[str] = None   # 备份时的最新 commit
    file_size: Optional[int] = None     # 文件大小（字节）
    cloud_path: Optional[str] = None    # 云端存储路径
    backup_time: Optional[datetime] = None  # 备份时间
    id: Optional[int] = None            # 数据库 ID


@dataclass
class StarSource:
    """Star 来源记录"""
    repo_id: int                        # 关联仓库 ID
    github_user: str                    # Star 来源用户
    starred_at: Optional[datetime] = None  # Star 时间
    id: Optional[int] = None            # 数据库 ID


@dataclass
class BackupResult:
    """单个仓库的备份结果"""
    repository: Repository
    success: bool
    bundle_type: Optional[BundleType] = None
    bundle_path: Optional[str] = None
    cloud_path: Optional[str] = None
    error_message: Optional[str] = None
    skipped: bool = False               # 是否跳过（无更新）
    is_deleted: bool = False            # 仓库是否已删除


@dataclass
class BackupSummary:
    """备份任务汇总"""
    total_repos: int = 0                # 总仓库数
    success_count: int = 0              # 成功数量
    skipped_count: int = 0              # 跳过数量（无更新）
    failed_count: int = 0               # 失败数量
    deleted_count: int = 0              # 已删除仓库数量
    start_time: Optional[datetime] = None   # 开始时间
    end_time: Optional[datetime] = None     # 结束时间
    results: list[BackupResult] = field(default_factory=list)
    
    @property
    def duration_seconds(self) -> float:
        """备份耗时（秒）"""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0.0
    
    @property
    def duration_str(self) -> str:
        """格式化的耗时字符串"""
        seconds = self.duration_seconds
        if seconds < 60:
            return f"{seconds:.1f} 秒"
        elif seconds < 3600:
            return f"{seconds / 60:.1f} 分钟"
        else:
            return f"{seconds / 3600:.1f} 小时"
