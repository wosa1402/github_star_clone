"""
Git 操作模块

负责仓库克隆、Bundle 创建和管理。
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger


@dataclass
class BundleResult:
    """Bundle 创建结果"""
    success: bool
    bundle_path: Optional[str] = None
    bundle_type: Optional[str] = None  # full / incremental
    commit_hash: Optional[str] = None
    file_size: Optional[int] = None
    error_message: Optional[str] = None


class GitOperations:
    """Git 操作类"""
    
    def __init__(self, temp_dir: str):
        """
        初始化 Git 操作
        
        Args:
            temp_dir: 临时目录路径
        """
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        # 验证 git 是否可用
        self._verify_git()
    
    def _verify_git(self) -> None:
        """验证 Git 是否已安装"""
        try:
            result = subprocess.run(
                ["git", "--version"],
                capture_output=True,
                text=True,
                check=True
            )
            logger.debug(f"Git 版本: {result.stdout.strip()}")
        except FileNotFoundError:
            raise RuntimeError("Git 未安装或不在 PATH 中")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Git 验证失败: {e}")
    
    def _run_git_command(
        self, 
        args: list[str], 
        cwd: str = None,
        check: bool = True
    ) -> subprocess.CompletedProcess:
        """
        执行 Git 命令
        
        Args:
            args: 命令参数列表
            cwd: 工作目录
            check: 是否检查返回码
            
        Returns:
            执行结果
        """
        cmd = ["git"] + args
        logger.debug(f"执行 Git 命令: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=check,
                encoding='utf-8',
                errors='replace'
            )
            return result
        except subprocess.CalledProcessError as e:
            logger.error(f"Git 命令失败: {e.stderr}")
            raise
    
    def get_mirror_path(self, repo_full_name: str) -> Path:
        """
        获取仓库镜像的本地路径
        
        Args:
            repo_full_name: 仓库完整名称 (owner/name)
            
        Returns:
            本地路径
        """
        # 使用 owner_name.git 格式存储镜像
        safe_name = repo_full_name.replace('/', '_')
        return self.temp_dir / "mirrors" / f"{safe_name}.git"
    
    def clone_or_update_mirror(self, repo_full_name: str, clone_url: str) -> Tuple[bool, str]:
        """
        克隆或更新仓库镜像
        
        Args:
            repo_full_name: 仓库完整名称
            clone_url: 克隆地址
            
        Returns:
            (是否有更新, 最新 commit hash)
        """
        mirror_path = self.get_mirror_path(repo_full_name)
        
        if mirror_path.exists():
            # 已存在，执行 fetch 更新
            logger.info(f"更新仓库镜像: {repo_full_name}")
            
            # 获取更新前的 HEAD
            old_head = self._get_head_commit(mirror_path)
            
            # 执行 fetch
            self._run_git_command(
                ["fetch", "--all", "--prune"],
                cwd=str(mirror_path)
            )
            
            # 获取更新后的 HEAD
            new_head = self._get_head_commit(mirror_path)
            
            has_updates = old_head != new_head
            logger.debug(f"镜像更新完成，有更新: {has_updates}")
            
            return has_updates, new_head
        else:
            # 不存在，执行 clone --mirror
            logger.info(f"克隆仓库镜像: {repo_full_name}")
            
            mirror_path.parent.mkdir(parents=True, exist_ok=True)
            
            self._run_git_command([
                "clone", "--mirror", clone_url, str(mirror_path)
            ])
            
            new_head = self._get_head_commit(mirror_path)
            logger.debug(f"镜像克隆完成，HEAD: {new_head}")
            
            return True, new_head  # 新克隆视为有更新
    
    def _get_head_commit(self, repo_path: Path) -> Optional[str]:
        """获取仓库的 HEAD commit hash"""
        try:
            result = self._run_git_command(
                ["rev-parse", "HEAD"],
                cwd=str(repo_path)
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            # 可能是空仓库
            return None
    
    def _get_all_refs(self, repo_path: Path) -> list[str]:
        """获取仓库的所有引用"""
        try:
            result = self._run_git_command(
                ["show-ref", "--head"],
                cwd=str(repo_path),
                check=False  # 空仓库可能返回非零
            )
            if result.returncode == 0:
                refs = []
                for line in result.stdout.strip().split('\n'):
                    if line:
                        parts = line.split()
                        if len(parts) >= 2:
                            refs.append(parts[0])
                return refs
        except Exception as e:
            logger.warning(f"获取引用失败: {e}")
        return []
    
    def create_full_bundle(
        self, 
        repo_full_name: str,
        output_dir: str = None
    ) -> BundleResult:
        """
        创建完整备份 Bundle
        
        Args:
            repo_full_name: 仓库完整名称
            output_dir: 输出目录（默认为临时目录下的 bundles）
            
        Returns:
            BundleResult
        """
        mirror_path = self.get_mirror_path(repo_full_name)
        
        if not mirror_path.exists():
            return BundleResult(
                success=False,
                error_message=f"镜像不存在: {mirror_path}"
            )
        
        # 设置输出目录
        if output_dir:
            out_path = Path(output_dir)
        else:
            out_path = self.temp_dir / "bundles"
        out_path.mkdir(parents=True, exist_ok=True)
        
        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = repo_full_name.replace('/', '_')
        bundle_name = f"{safe_name}_full_{timestamp}.bundle"
        bundle_path = out_path / bundle_name
        
        try:
            # 创建 Bundle（使用绝对路径，因为 cwd 是镜像目录）
            self._run_git_command(
                ["bundle", "create", str(bundle_path.resolve()), "--all"],
                cwd=str(mirror_path)
            )
            
            # 验证 Bundle
            self._run_git_command(
                ["bundle", "verify", str(bundle_path.resolve())],
                cwd=str(mirror_path)
            )
            
            # 获取文件大小和 commit hash
            file_size = bundle_path.stat().st_size
            commit_hash = self._get_head_commit(mirror_path)
            
            logger.info(f"完整备份创建成功: {bundle_name} ({file_size} bytes)")
            
            return BundleResult(
                success=True,
                bundle_path=str(bundle_path),
                bundle_type="full",
                commit_hash=commit_hash,
                file_size=file_size
            )
            
        except subprocess.CalledProcessError as e:
            return BundleResult(
                success=False,
                error_message=f"Bundle 创建失败: {e.stderr}"
            )
    
    def create_incremental_bundle(
        self,
        repo_full_name: str,
        base_commit: str,
        output_dir: str = None
    ) -> BundleResult:
        """
        创建增量备份 Bundle
        
        Args:
            repo_full_name: 仓库完整名称
            base_commit: 基准 commit hash（上次备份的 commit）
            output_dir: 输出目录
            
        Returns:
            BundleResult
        """
        mirror_path = self.get_mirror_path(repo_full_name)
        
        if not mirror_path.exists():
            return BundleResult(
                success=False,
                error_message=f"镜像不存在: {mirror_path}"
            )
        
        # 检查是否有新提交
        current_head = self._get_head_commit(mirror_path)
        if current_head == base_commit:
            logger.info(f"无新提交，跳过增量备份: {repo_full_name}")
            return BundleResult(
                success=True,
                bundle_type="incremental",
                commit_hash=current_head,
                error_message="无新提交"
            )
        
        # 设置输出目录
        if output_dir:
            out_path = Path(output_dir)
        else:
            out_path = self.temp_dir / "bundles"
        out_path.mkdir(parents=True, exist_ok=True)
        
        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = repo_full_name.replace('/', '_')
        short_hash = current_head[:8] if current_head else "unknown"
        bundle_name = f"{safe_name}_incr_{timestamp}_{short_hash}.bundle"
        bundle_path = out_path / bundle_name
        
        try:
            # 创建增量 Bundle（使用绝对路径）
            # 使用 base_commit..HEAD 的范围，并包含所有分支
            self._run_git_command(
                ["bundle", "create", str(bundle_path.resolve()), f"{base_commit}..HEAD", "--all"],
                cwd=str(mirror_path)
            )
            
            # 获取文件大小
            file_size = bundle_path.stat().st_size
            
            logger.info(f"增量备份创建成功: {bundle_name} ({file_size} bytes)")
            
            return BundleResult(
                success=True,
                bundle_path=str(bundle_path),
                bundle_type="incremental",
                commit_hash=current_head,
                file_size=file_size
            )
            
        except subprocess.CalledProcessError as e:
            # 增量 Bundle 创建失败时，尝试创建完整备份
            logger.warning(f"增量备份失败，尝试完整备份: {e.stderr}")
            return self.create_full_bundle(repo_full_name, output_dir)
    
    def cleanup_mirror(self, repo_full_name: str) -> None:
        """
        清理仓库镜像
        
        Args:
            repo_full_name: 仓库完整名称
        """
        mirror_path = self.get_mirror_path(repo_full_name)
        if mirror_path.exists():
            shutil.rmtree(mirror_path)
            logger.debug(f"已清理镜像: {mirror_path}")
    
    def cleanup_bundle(self, bundle_path: str) -> None:
        """
        清理 Bundle 文件
        
        Args:
            bundle_path: Bundle 文件路径
        """
        path = Path(bundle_path)
        if path.exists():
            path.unlink()
            logger.debug(f"已清理 Bundle: {bundle_path}")
    
    def cleanup_all(self) -> None:
        """清理所有临时文件"""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            logger.info("已清理所有临时文件")
