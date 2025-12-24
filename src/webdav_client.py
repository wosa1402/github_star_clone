"""
WebDAV 客户端模块

负责与 Alist WebDAV 服务交互，上传备份文件。
"""

from pathlib import Path
from typing import Optional

from loguru import logger
from webdav3.client import Client
from webdav3.exceptions import WebDavException

from .config import WebDAVConfig


class WebDAVClient:
    """WebDAV 客户端类"""
    
    def __init__(self, config: WebDAVConfig):
        """
        初始化 WebDAV 客户端
        
        Args:
            config: WebDAV 配置
        """
        self.config = config
        self.base_path = config.base_path.rstrip('/')
        
        # 配置 WebDAV 客户端
        options = {
            'webdav_hostname': config.url,
            'webdav_login': config.username,
            'webdav_password': config.password,
            'webdav_timeout': 120,  # 上传超时时间
        }
        
        self.client = Client(options)
    
    def test_connection(self) -> bool:
        """
        测试 WebDAV 连接
        
        Returns:
            连接是否成功
        """
        try:
            # 尝试列出根目录
            self.client.list("/")
            logger.info("WebDAV 连接成功")
            return True
        except WebDavException as e:
            logger.error(f"WebDAV 连接失败: {e}")
            return False
        except Exception as e:
            logger.error(f"WebDAV 连接异常: {e}")
            return False
    
    def ensure_directory(self, remote_path: str) -> bool:
        """
        确保远程目录存在
        
        Args:
            remote_path: 远程目录路径
            
        Returns:
            是否成功
        """
        path = remote_path.rstrip('/')
        if not path:
            return True
        
        try:
            # 检查目录是否存在
            if self.client.check(path):
                return True
            
            # 递归创建父目录
            parent = str(Path(path).parent).replace('\\', '/')
            if parent and parent != '/':
                self.ensure_directory(parent)
            
            # 创建当前目录
            self.client.mkdir(path)
            logger.debug(f"创建目录: {path}")
            return True
            
        except WebDavException as e:
            # 目录可能已存在
            if "already exists" in str(e).lower():
                return True
            logger.error(f"创建目录失败 {path}: {e}")
            return False
        except Exception as e:
            logger.error(f"创建目录异常 {path}: {e}")
            return False
    
    def get_remote_path(self, repo_full_name: str, filename: str) -> str:
        """
        获取远程存储路径
        
        Args:
            repo_full_name: 仓库完整名称 (owner/name)
            filename: 文件名
            
        Returns:
            远程路径
        """
        # 路径格式: base_path/owner/name/filename
        return f"{self.base_path}/{repo_full_name}/{filename}"
    
    def upload_file(
        self, 
        local_path: str, 
        repo_full_name: str,
        filename: str = None
    ) -> Optional[str]:
        """
        上传文件到 WebDAV
        
        Args:
            local_path: 本地文件路径
            repo_full_name: 仓库完整名称
            filename: 远程文件名（默认使用本地文件名）
            
        Returns:
            远程路径或 None（失败时）
        """
        local_file = Path(local_path)
        
        if not local_file.exists():
            logger.error(f"本地文件不存在: {local_path}")
            return None
        
        if filename is None:
            filename = local_file.name
        
        # 确保目录存在
        remote_dir = f"{self.base_path}/{repo_full_name}"
        if not self.ensure_directory(remote_dir):
            return None
        
        remote_path = f"{remote_dir}/{filename}"
        
        try:
            logger.info(f"上传文件: {local_file.name} -> {remote_path}")
            
            # 上传文件
            self.client.upload_sync(
                remote_path=remote_path,
                local_path=str(local_file)
            )
            
            file_size = local_file.stat().st_size
            logger.info(f"上传成功: {filename} ({file_size} bytes)")
            
            return remote_path
            
        except WebDavException as e:
            logger.error(f"上传失败: {e}")
            return None
        except Exception as e:
            logger.error(f"上传异常: {e}")
            return None
    
    def file_exists(self, remote_path: str) -> bool:
        """
        检查远程文件是否存在
        
        Args:
            remote_path: 远程路径
            
        Returns:
            是否存在
        """
        try:
            return self.client.check(remote_path)
        except Exception:
            return False
    
    def list_files(self, remote_dir: str) -> list[str]:
        """
        列出远程目录中的文件
        
        Args:
            remote_dir: 远程目录路径
            
        Returns:
            文件名列表
        """
        try:
            if not self.client.check(remote_dir):
                return []
            
            items = self.client.list(remote_dir)
            # 过滤掉目录本身
            return [item for item in items if item and item != '/']
        except Exception as e:
            logger.error(f"列出文件失败 {remote_dir}: {e}")
            return []
    
    def delete_file(self, remote_path: str) -> bool:
        """
        删除远程文件
        
        Args:
            remote_path: 远程文件路径
            
        Returns:
            是否成功
        """
        try:
            if self.client.check(remote_path):
                self.client.clean(remote_path)
                logger.debug(f"已删除远程文件: {remote_path}")
                return True
            return True  # 文件不存在也视为成功
        except Exception as e:
            logger.error(f"删除文件失败 {remote_path}: {e}")
            return False
    
    def get_backup_files(self, repo_full_name: str) -> list[str]:
        """
        获取仓库的所有备份文件
        
        Args:
            repo_full_name: 仓库完整名称
            
        Returns:
            备份文件名列表
        """
        remote_dir = f"{self.base_path}/{repo_full_name}"
        files = self.list_files(remote_dir)
        # 只返回 .bundle 文件
        return [f for f in files if f.endswith('.bundle')]
    
    def download_file(self, remote_path: str, local_path: str) -> bool:
        """
        从 WebDAV 下载文件
        
        Args:
            remote_path: 远程文件路径
            local_path: 本地保存路径
            
        Returns:
            是否成功
        """
        try:
            # 检查远程文件是否存在
            if not self.client.check(remote_path):
                logger.debug(f"远程文件不存在: {remote_path}")
                return False
            
            # 确保本地目录存在
            local_file = Path(local_path)
            local_file.parent.mkdir(parents=True, exist_ok=True)
            
            # 下载文件
            logger.info(f"下载文件: {remote_path} -> {local_path}")
            self.client.download_sync(
                remote_path=remote_path,
                local_path=str(local_file)
            )
            
            logger.info(f"下载成功: {local_file.name}")
            return True
            
        except WebDavException as e:
            logger.error(f"下载失败: {e}")
            return False
        except Exception as e:
            logger.error(f"下载异常: {e}")
            return False

