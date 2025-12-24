"""
数据库操作模块

负责 SQLite 数据库的初始化、CRUD 操作。
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

from loguru import logger

from .models import BackupRecord, BundleType, Repository, StarSource


class Database:
    """数据库操作类"""
    
    def __init__(self, db_path: str):
        """
        初始化数据库
        
        Args:
            db_path: 数据库文件路径
        """
        self.db_path = db_path
        self._ensure_db_exists()
        self._init_tables()
    
    def _ensure_db_exists(self) -> None:
        """确保数据库文件和目录存在"""
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
    
    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """获取数据库连接的上下文管理器"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def _init_tables(self) -> None:
        """初始化数据库表"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 仓库信息表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS repositories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    name TEXT NOT NULL,
                    full_name TEXT UNIQUE NOT NULL,
                    description TEXT,
                    html_url TEXT,
                    clone_url TEXT,
                    pushed_at TEXT,
                    is_deleted INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            
            # 备份记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS backup_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_id INTEGER NOT NULL,
                    bundle_name TEXT NOT NULL,
                    bundle_type TEXT NOT NULL,
                    commit_hash TEXT,
                    file_size INTEGER,
                    cloud_path TEXT,
                    backup_time TEXT NOT NULL,
                    FOREIGN KEY (repo_id) REFERENCES repositories(id)
                )
            """)
            
            # Star 来源表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS star_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_id INTEGER NOT NULL,
                    github_user TEXT NOT NULL,
                    starred_at TEXT,
                    FOREIGN KEY (repo_id) REFERENCES repositories(id),
                    UNIQUE(repo_id, github_user)
                )
            """)
            
            # 创建索引
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_repositories_full_name 
                ON repositories(full_name)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_backup_records_repo_id 
                ON backup_records(repo_id)
            """)
            
            logger.debug("数据库表初始化完成")
    
    # ========== 仓库操作 ==========
    
    def get_repository_by_full_name(self, full_name: str) -> Optional[Repository]:
        """
        根据完整名称获取仓库
        
        Args:
            full_name: 仓库完整名称 (owner/name)
            
        Returns:
            Repository 或 None
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM repositories WHERE full_name = ?",
                (full_name,)
            )
            row = cursor.fetchone()
            if row:
                return self._row_to_repository(row)
        return None
    
    def save_repository(self, repo: Repository) -> int:
        """
        保存或更新仓库信息
        
        Args:
            repo: Repository 实例
            
        Returns:
            仓库 ID
        """
        now = datetime.now().isoformat()
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 检查是否已存在
            existing = self.get_repository_by_full_name(repo.full_name)
            
            if existing:
                # 更新
                cursor.execute("""
                    UPDATE repositories SET
                        description = ?,
                        html_url = ?,
                        clone_url = ?,
                        pushed_at = ?,
                        is_deleted = ?,
                        updated_at = ?
                    WHERE full_name = ?
                """, (
                    repo.description,
                    repo.html_url,
                    repo.clone_url,
                    repo.pushed_at.isoformat() if repo.pushed_at else None,
                    1 if repo.is_deleted else 0,
                    now,
                    repo.full_name,
                ))
                return existing.id
            else:
                # 插入
                cursor.execute("""
                    INSERT INTO repositories 
                    (owner, name, full_name, description, html_url, clone_url, 
                     pushed_at, is_deleted, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    repo.owner,
                    repo.name,
                    repo.full_name,
                    repo.description,
                    repo.html_url,
                    repo.clone_url,
                    repo.pushed_at.isoformat() if repo.pushed_at else None,
                    1 if repo.is_deleted else 0,
                    now,
                    now,
                ))
                return cursor.lastrowid
    
    def mark_repository_deleted(self, full_name: str) -> None:
        """标记仓库为已删除"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE repositories SET is_deleted = 1, updated_at = ? WHERE full_name = ?",
                (datetime.now().isoformat(), full_name)
            )
    
    def get_all_repositories(self) -> list[Repository]:
        """获取所有仓库"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM repositories")
            return [self._row_to_repository(row) for row in cursor.fetchall()]
    
    # ========== 备份记录操作 ==========
    
    def get_latest_backup(self, repo_id: int) -> Optional[BackupRecord]:
        """
        获取仓库的最新备份记录
        
        Args:
            repo_id: 仓库 ID
            
        Returns:
            BackupRecord 或 None
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM backup_records 
                WHERE repo_id = ? 
                ORDER BY backup_time DESC 
                LIMIT 1
            """, (repo_id,))
            row = cursor.fetchone()
            if row:
                return self._row_to_backup_record(row)
        return None
    
    def save_backup_record(self, record: BackupRecord) -> int:
        """
        保存备份记录
        
        Args:
            record: BackupRecord 实例
            
        Returns:
            记录 ID
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO backup_records 
                (repo_id, bundle_name, bundle_type, commit_hash, file_size, cloud_path, backup_time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                record.repo_id,
                record.bundle_name,
                record.bundle_type.value,
                record.commit_hash,
                record.file_size,
                record.cloud_path,
                record.backup_time.isoformat() if record.backup_time else datetime.now().isoformat(),
            ))
            return cursor.lastrowid
    
    def get_backup_history(self, repo_id: int, limit: int = 10) -> list[BackupRecord]:
        """获取仓库的备份历史"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM backup_records 
                WHERE repo_id = ? 
                ORDER BY backup_time DESC 
                LIMIT ?
            """, (repo_id, limit))
            return [self._row_to_backup_record(row) for row in cursor.fetchall()]
    
    # ========== Star 来源操作 ==========
    
    def add_star_source(self, repo_id: int, github_user: str) -> None:
        """
        添加 Star 来源记录
        
        Args:
            repo_id: 仓库 ID
            github_user: GitHub 用户名
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT OR IGNORE INTO star_sources (repo_id, github_user, starred_at)
                    VALUES (?, ?, ?)
                """, (repo_id, github_user, datetime.now().isoformat()))
            except sqlite3.IntegrityError:
                pass  # 已存在，忽略
    
    def get_star_sources(self, repo_id: int) -> list[str]:
        """获取仓库的所有 Star 来源用户"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT github_user FROM star_sources WHERE repo_id = ?",
                (repo_id,)
            )
            return [row['github_user'] for row in cursor.fetchall()]
    
    # ========== 辅助方法 ==========
    
    def _row_to_repository(self, row: sqlite3.Row) -> Repository:
        """将数据库行转换为 Repository 对象"""
        pushed_at = None
        if row['pushed_at']:
            pushed_at = datetime.fromisoformat(row['pushed_at'])
        
        created_at = None
        if row['created_at']:
            created_at = datetime.fromisoformat(row['created_at'])
        
        updated_at = None
        if row['updated_at']:
            updated_at = datetime.fromisoformat(row['updated_at'])
        
        return Repository(
            id=row['id'],
            owner=row['owner'],
            name=row['name'],
            full_name=row['full_name'],
            description=row['description'],
            html_url=row['html_url'],
            clone_url=row['clone_url'],
            pushed_at=pushed_at,
            is_deleted=bool(row['is_deleted']),
            created_at=created_at,
            updated_at=updated_at,
        )
    
    def _row_to_backup_record(self, row: sqlite3.Row) -> BackupRecord:
        """将数据库行转换为 BackupRecord 对象"""
        backup_time = None
        if row['backup_time']:
            backup_time = datetime.fromisoformat(row['backup_time'])
        
        return BackupRecord(
            id=row['id'],
            repo_id=row['repo_id'],
            bundle_name=row['bundle_name'],
            bundle_type=BundleType(row['bundle_type']),
            commit_hash=row['commit_hash'],
            file_size=row['file_size'],
            cloud_path=row['cloud_path'],
            backup_time=backup_time,
        )
