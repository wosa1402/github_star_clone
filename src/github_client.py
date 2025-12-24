"""
GitHub API 客户端

负责与 GitHub API 交互，获取用户 star 列表和仓库信息。
"""

import asyncio
from datetime import datetime
from typing import AsyncGenerator, Optional

import httpx
from loguru import logger

from .config import GitHubConfig
from .models import Repository


class GitHubClient:
    """GitHub API 客户端"""
    
    BASE_URL = "https://api.github.com"
    
    def __init__(self, config: GitHubConfig):
        """
        初始化 GitHub 客户端
        
        Args:
            config: GitHub 配置
        """
        self.config = config
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {config.token}",
            "User-Agent": "GitHub-Star-Backup-Tool",
        }
        self._rate_limit_remaining = 5000
        self._rate_limit_reset = None
    
    async def _request(
        self, 
        method: str, 
        endpoint: str, 
        **kwargs
    ) -> Optional[dict | list]:
        """
        发送 API 请求
        
        Args:
            method: HTTP 方法
            endpoint: API 端点
            **kwargs: 其他请求参数
            
        Returns:
            响应数据或 None
        """
        url = f"{self.BASE_URL}{endpoint}"
        
        async with httpx.AsyncClient(timeout=self.config.api_timeout) as client:
            try:
                response = await client.request(
                    method, 
                    url, 
                    headers=self.headers,
                    **kwargs
                )
                
                # 更新速率限制信息
                self._update_rate_limit(response)
                
                # 检查速率限制
                if response.status_code == 403 and self._rate_limit_remaining == 0:
                    wait_time = self._get_wait_time()
                    logger.warning(f"GitHub API 速率限制，等待 {wait_time} 秒")
                    await asyncio.sleep(wait_time)
                    return await self._request(method, endpoint, **kwargs)
                
                if response.status_code == 404:
                    return None
                
                response.raise_for_status()
                return response.json()
                
            except httpx.HTTPStatusError as e:
                logger.error(f"GitHub API 请求失败: {e}")
                raise
            except httpx.TimeoutException:
                logger.error(f"GitHub API 请求超时: {endpoint}")
                raise
    
    def _update_rate_limit(self, response: httpx.Response) -> None:
        """更新速率限制信息"""
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        
        if remaining:
            self._rate_limit_remaining = int(remaining)
        if reset:
            self._rate_limit_reset = datetime.fromtimestamp(int(reset))
        
        if self._rate_limit_remaining < 100:
            logger.warning(f"GitHub API 剩余请求次数: {self._rate_limit_remaining}")
    
    def _get_wait_time(self) -> int:
        """计算需要等待的时间（秒）"""
        if self._rate_limit_reset:
            wait = (self._rate_limit_reset - datetime.now()).total_seconds()
            return max(1, int(wait) + 1)
        return 60  # 默认等待 60 秒
    
    async def get_user_starred_repos(
        self, 
        username: str,
        per_page: int = 100
    ) -> AsyncGenerator[Repository, None]:
        """
        获取用户的 star 仓库列表
        
        Args:
            username: GitHub 用户名
            per_page: 每页数量
            
        Yields:
            Repository 对象
        """
        page = 1
        
        while True:
            endpoint = f"/users/{username}/starred"
            params = {"page": page, "per_page": per_page}
            
            logger.debug(f"获取 {username} 的 star 列表，第 {page} 页")
            
            data = await self._request("GET", endpoint, params=params)
            
            if not data:
                break
            
            for repo_data in data:
                yield Repository.from_github_api(repo_data)
            
            if len(data) < per_page:
                break
            
            page += 1
            
            # 简单的速率控制
            await asyncio.sleep(0.5)
    
    async def get_all_starred_repos(self, username: str) -> list[Repository]:
        """
        获取用户的所有 star 仓库
        
        Args:
            username: GitHub 用户名
            
        Returns:
            Repository 列表
        """
        repos = []
        async for repo in self.get_user_starred_repos(username):
            repos.append(repo)
        
        logger.info(f"获取到 {username} 的 {len(repos)} 个 star 仓库")
        return repos
    
    async def check_repository_exists(self, full_name: str) -> bool:
        """
        检查仓库是否存在
        
        Args:
            full_name: 仓库完整名称 (owner/name)
            
        Returns:
            是否存在
        """
        endpoint = f"/repos/{full_name}"
        data = await self._request("GET", endpoint)
        return data is not None
    
    async def get_repository_info(self, full_name: str) -> Optional[Repository]:
        """
        获取仓库详细信息
        
        Args:
            full_name: 仓库完整名称 (owner/name)
            
        Returns:
            Repository 或 None（如果仓库不存在）
        """
        endpoint = f"/repos/{full_name}"
        data = await self._request("GET", endpoint)
        
        if data:
            return Repository.from_github_api(data)
        return None
    
    async def get_latest_commit_hash(self, full_name: str, branch: str = None) -> Optional[str]:
        """
        获取仓库的最新 commit hash
        
        Args:
            full_name: 仓库完整名称
            branch: 分支名（默认为默认分支）
            
        Returns:
            commit hash 或 None
        """
        # 首先获取仓库信息以确定默认分支
        if not branch:
            repo_info = await self._request("GET", f"/repos/{full_name}")
            if not repo_info:
                return None
            branch = repo_info.get('default_branch', 'main')
        
        endpoint = f"/repos/{full_name}/commits/{branch}"
        data = await self._request("GET", endpoint)
        
        if data:
            return data.get('sha')
        return None
    
    async def test_connection(self) -> bool:
        """
        测试 GitHub API 连接
        
        Returns:
            连接是否成功
        """
        try:
            data = await self._request("GET", "/user")
            if data:
                logger.info(f"GitHub API 连接成功，当前用户: {data.get('login')}")
                return True
        except Exception as e:
            logger.error(f"GitHub API 连接失败: {e}")
        return False
