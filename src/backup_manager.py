"""
备份管理器模块

负责协调整个备份流程，包括获取仓库列表、去重、更新检测和备份执行。
"""

import asyncio
from datetime import datetime
from typing import Optional

from loguru import logger

from .config import AppConfig
from .database import Database
from .git_operations import GitOperations
from .github_client import GitHubClient
from .models import BackupRecord, BackupResult, BackupSummary, BundleType, Repository
from .notifier import TelegramNotifier
from .webdav_client import WebDAVClient


class BackupManager:
    """备份管理器"""
    
    def __init__(self, config: AppConfig, auto_restore_db: bool = True):
        """
        初始化备份管理器
        
        Args:
            config: 应用配置
            auto_restore_db: 是否自动从云端恢复数据库（默认开启）
        """
        self.config = config
        
        # 初始化 WebDAV 客户端（优先，用于恢复数据库）
        self.webdav = WebDAVClient(config.webdav)
        
        # 尝试从云端恢复数据库（如果本地不存在）
        if auto_restore_db:
            self._try_restore_database()
        
        # 初始化其他组件
        self.db = Database(config.backup.db_path)
        self.github = GitHubClient(config.github)
        self.git = GitOperations(config.backup.temp_dir)
        self.notifier = TelegramNotifier(config.telegram)
    
    def _try_restore_database(self) -> bool:
        """
        尝试从云端恢复数据库
        
        如果本地数据库不存在，则从 WebDAV 下载 latest.db
        
        Returns:
            是否恢复成功
        """
        from pathlib import Path
        
        db_path = Path(self.config.backup.db_path)
        
        # 如果本地数据库已存在，不需要恢复
        if db_path.exists():
            logger.debug("本地数据库已存在，无需恢复")
            return True
        
        logger.info("本地数据库不存在，尝试从云端恢复...")
        
        # 确保目录存在
        db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 尝试下载 latest.db
        remote_path = f"{self.webdav.base_path}/_database/latest.db"
        
        if self.webdav.download_file(remote_path, str(db_path)):
            logger.info("✅ 数据库恢复成功！")
            return True
        else:
            logger.info("云端无数据库备份，将创建新数据库")
            return False
    
    async def run_backup(self) -> BackupSummary:
        """
        执行完整备份流程
        
        支持断点续传和自动跳过配置/失败的仓库。
        
        Returns:
            备份汇总
        """
        import uuid
        
        summary = BackupSummary(start_time=datetime.now())
        session_id = str(uuid.uuid4())[:8]
        start_index = 0
        
        try:
            # 1. 发送开始通知
            logger.info("开始备份流程")
            
            # 2. 获取所有用户的 star 仓库
            all_repos = await self._gather_all_stars()
            
            # 3. 去重
            unique_repos = self._deduplicate_repos(all_repos)
            summary.total_repos = len(unique_repos)
            
            logger.info(f"共 {len(unique_repos)} 个唯一仓库待检查")
            
            # 4. 检查是否有未完成的备份（断点续传）
            if self.config.backup.resume_from_last:
                last_progress = self.db.get_last_progress()
                if last_progress:
                    # 找到上次中断的位置
                    last_repo = last_progress['last_repo_full_name']
                    for idx, repo in enumerate(unique_repos):
                        if repo.full_name == last_repo:
                            start_index = idx + 1  # 从下一个开始
                            break
                    
                    if start_index > 0:
                        logger.info(f"从断点继续: 跳过前 {start_index} 个仓库，从第 {start_index + 1} 个开始")
                        session_id = last_progress['session_id']  # 继续使用之前的 session
            
            # 发送开始通知
            remaining = len(unique_repos) - start_index
            await self.notifier.send_start_notification(
                remaining, 
                self.config.github.users
            )
            
            # 5. 获取跳过列表（配置文件 + 数据库记录）
            skip_set = set(self.config.backup.skip_repos)
            db_skipped = self.db.get_skipped_repos()
            for full_name, reason in db_skipped:
                skip_set.add(full_name)
            
            if skip_set:
                logger.info(f"跳过列表中有 {len(skip_set)} 个仓库")
            
            # 6. 单线程顺序备份
            for i, repo in enumerate(unique_repos[start_index:], start_index + 1):
                logger.info(f"处理 [{i}/{summary.total_repos}]: {repo.full_name}")
                
                # 检查是否在跳过列表中
                if repo.full_name in skip_set:
                    logger.info(f"跳过仓库（在跳过列表中）: {repo.full_name}")
                    result = BackupResult(repository=repo, success=True, skipped=True)
                    summary.results.append(result)
                    summary.skipped_count += 1
                    continue
                
                # 保存进度
                self.db.save_backup_progress(
                    session_id=session_id,
                    total_repos=summary.total_repos,
                    current_index=i,
                    last_repo_full_name=repo.full_name
                )
                
                result = await self._backup_single_repo(repo)
                summary.results.append(result)
                
                # 更新统计
                if result.is_deleted:
                    summary.deleted_count += 1
                elif result.skipped:
                    summary.skipped_count += 1
                elif result.success:
                    summary.success_count += 1
                else:
                    summary.failed_count += 1
                    # 检查是否是磁盘空间错误，如果是则自动添加到跳过列表
                    if result.error_message and self._is_disk_error(result.error_message):
                        self.db.add_skipped_repo(repo.full_name, f"磁盘空间不足: {result.error_message[:100]}")
                        await self.notifier.send_error_notification(
                            f"仓库 {repo.full_name} 因磁盘空间不足已加入跳过列表",
                            repo
                        )
                
                # 简单的速率控制
                await asyncio.sleep(1)
            
            # 7. 标记备份完成
            self.db.mark_progress_completed(session_id)
            
            # 8. 备份数据库到云端
            logger.info("备份数据库到云端...")
            await self.backup_database()
            
            # 9. 发送完成通知
            summary.end_time = datetime.now()
            await self.notifier.send_complete_notification(summary)
            
            logger.info(
                f"备份完成: 成功 {summary.success_count}, "
                f"跳过 {summary.skipped_count}, "
                f"失败 {summary.failed_count}, "
                f"已删除 {summary.deleted_count}"
            )
            
        except Exception as e:
            logger.error(f"备份流程异常: {e}")
            await self.notifier.send_error_notification(str(e))
            raise
        
        return summary
    
    def _is_disk_error(self, error_message: str) -> bool:
        """判断错误是否是磁盘空间不足"""
        disk_error_keywords = [
            "No space left on device",
            "no space left",
            "disk full",
            "not enough space",
            "磁盘空间不足",
            "out of disk space",
            "ENOSPC",
        ]
        error_lower = error_message.lower()
        return any(kw.lower() in error_lower for kw in disk_error_keywords)
    
    async def _gather_all_stars(self) -> list[tuple[Repository, str]]:
        """
        获取所有用户的 star 仓库
        
        Returns:
            (仓库, 来源用户) 元组列表
        """
        all_repos = []
        
        for user in self.config.github.users:
            logger.info(f"获取用户 {user} 的 star 列表...")
            try:
                repos = await self.github.get_all_starred_repos(user)
                for repo in repos:
                    all_repos.append((repo, user))
                logger.info(f"用户 {user} 共 {len(repos)} 个 star")
            except Exception as e:
                logger.error(f"获取用户 {user} 的 star 列表失败: {e}")
        
        return all_repos
    
    def _deduplicate_repos(self, repos_with_users: list[tuple[Repository, str]]) -> list[Repository]:
        """
        去重仓库列表
        
        多个用户 star 同一仓库时只保留一份，但记录所有来源用户。
        
        Args:
            repos_with_users: (仓库, 来源用户) 元组列表
            
        Returns:
            去重后的仓库列表
        """
        repo_map: dict[str, Repository] = {}
        
        for repo, user in repos_with_users:
            if repo.full_name not in repo_map:
                repo_map[repo.full_name] = repo
            
            # 保存或更新仓库到数据库
            repo_id = self.db.save_repository(repo)
            repo.id = repo_id
            
            # 记录 star 来源
            self.db.add_star_source(repo_id, user)
        
        logger.info(f"去重完成: {len(repos_with_users)} -> {len(repo_map)}")
        return list(repo_map.values())
    
    async def _backup_single_repo(self, repo: Repository) -> BackupResult:
        """
        备份单个仓库
        
        备份完成后会立即清理本地镜像以节省磁盘空间。
        
        Args:
            repo: 仓库信息
            
        Returns:
            备份结果
        """
        result = BackupResult(repository=repo, success=False)
        mirror_created = False  # 标记是否创建了镜像，用于清理
        
        try:
            # 1. 检查仓库是否还存在
            exists = await self.github.check_repository_exists(repo.full_name)
            
            if not exists:
                logger.warning(f"仓库已删除: {repo.full_name}")
                result.is_deleted = True
                
                # 标记为已删除
                self.db.mark_repository_deleted(repo.full_name)
                
                # 发送删除警告
                await self.notifier.send_deleted_warning(repo)
                
                result.success = True  # 删除检测成功
                return result
            
            # 2. 获取最新的仓库信息
            latest_info = await self.github.get_repository_info(repo.full_name)
            if latest_info:
                repo.pushed_at = latest_info.pushed_at
                repo.description = latest_info.description
                repo.clone_url = latest_info.clone_url
                self.db.save_repository(repo)
            
            # 3. 检查是否需要备份
            latest_backup = self.db.get_latest_backup(repo.id)
            
            if latest_backup and repo.pushed_at:
                # 比较上次备份时间和最新推送时间
                if latest_backup.backup_time and latest_backup.backup_time >= repo.pushed_at:
                    logger.info(f"仓库无更新，跳过: {repo.full_name}")
                    result.skipped = True
                    result.success = True
                    return result
            
            # 4. 克隆仓库镜像（每次都重新克隆以节省磁盘空间）
            clone_url = repo.clone_url or f"https://github.com/{repo.full_name}.git"
            has_updates, current_commit = self.git.clone_or_update_mirror(
                repo.full_name, 
                clone_url
            )
            mirror_created = True  # 标记已创建镜像
            
            if not has_updates and latest_backup:
                logger.info(f"镜像无更新，跳过: {repo.full_name}")
                result.skipped = True
                result.success = True
                # 清理镜像以节省磁盘空间
                self.git.cleanup_mirror(repo.full_name)
                return result
            
            # 5. 创建 Bundle
            if latest_backup and latest_backup.commit_hash:
                # 增量备份
                bundle_result = self.git.create_incremental_bundle(
                    repo.full_name,
                    latest_backup.commit_hash
                )
            else:
                # 完整备份
                bundle_result = self.git.create_full_bundle(repo.full_name)
            
            if not bundle_result.success:
                result.error_message = bundle_result.error_message
                return result
            
            if not bundle_result.bundle_path:
                # 无新提交
                result.skipped = True
                result.success = True
                return result
            
            # 6. 上传到 WebDAV
            bundle_filename = bundle_result.bundle_path.split('/')[-1].split('\\')[-1]
            cloud_path = self.webdav.upload_file(
                bundle_result.bundle_path,
                repo.full_name,
                bundle_filename
            )
            
            if not cloud_path:
                result.error_message = "上传到 WebDAV 失败"
                return result
            
            # 7. 记录备份
            record = BackupRecord(
                repo_id=repo.id,
                bundle_name=bundle_filename,
                bundle_type=BundleType(bundle_result.bundle_type),
                commit_hash=bundle_result.commit_hash,
                file_size=bundle_result.file_size,
                cloud_path=cloud_path,
                backup_time=datetime.now()
            )
            self.db.save_backup_record(record)
            
            # 8. 上传 metadata.json（包含仓库描述等信息）
            await self._upload_metadata(repo, bundle_result.commit_hash, cloud_path)
            
            # 9. 清理本地 Bundle 文件
            if self.config.backup.cleanup_temp:
                self.git.cleanup_bundle(bundle_result.bundle_path)
            
            result.success = True
            result.bundle_type = BundleType(bundle_result.bundle_type)
            result.bundle_path = bundle_result.bundle_path
            result.cloud_path = cloud_path
            
            logger.info(f"备份成功: {repo.full_name} -> {cloud_path}")
            
        except Exception as e:
            logger.error(f"备份失败 {repo.full_name}: {e}")
            result.error_message = str(e)
            await self.notifier.send_error_notification(str(e), repo)
        
        finally:
            # 无论成功与否，都清理镜像以节省磁盘空间
            if mirror_created:
                try:
                    self.git.cleanup_mirror(repo.full_name)
                    logger.debug(f"已清理镜像: {repo.full_name}")
                except Exception as cleanup_error:
                    logger.warning(f"清理镜像失败: {cleanup_error}")
        
        return result
    
    async def backup_single(self, repo_full_name: str) -> BackupResult:
        """
        备份单个指定仓库
        
        Args:
            repo_full_name: 仓库完整名称 (owner/name)
            
        Returns:
            备份结果
        """
        # 获取仓库信息
        repo_info = await self.github.get_repository_info(repo_full_name)
        
        if not repo_info:
            return BackupResult(
                repository=Repository(
                    owner=repo_full_name.split('/')[0],
                    name=repo_full_name.split('/')[1],
                    full_name=repo_full_name
                ),
                success=False,
                is_deleted=True,
                error_message="仓库不存在"
            )
        
        # 保存到数据库
        repo_id = self.db.save_repository(repo_info)
        repo_info.id = repo_id
        
        return await self._backup_single_repo(repo_info)
    
    async def test_connections(self) -> dict[str, bool]:
        """
        测试所有连接
        
        Returns:
            各连接的测试结果
        """
        results = {}
        
        # 测试 GitHub
        logger.info("测试 GitHub 连接...")
        results['github'] = await self.github.test_connection()
        
        # 测试 WebDAV
        logger.info("测试 WebDAV 连接...")
        results['webdav'] = self.webdav.test_connection()
        
        # 测试 Telegram
        logger.info("测试 Telegram 连接...")
        results['telegram'] = await self.notifier.test_connection()
        
        return results
    
    async def _upload_metadata(
        self, 
        repo: Repository, 
        commit_hash: str,
        bundle_cloud_path: str
    ) -> None:
        """
        上传仓库元信息 metadata.json
        
        Args:
            repo: 仓库信息
            commit_hash: 当前 commit hash
            bundle_cloud_path: Bundle 在云端的路径
        """
        import json
        import tempfile
        
        # 获取 star 来源用户
        star_sources = self.db.get_star_sources(repo.id) if repo.id else []
        
        # 构建元数据
        metadata = {
            "full_name": repo.full_name,
            "owner": repo.owner,
            "name": repo.name,
            "description": repo.description,
            "html_url": repo.html_url or f"https://github.com/{repo.full_name}",
            "clone_url": repo.clone_url,
            "pushed_at": repo.pushed_at.isoformat() if repo.pushed_at else None,
            "is_deleted": repo.is_deleted,
            "last_backup_commit": commit_hash,
            "last_backup_time": datetime.now().isoformat(),
            "last_backup_bundle": bundle_cloud_path,
            "starred_by": star_sources,
        }
        
        try:
            # 创建临时文件
            with tempfile.NamedTemporaryFile(
                mode='w', 
                suffix='.json', 
                delete=False,
                encoding='utf-8'
            ) as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
                temp_path = f.name
            
            # 上传到 WebDAV
            self.webdav.upload_file(
                temp_path,
                repo.full_name,
                "metadata.json"
            )
            
            # 清理临时文件
            import os
            os.unlink(temp_path)
            
            logger.debug(f"已上传 metadata.json: {repo.full_name}")
            
        except Exception as e:
            logger.warning(f"上传 metadata.json 失败: {e}")
            # 不影响主备份流程，仅记录警告
    
    async def backup_database(self) -> bool:
        """
        备份数据库文件到云端
        
        Returns:
            是否成功
        """
        import shutil
        from pathlib import Path
        
        db_path = Path(self.config.backup.db_path)
        
        if not db_path.exists():
            logger.warning(f"数据库文件不存在: {db_path}")
            return False
        
        try:
            # 生成备份文件名
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"backup_{timestamp}.db"
            
            # 复制数据库文件（避免锁定问题）
            temp_backup = db_path.parent / backup_name
            shutil.copy2(db_path, temp_backup)
            
            # 上传到 WebDAV 的 _database 目录
            cloud_path = self.webdav.upload_file(
                str(temp_backup),
                "_database",
                backup_name
            )
            
            # 同时上传一个 latest.db 作为最新版本
            self.webdav.upload_file(
                str(temp_backup),
                "_database",
                "latest.db"
            )
            
            # 清理临时文件
            temp_backup.unlink()
            
            if cloud_path:
                logger.info(f"数据库备份成功: {cloud_path}")
                return True
            else:
                logger.error("数据库上传失败")
                return False
                
        except Exception as e:
            logger.error(f"数据库备份失败: {e}")
            return False

