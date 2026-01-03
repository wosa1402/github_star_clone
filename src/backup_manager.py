"""
备份管理器模块

负责协调整个备份流程，包括获取仓库列表、去重、更新检测和备份执行。
支持两种模式：
- 挂载模式（推荐）：使用 rclone 挂载 WebDAV，直接克隆仓库到挂载路径
- 上传模式：克隆仓库到本地，创建 Bundle 后上传到 WebDAV
"""

import asyncio
from datetime import datetime
from pathlib import Path
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
        self.mount = None  # WebDAV 挂载管理器
        self.use_mount_mode = config.backup.use_mount_mode
        
        # 初始化 WebDAV 客户端
        self.webdav = WebDAVClient(config.webdav)
        
        # 挂载模式：初始化挂载管理器
        if self.use_mount_mode:
            from .webdav_mount import WebDAVMount
            self.mount = WebDAVMount(config.webdav, config.backup.mount_point)
        
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
        支持挂载模式（直接克隆到 WebDAV）和上传模式（创建 Bundle 后上传）。
        
        Returns:
            备份汇总
        """
        import uuid
        
        summary = BackupSummary(start_time=datetime.now())
        session_id = str(uuid.uuid4())[:8]
        start_index = 0
        
        try:
            # 0. 挂载模式：挂载 WebDAV
            if self.use_mount_mode and self.mount:
                logger.info("挂载模式：正在挂载 WebDAV...")
                if not self.mount.mount():
                    logger.error("WebDAV 挂载失败，无法继续备份")
                    await self.notifier.send_error_notification("WebDAV 挂载失败，请检查 rclone 配置")
                    raise RuntimeError("WebDAV 挂载失败")
                logger.info(f"✅ WebDAV 已挂载到 {self.mount.mount_point}")
            
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
            storage_full = False  # 标记存储空间是否已满
            
            for i, repo in enumerate(unique_repos[start_index:], start_index + 1):
                # 检查存储空间是否已满
                if storage_full:
                    logger.warning("存储空间已满，停止备份")
                    break
                
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
                
                # 创建后台心跳任务（每 60 秒刷新一次进度通知）
                heartbeat_stop = asyncio.Event()
                
                async def heartbeat_task():
                    while not heartbeat_stop.is_set():
                        await asyncio.sleep(60)
                        if not heartbeat_stop.is_set():
                            await self.notifier.refresh_progress()
                            logger.debug("心跳刷新进度通知")
                
                heartbeat = asyncio.create_task(heartbeat_task())
                
                try:
                    result = await self._backup_single_repo(repo)
                finally:
                    # 停止心跳任务
                    heartbeat_stop.set()
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except asyncio.CancelledError:
                        pass
                
                summary.results.append(result)
                
                # 更新统计和状态
                status = "成功"
                if result.is_deleted:
                    summary.deleted_count += 1
                    status = "已删除"
                elif result.skipped:
                    summary.skipped_count += 1
                    status = "跳过"
                elif result.success:
                    summary.success_count += 1
                    status = "成功"
                else:
                    summary.failed_count += 1
                    status = "失败"
                    
                    # 检查是否是存储空间错误
                    if result.error_message:
                        if self._is_disk_error(result.error_message):
                            self.db.add_skipped_repo(repo.full_name, f"磁盘空间不足: {result.error_message[:100]}")
                        if self._is_storage_full_error(result.error_message):
                            storage_full = True
                            await self.notifier.send_error_notification(
                                "⚠️ WebDAV 存储空间已满，备份已停止！",
                                repo
                            )
                
                # 发送进度通知（每个仓库完成后）
                await self.notifier.send_progress_notification(
                    current=i,
                    total=summary.total_repos,
                    repo_name=repo.full_name,
                    success_count=summary.success_count,
                    skipped_count=summary.skipped_count,
                    failed_count=summary.failed_count,
                    status=status
                )
                
                # 只有真正执行了备份（成功上传）才等待 60 秒
                # 跳过和失败的仓库不需要等待
                if result.success and not result.skipped and not result.is_deleted:
                    if i < summary.total_repos and not storage_full:
                        logger.info("等待 60 秒后开始下一个仓库...")
                        await asyncio.sleep(60)
            
            # 7. 生成仓库描述索引文件
            logger.info("生成仓库描述索引文件...")
            await self._generate_repository_index()
            
            # 8. 标记备份完成
            self.db.mark_progress_completed(session_id)
            
            # 9. 备份数据库到云端
            logger.info("备份数据库到云端...")
            await self.backup_database()
            
            # 10. 发送完成通知
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
        """判断错误是否是本地磁盘空间不足或内存不足（OOM）"""
        disk_error_keywords = [
            # 磁盘空间不足
            "No space left on device",
            "no space left",
            "disk full",
            "not enough space",
            "磁盘空间不足",
            "out of disk space",
            "ENOSPC",
            # 内存不足 (OOM)
            "signal 9",  # OOM Killer
            "died of signal 9",
            "pack-objects died",
            "Cannot allocate memory",
            "out of memory",
            "oom",
            "内存不足",
        ]
        error_lower = error_message.lower()
        return any(kw.lower() in error_lower for kw in disk_error_keywords)
    
    def _is_storage_full_error(self, error_message: str) -> bool:
        """判断错误是否是 WebDAV 存储空间不足"""
        storage_error_keywords = [
            "insufficient storage",
            "quota exceeded",
            "507",  # HTTP 507 Insufficient Storage
            "storage full",
            "no space",
            "disk quota",
            "存储空间不足",
            "容量已满",
        ]
        error_lower = error_message.lower()
        return any(kw.lower() in error_lower for kw in storage_error_keywords)
    
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
        
        挂载模式：直接克隆/更新到 WebDAV 挂载路径
        上传模式：创建 Bundle 后上传到 WebDAV
        
        Args:
            repo: 仓库信息
            
        Returns:
            备份结果
        """
        result = BackupResult(repository=repo, success=False)
        mirror_created = False  # 标记是否创建了本地镜像（仅上传模式使用）
        
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
            
            clone_url = repo.clone_url or f"https://github.com/{repo.full_name}.git"
            
            # ========== 挂载模式 ==========
            if self.use_mount_mode and self.mount and self.mount.is_mounted:
                return await self._backup_mount_mode(repo, clone_url, result)
            
            # ========== 上传模式（Bundle）==========
            return await self._backup_upload_mode(repo, clone_url, result)
            
        except Exception as e:
            logger.error(f"备份失败 {repo.full_name}: {e}")
            result.error_message = str(e)
            await self.notifier.send_error_notification(str(e), repo)
        
        return result
    
    async def _backup_mount_mode(
        self, 
        repo: Repository, 
        clone_url: str,
        result: BackupResult
    ) -> BackupResult:
        """
        挂载模式备份：直接克隆/更新到 WebDAV 挂载路径
        
        Args:
            repo: 仓库信息
            clone_url: 克隆地址
            result: 备份结果对象
            
        Returns:
            备份结果
        """
        try:
            # 获取挂载路径上的仓库目标位置
            target_path = self.mount.get_repo_path(repo.full_name)
            
            # 确保 owner 目录存在
            owner = repo.full_name.split('/')[0]
            self.mount.ensure_owner_dir(owner)
            
            # 克隆或更新镜像到挂载路径
            logger.info(f"挂载模式备份: {repo.full_name} -> {target_path}")
            
            has_updates, current_commit = await self.git.clone_or_update_mirror(
                repo.full_name,
                clone_url,
                target_path=target_path
            )
            
            if not has_updates:
                logger.info(f"镜像无更新，跳过: {repo.full_name}")
                result.skipped = True
                result.success = True
                return result
            
            # 强制同步缓存到网络（确保数据完全写入）
            logger.info("正在同步数据到网络...")
            import subprocess
            subprocess.run(["sync"], check=False)
            
            # 等待同步完成（给 rclone 足够时间刷新缓存）
            await asyncio.sleep(10)
            logger.debug("数据同步完成")
            
            # 记录备份
            record = BackupRecord(
                repo_id=repo.id,
                bundle_name=f"{repo.full_name.replace('/', '_')}.git",
                bundle_type=BundleType.FULL,  # 挂载模式使用完整镜像
                commit_hash=current_commit,
                file_size=0,  # 挂载模式不计算大小
                cloud_path=str(target_path),
                backup_time=datetime.now()
            )
            self.db.save_backup_record(record)
            
            # 上传 metadata.json
            await self._upload_metadata_mount_mode(repo, current_commit, target_path)
            
            result.success = True
            result.bundle_type = BundleType.FULL
            result.cloud_path = str(target_path)
            
            logger.info(f"✅ 备份成功: {repo.full_name}")
            
        except Exception as e:
            logger.error(f"挂载模式备份失败 {repo.full_name}: {e}")
            result.error_message = str(e)
        
        return result
    
    async def _backup_upload_mode(
        self, 
        repo: Repository, 
        clone_url: str,
        result: BackupResult
    ) -> BackupResult:
        """
        上传模式备份：克隆到本地，创建 Bundle 后上传到 WebDAV
        
        Args:
            repo: 仓库信息
            clone_url: 克隆地址
            result: 备份结果对象
            
        Returns:
            备份结果
        """
        mirror_created = False
        
        try:
            # 检查是否需要备份
            latest_backup = self.db.get_latest_backup(repo.id)
            
            if latest_backup and repo.pushed_at:
                if latest_backup.backup_time:
                    # 统一转换为无时区格式进行比较
                    backup_time = latest_backup.backup_time
                    pushed_at = repo.pushed_at
                    
                    # 移除时区信息（如果有）
                    if backup_time.tzinfo is not None:
                        backup_time = backup_time.replace(tzinfo=None)
                    if pushed_at.tzinfo is not None:
                        pushed_at = pushed_at.replace(tzinfo=None)
                    
                    if backup_time >= pushed_at:
                        logger.info(f"仓库无更新，跳过: {repo.full_name}")
                        result.skipped = True
                        result.success = True
                        return result
            
            # 克隆仓库镜像
            has_updates, current_commit = await self.git.clone_or_update_mirror(
                repo.full_name, 
                clone_url
            )
            mirror_created = True
            
            if not has_updates and latest_backup:
                logger.info(f"镜像无更新，跳过: {repo.full_name}")
                result.skipped = True
                result.success = True
                self.git.cleanup_mirror(repo.full_name)
                return result
            
            # 创建 Bundle
            mirror_path = self.git.get_mirror_path(repo.full_name)
            
            if latest_backup and latest_backup.commit_hash:
                # 检查上次备份的 commit 是否还存在
                if self.git.commit_exists(mirror_path, latest_backup.commit_hash):
                    # commit 存在，创建增量备份
                    bundle_result = self.git.create_incremental_bundle(
                        repo.full_name,
                        latest_backup.commit_hash
                    )
                else:
                    # commit 不存在（仓库被 force push），归档旧备份并创建新的完整备份
                    logger.warning(f"检测到仓库历史重写，归档旧备份并创建新的完整备份: {repo.full_name}")
                    self.webdav.archive_backups(repo.full_name)
                    bundle_result = self.git.create_full_bundle(repo.full_name)
            else:
                bundle_result = self.git.create_full_bundle(repo.full_name)
            
            if not bundle_result.success:
                result.error_message = bundle_result.error_message
                return result
            
            if not bundle_result.bundle_path:
                result.skipped = True
                result.success = True
                return result
            
            # 上传到 WebDAV
            bundle_filename = bundle_result.bundle_path.split('/')[-1].split('\\')[-1]
            cloud_path = self.webdav.upload_file(
                bundle_result.bundle_path,
                repo.full_name,
                bundle_filename
            )
            
            if not cloud_path:
                result.error_message = "上传到 WebDAV 失败"
                return result
            
            # 记录备份
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
            
            # 上传 metadata.json
            await self._upload_metadata(repo, bundle_result.commit_hash, cloud_path)
            
            # 清理本地 Bundle 文件
            if self.config.backup.cleanup_temp:
                self.git.cleanup_bundle(bundle_result.bundle_path)
            
            result.success = True
            result.bundle_type = BundleType(bundle_result.bundle_type)
            result.bundle_path = bundle_result.bundle_path
            result.cloud_path = cloud_path
            
            logger.info(f"备份成功: {repo.full_name} -> {cloud_path}")
            
        except Exception as e:
            error_str = str(e)
            logger.error(f"上传模式备份失败 {repo.full_name}: {e}")
            result.error_message = error_str
            
            # 检测磁盘空间不足错误，自动添加到跳过列表
            if self._is_disk_error(error_str):
                logger.warning(f"检测到磁盘空间不足，将仓库添加到跳过列表: {repo.full_name}")
                self.db.add_skipped_repo(repo.full_name, "磁盘空间不足")
        
        finally:
            # 无论成功失败，都清理本地镜像和 Bundle
            if mirror_created:
                try:
                    self.git.cleanup_mirror(repo.full_name)
                    logger.debug(f"已清理镜像: {repo.full_name}")
                except Exception as cleanup_error:
                    logger.warning(f"清理镜像失败: {cleanup_error}")
            
            # 额外清理可能残留的 Bundle 文件
            try:
                import glob
                from pathlib import Path
                temp_bundles = glob.glob(str(Path(self.config.backup.temp_dir) / "bundles" / "*.bundle"))
                for bundle in temp_bundles:
                    Path(bundle).unlink(missing_ok=True)
                    logger.debug(f"清理残留 Bundle: {bundle}")
            except Exception:
                pass
        
        return result
    
    async def _upload_metadata_mount_mode(
        self, 
        repo: Repository, 
        commit_hash: str,
        target_path: Path
    ) -> None:
        """
        挂载模式下上传 metadata.json（直接写入挂载目录）
        """
        import json
        
        try:
            # 构建元数据
            star_sources = self.db.get_star_sources(repo.id) if repo.id else []
            
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
                "starred_by": star_sources,
            }
            
            # 直接写入挂载目录
            metadata_path = target_path.parent / "metadata.json"
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
            
            logger.debug(f"已写入 metadata.json: {metadata_path}")
            
        except Exception as e:
            logger.warning(f"写入 metadata.json 失败: {e}")
    
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
    
    async def _generate_repository_index(self) -> bool:
        """
        生成仓库描述索引文件，用于 AI 检索
        
        生成一个包含所有已备份仓库信息的 Markdown 文件，
        方便用户发送给 AI 来查找合适的工具。
        
        Returns:
            是否成功
        """
        import json
        import tempfile
        
        try:
            # 获取所有仓库
            repos = self.db.get_all_repositories()
            
            if not repos:
                logger.info("没有仓库记录，跳过索引生成")
                return True
            
            # 生成 Markdown 格式的索引
            lines = [
                "# GitHub Star 仓库索引",
                "",
                f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"总仓库数: {len(repos)}",
                "",
                "## 使用说明",
                "",
                "这是你备份的 GitHub Star 仓库列表，包含仓库名称和描述。",
                "你可以将此文件发送给 AI，帮助你找到适合某个需求的仓库。",
                "",
                "---",
                "",
            ]
            
            # 按仓库名称排序
            sorted_repos = sorted(repos, key=lambda r: r.full_name.lower())
            
            for repo in sorted_repos:
                # 跳过已删除的仓库
                if repo.is_deleted:
                    continue
                
                desc = repo.description or "无描述"
                # 清理描述中的特殊字符
                desc = desc.replace("\n", " ").replace("\r", " ").strip()
                
                lines.append(f"### {repo.full_name}")
                lines.append("")
                lines.append(f"- **链接**: https://github.com/{repo.full_name}")
                lines.append(f"- **描述**: {desc}")
                if repo.pushed_at:
                    lines.append(f"- **最后更新**: {repo.pushed_at.strftime('%Y-%m-%d')}")
                lines.append("")
            
            # 生成简洁版（仅名称和描述，用于快速检索）
            lines.append("---")
            lines.append("")
            lines.append("## 快速检索列表")
            lines.append("")
            lines.append("| 仓库 | 描述 |")
            lines.append("|------|------|")
            
            for repo in sorted_repos:
                if repo.is_deleted:
                    continue
                desc = (repo.description or "无描述")[:80]
                desc = desc.replace("|", "/").replace("\n", " ")
                lines.append(f"| {repo.full_name} | {desc} |")
            
            content = "\n".join(lines)
            
            # 写入临时文件
            with tempfile.NamedTemporaryFile(
                mode='w',
                suffix='.md',
                delete=False,
                encoding='utf-8'
            ) as f:
                f.write(content)
                temp_path = f.name
            
            # 上传到 WebDAV
            self.webdav.upload_file(
                temp_path,
                "_index",
                "repository_index.md"
            )
            
            # 同时生成 JSON 格式（便于程序处理）
            json_data = {
                "generated_at": datetime.now().isoformat(),
                "total_repos": len(repos),
                "repositories": [
                    {
                        "full_name": r.full_name,
                        "description": r.description,
                        "html_url": f"https://github.com/{r.full_name}",
                        "pushed_at": r.pushed_at.isoformat() if r.pushed_at else None,
                        "is_deleted": r.is_deleted,
                    }
                    for r in sorted_repos
                    if not r.is_deleted
                ]
            }
            
            with tempfile.NamedTemporaryFile(
                mode='w',
                suffix='.json',
                delete=False,
                encoding='utf-8'
            ) as f:
                json.dump(json_data, f, ensure_ascii=False, indent=2)
                json_temp_path = f.name
            
            self.webdav.upload_file(
                json_temp_path,
                "_index",
                "repository_index.json"
            )
            
            # 清理临时文件
            import os
            os.unlink(temp_path)
            os.unlink(json_temp_path)
            
            logger.info(f"仓库索引生成成功，共 {len(repos)} 个仓库")
            return True
            
        except Exception as e:
            logger.error(f"生成仓库索引失败: {e}")
            return False


