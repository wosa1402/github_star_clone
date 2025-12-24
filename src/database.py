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
            
            # 跳过仓库表（自动记录需要跳过的仓库）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS skipped_repos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    full_name TEXT UNIQUE NOT NULL,
                    skip_reason TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            
            # 备份进度表（用于断点续传）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS backup_progress (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    total_repos INTEGER,
                    current_index INTEGER DEFAULT 0,
                    last_repo_full_name TEXT,
                    status TEXT DEFAULT 'running',
                    started_at TEXT,
                    updated_at TEXT
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
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_skipped_repos_full_name 
                ON skipped_repos(full_name)
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
    
    # ========== 跳过仓库操作 ==========
    
    def add_skipped_repo(self, full_name: str, reason: str) -> None:
        """
        添加跳过的仓库记录
        
        Args:
            full_name: 仓库完整名称
            reason: 跳过原因
        """
        now = datetime.now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO skipped_repos (full_name, skip_reason, created_at, updated_at)
                VALUES (?, ?, COALESCE((SELECT created_at FROM skipped_repos WHERE full_name = ?), ?), ?)
            """, (full_name, reason, full_name, now, now))
        logger.info(f"已记录跳过仓库: {full_name}, 原因: {reason}")
    
    def is_repo_skipped(self, full_name: str) -> bool:
        """检查仓库是否在跳过列表中"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM skipped_repos WHERE full_name = ?",
                (full_name,)
            )
            return cursor.fetchone() is not None
    
    def get_skipped_repos(self) -> list[tuple[str, str]]:
        """获取所有跳过的仓库列表，返回 (full_name, reason) 元组"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT full_name, skip_reason FROM skipped_repos")
            return [(row['full_name'], row['skip_reason']) for row in cursor.fetchall()]
    
    def remove_skipped_repo(self, full_name: str) -> None:
        """从跳过列表中移除仓库"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM skipped_repos WHERE full_name = ?", (full_name,))
    
    # ========== 备份进度操作 ==========
    
    def save_backup_progress(
        self, 
        session_id: str, 
        total_repos: int, 
        current_index: int,
        last_repo_full_name: str,
        status: str = "running"
    ) -> None:
        """
        保存备份进度
        
        Args:
            session_id: 会话 ID
            total_repos: 总仓库数
            current_index: 当前处理索引
            last_repo_full_name: 最后处理的仓库名
            status: 状态 (running/completed/failed)
        """
        now = datetime.now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # 更新或插入进度记录
            cursor.execute("""
                INSERT OR REPLACE INTO backup_progress 
                (id, session_id, total_repos, current_index, last_repo_full_name, status, started_at, updated_at)
                VALUES (
                    (SELECT id FROM backup_progress WHERE session_id = ?),
                    ?, ?, ?, ?, ?,
                    COALESCE((SELECT started_at FROM backup_progress WHERE session_id = ?), ?),
                    ?
                )
            """, (session_id, session_id, total_repos, current_index, last_repo_full_name, status, session_id, now, now))
    
    def get_last_progress(self) -> Optional[dict]:
        """
        获取最后一次未完成的备份进度
        
        Returns:
            进度信息字典或 None
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM backup_progress 
                WHERE status = 'running'
                ORDER BY updated_at DESC 
                LIMIT 1
            """)
            row = cursor.fetchone()
            if row:
                return {
                    'session_id': row['session_id'],
                    'total_repos': row['total_repos'],
                    'current_index': row['current_index'],
                    'last_repo_full_name': row['last_repo_full_name'],
                    'status': row['status'],
                    'started_at': row['started_at'],
                    'updated_at': row['updated_at'],
                }
        return None
    
    def mark_progress_completed(self, session_id: str) -> None:
        """标记备份进度为已完成"""
        now = datetime.now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE backup_progress SET status = 'completed', updated_at = ? WHERE session_id = ?",
                (now, session_id)
            )

