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
        确保远程目录存在（兼容 AList 等 WebDAV 服务器）
        
        使用 requests 直接发送 MKCOL 请求，绕过 webdavclient3 的兼容性问题。
        
        Args:
            remote_path: 远程目录路径
            
        Returns:
            是否成功
        """
        import requests
        from requests.auth import HTTPBasicAuth
        
        path = remote_path.rstrip('/')
        if not path or path == '/':
            return True
        
        # 确保路径以 / 开头
        if not path.startswith('/'):
            path = '/' + path
        
        # 构建完整 URL
        base_url = self.config.url.rstrip('/')
        full_url = f"{base_url}{path}/"
        
        try:
            # 先递归确保父目录存在
            parent = str(Path(path).parent).replace('\\', '/')
            if parent and parent != '/' and parent != path:
                if not self.ensure_directory(parent):
                    # 父目录创建失败，继续尝试（可能只是已存在）
                    pass
            
            # 使用 MKCOL 方法创建目录
            auth = HTTPBasicAuth(self.config.username, self.config.password)
            response = requests.request(
                method='MKCOL',
                url=full_url,
                auth=auth,
                timeout=30
            )
            
            # 201 = 创建成功, 405 = 已存在或不支持, 301/302 = 重定向（已存在）
            if response.status_code in [201, 200]:
                logger.debug(f"创建目录成功: {path}")
                return True
            elif response.status_code in [405, 301, 302, 409]:
                # 目录已存在或其他可接受的状态
                logger.debug(f"目录已存在或已处理: {path} (状态码: {response.status_code})")
                return True
            else:
                logger.warning(f"创建目录返回状态码 {response.status_code}: {path}")
                # 继续尝试，不要因为创建目录失败就阻止上传
                return True
            
        except Exception as e:
            logger.warning(f"创建目录异常 {path}: {e}，将继续尝试上传")
            # 返回 True 继续尝试上传，让上传函数自己处理错误
            return True
    
    def _check_path_exists(self, path: str) -> bool:
        """
        检查路径是否存在（兼容方式）
        
        Args:
            path: 远程路径
            
        Returns:
            是否存在
        """
        try:
            # 尝试使用 check 方法
            return self.client.check(path)
        except Exception:
            # 如果 check 不支持，尝试用 list 父目录的方式
            try:
                parent = str(Path(path).parent).replace('\\', '/')
                name = Path(path).name
                items = self.client.list(parent)
                return name in items or f"{name}/" in items
            except Exception:
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
        import requests
        from requests.auth import HTTPBasicAuth
        
        local_file = Path(local_path)
        
        if not local_file.exists():
            logger.error(f"本地文件不存在: {local_path}")
            return None
        
        if filename is None:
            filename = local_file.name
        
        # 确保目录存在
        remote_dir = f"{self.base_path}/{repo_full_name}"
        self.ensure_directory(remote_dir)  # 不检查返回值，继续尝试上传
        
        # 构建远程路径
        remote_path = f"{remote_dir}/{filename}"
        if not remote_path.startswith('/'):
            remote_path = '/' + remote_path
        
        # 构建完整 URL
        base_url = self.config.url.rstrip('/')
        full_url = f"{base_url}{remote_path}"
        
        file_size = local_file.stat().st_size
        logger.info(f"上传文件: {local_file.name} ({file_size} bytes) -> {remote_path}")
        
        # 重试机制
        max_retries = 3
        retry_delay = 10  # 秒
        
        for attempt in range(max_retries):
            try:
                # 使用 requests 直接 PUT 上传文件
                auth = HTTPBasicAuth(self.config.username, self.config.password)
                
                with open(local_file, 'rb') as f:
                    response = requests.put(
                        url=full_url,
                        data=f,
                        auth=auth,
                        headers={'Content-Type': 'application/octet-stream'},
                        timeout=1800  # 30 分钟超时（大文件）
                    )
                
                if response.status_code in [200, 201, 204]:
                    logger.info(f"上传成功: {filename} ({file_size} bytes)")
                    return remote_path
                elif response.status_code == 405:
                    # 405 通常是目录问题，不重试
                    logger.error(f"上传失败: HTTP 405 - 请检查 WebDAV 配置")
                    return None
                else:
                    logger.warning(f"上传失败 (尝试 {attempt + 1}/{max_retries}): HTTP {response.status_code}")
                    
            except requests.exceptions.Timeout:
                logger.warning(f"上传超时 (尝试 {attempt + 1}/{max_retries}): {filename}")
            except requests.exceptions.SSLError as e:
                logger.warning(f"SSL 错误 (尝试 {attempt + 1}/{max_retries}): {e}")
            except Exception as e:
                logger.warning(f"上传异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            
            # 重试前等待
            if attempt < max_retries - 1:
                logger.info(f"等待 {retry_delay} 秒后重试...")
                import time
                time.sleep(retry_delay)
        
        logger.error(f"上传失败: 已重试 {max_retries} 次")
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
    
    def archive_backups(self, repo_full_name: str) -> bool:
        """
        归档仓库的所有现有备份文件到子目录
        
        当检测到仓库被 force push 导致历史丢失时调用。
        将所有现有的 bundle 文件移动到 archived_YYYYMMDD/ 子目录。
        
        Args:
            repo_full_name: 仓库完整名称
            
        Returns:
            是否成功
        """
        import requests
        from requests.auth import HTTPBasicAuth
        from datetime import datetime
        
        try:
            # 获取仓库目录
            repo_dir = f"{self.base_path}/{repo_full_name}"
            
            # 列出现有文件
            files = self.list_files(repo_dir)
            bundle_files = [f for f in files if f.endswith('.bundle')]
            
            if not bundle_files:
                logger.info(f"没有需要归档的备份文件: {repo_full_name}")
                return True
            
            # 创建归档目录
            timestamp = datetime.now().strftime("%Y%m%d")
            archive_dir = f"{repo_dir}/archived_{timestamp}"
            self.ensure_directory(archive_dir)
            
            # 移动文件到归档目录
            base_url = self.config.url.rstrip('/')
            auth = HTTPBasicAuth(self.config.username, self.config.password)
            
            for filename in bundle_files:
                src_path = f"{repo_dir}/{filename}"
                dst_path = f"{archive_dir}/{filename}"
                
                # 使用 MOVE 请求移动文件
                src_url = f"{base_url}{src_path}"
                dst_url = f"{base_url}{dst_path}"
                
                try:
                    response = requests.request(
                        method='MOVE',
                        url=src_url,
                        auth=auth,
                        headers={'Destination': dst_url, 'Overwrite': 'T'},
                        timeout=60
                    )
                    
                    if response.status_code in [200, 201, 204]:
                        logger.info(f"已归档: {filename} -> archived_{timestamp}/")
                    else:
                        # MOVE 可能不被支持，尝试复制后删除
                        logger.warning(f"移动文件失败 (HTTP {response.status_code})，尝试复制后删除")
                        # 这里不做复制，因为可能是大文件，让它保留在原位
                        
                except Exception as e:
                    logger.warning(f"移动文件失败 {filename}: {e}")
            
            logger.info(f"归档完成: {repo_full_name} -> archived_{timestamp}/")
            return True
            
        except Exception as e:
            logger.error(f"归档备份失败 {repo_full_name}: {e}")
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
        从 WebDAV 下载文件（使用 requests 兼容 AList）
        
        Args:
            remote_path: 远程文件路径
            local_path: 本地保存路径
            
        Returns:
            是否成功
        """
        import requests
        from requests.auth import HTTPBasicAuth
        
        try:
            # 确保路径格式正确
            if not remote_path.startswith('/'):
                remote_path = '/' + remote_path
            
            # 构建完整 URL
            base_url = self.config.url.rstrip('/')
            full_url = f"{base_url}{remote_path}"
            
            # 确保本地目录存在
            local_file = Path(local_path)
            local_file.parent.mkdir(parents=True, exist_ok=True)
            
            # 使用 requests 下载
            logger.info(f"下载文件: {remote_path} -> {local_path}")
            auth = HTTPBasicAuth(self.config.username, self.config.password)
            
            response = requests.get(
                url=full_url,
                auth=auth,
                stream=True,
                timeout=300
            )
            
            if response.status_code == 404:
                logger.debug(f"远程文件不存在: {remote_path}")
                return False
            
            if response.status_code != 200:
                logger.error(f"下载失败: HTTP {response.status_code}")
                return False
            
            # 流式写入本地文件
            with open(local_file, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            logger.info(f"下载成功: {local_file.name}")
            return True
            
        except Exception as e:
            logger.error(f"下载异常: {e}")
            return False


