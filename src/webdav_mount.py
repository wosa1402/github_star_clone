"""
WebDAV 挂载模块

使用 rclone 自动挂载 WebDAV 到本地路径，支持直接备份仓库镜像。
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from .config import WebDAVConfig


class WebDAVMount:
    """WebDAV 挂载管理器"""
    
    def __init__(self, config: WebDAVConfig, mount_point: str = "/tmp/github-backup-mount"):
        """
        初始化挂载管理器
        
        Args:
            config: WebDAV 配置
            mount_point: 本地挂载点
        """
        self.config = config
        self.mount_point = Path(mount_point)
        self.rclone_remote_name = "github_backup_webdav"
        self._mounted = False
    
    def _check_rclone_installed(self) -> bool:
        """检查 rclone 是否已安装"""
        try:
            result = subprocess.run(
                ["rclone", "version"],
                capture_output=True,
                text=True,
                check=False
            )
            if result.returncode == 0:
                logger.debug(f"rclone 已安装: {result.stdout.split(chr(10))[0]}")
                return True
        except FileNotFoundError:
            pass
        return False
    
    def _install_rclone(self) -> bool:
        """自动安装 rclone"""
        logger.info("正在安装 rclone...")
        try:
            # 使用官方安装脚本
            result = subprocess.run(
                ["bash", "-c", "curl -fsSL https://rclone.org/install.sh | sudo bash"],
                capture_output=True,
                text=True,
                check=False
            )
            if result.returncode == 0:
                logger.info("rclone 安装成功")
                return True
            else:
                # 尝试使用 apt 安装
                result = subprocess.run(
                    ["sudo", "apt-get", "install", "-y", "rclone"],
                    capture_output=True,
                    text=True,
                    check=False
                )
                if result.returncode == 0:
                    logger.info("rclone 安装成功 (apt)")
                    return True
                logger.error(f"rclone 安装失败: {result.stderr}")
                return False
        except Exception as e:
            logger.error(f"安装 rclone 异常: {e}")
            return False
    
    def _configure_rclone(self) -> bool:
        """配置 rclone 远程"""
        logger.info("配置 rclone 远程...")
        
        # 解析 WebDAV URL
        url = self.config.url
        
        try:
            # 使用 rclone config create 命令创建配置
            result = subprocess.run(
                [
                    "rclone", "config", "create",
                    self.rclone_remote_name, "webdav",
                    "url", url,
                    "vendor", "other",
                    "user", self.config.username,
                    "pass", self.config.password,
                ],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "RCLONE_CONFIG_PASS": ""}
            )
            
            if result.returncode == 0:
                logger.info(f"rclone 远程 '{self.rclone_remote_name}' 配置成功")
                return True
            else:
                logger.error(f"rclone 配置失败: {result.stderr}")
                return False
                
        except Exception as e:
            logger.error(f"配置 rclone 异常: {e}")
            return False
    
    def mount(self) -> bool:
        """
        挂载 WebDAV 到本地路径
        
        Returns:
            是否成功
        """
        # 检查是否已挂载
        if self._is_mounted():
            logger.info(f"WebDAV 已挂载到 {self.mount_point}")
            self._mounted = True
            return True
        
        # 检查并安装 rclone
        if not self._check_rclone_installed():
            logger.info("rclone 未安装，尝试自动安装...")
            if not self._install_rclone():
                logger.error("无法安装 rclone，请手动安装: curl https://rclone.org/install.sh | sudo bash")
                return False
        
        # 配置 rclone 远程
        if not self._configure_rclone():
            return False
        
        # 创建挂载点
        self.mount_point.mkdir(parents=True, exist_ok=True)
        
        # 挂载 WebDAV
        logger.info(f"正在挂载 WebDAV 到 {self.mount_point}...")
        
        # 构建远程路径
        remote_path = f"{self.rclone_remote_name}:{self.config.base_path}"
        
        try:
            # 使用 rclone mount 挂载（后台运行）
            process = subprocess.Popen(
                [
                    "rclone", "mount",
                    remote_path,
                    str(self.mount_point),
                    "--vfs-cache-mode", "writes",  # 缓存写入
                    "--vfs-write-back", "1s",      # 快速写回
                    "--buffer-size", "32M",        # 缓冲区大小
                    "--dir-cache-time", "5s",      # 目录缓存时间
                    "--allow-non-empty",           # 允许挂载到非空目录
                    "--daemon",                    # 后台运行
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # 等待挂载完成
            time.sleep(3)
            
            if self._is_mounted():
                logger.info(f"✅ WebDAV 挂载成功: {self.mount_point}")
                self._mounted = True
                return True
            else:
                # 读取错误输出
                stderr = process.stderr.read() if process.stderr else ""
                logger.error(f"挂载失败: {stderr}")
                return False
                
        except Exception as e:
            logger.error(f"挂载异常: {e}")
            return False
    
    def _is_mounted(self) -> bool:
        """检查是否已挂载"""
        try:
            # 检查挂载点是否存在且可访问
            if not self.mount_point.exists():
                return False
            
            # 使用 mountpoint 命令检查
            result = subprocess.run(
                ["mountpoint", "-q", str(self.mount_point)],
                check=False
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def unmount(self) -> bool:
        """
        卸载 WebDAV
        
        Returns:
            是否成功
        """
        if not self._is_mounted():
            logger.debug("WebDAV 未挂载")
            return True
        
        logger.info(f"正在卸载 WebDAV: {self.mount_point}")
        
        try:
            # 使用 fusermount 卸载
            result = subprocess.run(
                ["fusermount", "-u", str(self.mount_point)],
                capture_output=True,
                text=True,
                check=False
            )
            
            if result.returncode == 0:
                logger.info("WebDAV 卸载成功")
                self._mounted = False
                return True
            else:
                # 尝试强制卸载
                result = subprocess.run(
                    ["fusermount", "-uz", str(self.mount_point)],
                    capture_output=True,
                    text=True,
                    check=False
                )
                if result.returncode == 0:
                    self._mounted = False
                    return True
                logger.error(f"卸载失败: {result.stderr}")
                return False
                
        except Exception as e:
            logger.error(f"卸载异常: {e}")
            return False
    
    def get_repo_path(self, repo_full_name: str) -> Path:
        """
        获取仓库在挂载路径中的位置
        
        Args:
            repo_full_name: 仓库完整名称 (owner/name)
            
        Returns:
            本地路径
        """
        # 格式: /mount_point/owner/name.git
        owner, name = repo_full_name.split('/', 1)
        return self.mount_point / owner / f"{name}.git"
    
    def ensure_owner_dir(self, owner: str) -> bool:
        """确保 owner 目录存在"""
        owner_dir = self.mount_point / owner
        try:
            owner_dir.mkdir(parents=True, exist_ok=True)
            return True
        except Exception as e:
            logger.error(f"创建目录失败 {owner_dir}: {e}")
            return False
    
    @property
    def is_mounted(self) -> bool:
        """是否已挂载"""
        return self._is_mounted()
