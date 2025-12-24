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
    
    def __init__(self, config: AppConfig):
        """
        初始化备份管理器
        
        Args:
            config: 应用配置
        """
        self.config = config
        
        # 初始化各组件
        self.db = Database(config.backup.db_path)
        self.github = GitHubClient(config.github)
        self.git = GitOperations(config.backup.temp_dir)
        self.webdav = WebDAVClient(config.webdav)
        self.notifier = TelegramNotifier(config.telegram)
    
    async def run_backup(self) -> BackupSummary:
        """
        执行完整备份流程
        
        Returns:
            备份汇总
        """
        summary = BackupSummary(start_time=datetime.now())
        
        try:
            # 1. 发送开始通知
            logger.info("开始备份流程")
            
            # 2. 获取所有用户的 star 仓库
            all_repos = await self._gather_all_stars()
            
            # 3. 去重
            unique_repos = self._deduplicate_repos(all_repos)
            summary.total_repos = len(unique_repos)
            
            logger.info(f"共 {len(unique_repos)} 个唯一仓库待检查")
            
            # 发送开始通知
            await self.notifier.send_start_notification(
                summary.total_repos, 
                self.config.github.users
            )
            
            # 4. 单线程顺序备份
            for i, repo in enumerate(unique_repos, 1):
                logger.info(f"处理 [{i}/{summary.total_repos}]: {repo.full_name}")
                
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
                
                # 简单的速率控制
                await asyncio.sleep(1)
            
            # 5. 清理临时文件
            if self.config.backup.cleanup_temp:
                logger.info("清理临时文件...")
                # 只清理 bundle 文件，保留镜像以便下次增量更新
                # self.git.cleanup_all()
            
            # 6. 发送完成通知
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
            
            # 8. 清理本地 Bundle 文件
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
