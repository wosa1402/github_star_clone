"""
GitHub Star 仓库备份工具 - 主入口

支持的命令：
    python -m src.main                  # 启动定时任务
    python -m src.main --run-now        # 立即执行一次并启动定时任务
    python -m src.main --once           # 只执行一次，不启动定时任务
    python -m src.main --test           # 测试所有连接
    python -m src.main --test-github    # 测试 GitHub 连接
    python -m src.main --test-webdav    # 测试 WebDAV 连接
    python -m src.main --test-telegram  # 测试 Telegram 连接
    python -m src.main --backup-single owner/repo  # 备份单个仓库
    python -m src.main --validate-config  # 验证配置文件
"""

import argparse
import asyncio
import sys
import os
import fcntl
from pathlib import Path

from loguru import logger

from .backup_manager import BackupManager
from .config import init_config, AppConfig
from .scheduler import run_once, run_scheduler
from .utils import setup_logger


class ProcessLock:
    """进程锁，防止重复运行"""
    
    def __init__(self, lock_file: str = "/tmp/github_backup.lock"):
        self.lock_file = lock_file
        self.lock_fd = None
    
    def acquire(self) -> bool:
        """获取锁"""
        try:
            self.lock_fd = open(self.lock_file, 'w')
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # 写入 PID
            self.lock_fd.write(str(os.getpid()))
            self.lock_fd.flush()
            return True
        except (IOError, OSError):
            # 锁已被占用
            if self.lock_fd:
                self.lock_fd.close()
            return False
    
    def release(self):
        """释放锁"""
        if self.lock_fd:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                self.lock_fd.close()
                os.remove(self.lock_file)
            except Exception:
                pass
    
    def get_running_pid(self) -> int:
        """获取正在运行的进程 PID"""
        try:
            with open(self.lock_file, 'r') as f:
                return int(f.read().strip())
        except Exception:
            return 0
    
    def __enter__(self):
        if not self.acquire():
            return None
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="GitHub Star 仓库备份工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python -m src.main                    # 启动定时任务
    python -m src.main --run-now          # 立即执行并启动定时
    python -m src.main --once             # 只执行一次
    python -m src.main --test             # 测试所有连接
    python -m src.main --backup-single torvalds/linux  # 备份单个仓库
        """
    )
    
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="配置文件路径 (默认: config.yaml)"
    )
    
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="启动时立即执行一次备份"
    )
    
    parser.add_argument(
        "--once",
        action="store_true",
        help="只执行一次备份，不启动定时任务"
    )
    
    parser.add_argument(
        "--test",
        action="store_true",
        help="测试所有连接"
    )
    
    parser.add_argument(
        "--test-github",
        action="store_true",
        help="测试 GitHub API 连接"
    )
    
    parser.add_argument(
        "--test-webdav",
        action="store_true",
        help="测试 WebDAV 连接"
    )
    
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="测试 Telegram 连接"
    )
    
    parser.add_argument(
        "--backup-single",
        metavar="REPO",
        help="备份单个仓库 (格式: owner/name)"
    )
    
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="验证配置文件"
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细日志"
    )
    
    return parser.parse_args()


async def test_connections(config: AppConfig) -> bool:
    """测试所有连接"""
    manager = BackupManager(config)
    results = await manager.test_connections()
    
    print("\n连接测试结果:")
    print("-" * 40)
    
    all_ok = True
    for name, status in results.items():
        icon = "✅" if status else "❌"
        print(f"  {icon} {name}: {'成功' if status else '失败'}")
        if not status:
            all_ok = False
    
    print("-" * 40)
    
    if all_ok:
        print("✅ 所有连接测试通过!")
    else:
        print("❌ 部分连接测试失败，请检查配置")
    
    return all_ok


async def test_github(config: AppConfig) -> bool:
    """测试 GitHub 连接"""
    from .github_client import GitHubClient
    
    client = GitHubClient(config.github)
    result = await client.test_connection()
    
    if result:
        print("✅ GitHub API 连接成功")
    else:
        print("❌ GitHub API 连接失败")
    
    return result


async def test_webdav(config: AppConfig) -> bool:
    """测试 WebDAV 连接"""
    from .webdav_client import WebDAVClient
    
    client = WebDAVClient(config.webdav)
    result = client.test_connection()
    
    if result:
        print("✅ WebDAV 连接成功")
    else:
        print("❌ WebDAV 连接失败")
    
    return result


async def test_telegram(config: AppConfig) -> bool:
    """测试 Telegram 连接"""
    from .notifier import TelegramNotifier
    
    notifier = TelegramNotifier(config.telegram)
    result = await notifier.test_connection()
    
    if result:
        print("✅ Telegram 连接成功")
    else:
        print("❌ Telegram 连接失败")
    
    return result


async def backup_single(config: AppConfig, repo_name: str) -> bool:
    """备份单个仓库"""
    manager = BackupManager(config)
    result = await manager.backup_single(repo_name)
    
    if result.success:
        if result.skipped:
            print(f"⏭️ 仓库无更新，已跳过: {repo_name}")
        elif result.is_deleted:
            print(f"⚠️ 仓库已删除: {repo_name}")
        else:
            print(f"✅ 备份成功: {repo_name}")
            print(f"   类型: {result.bundle_type.value if result.bundle_type else 'N/A'}")
            print(f"   云端路径: {result.cloud_path}")
    else:
        print(f"❌ 备份失败: {repo_name}")
        print(f"   错误: {result.error_message}")
    
    return result.success


def validate_config(config_path: str) -> bool:
    """验证配置文件"""
    try:
        config = init_config(config_path)
        print("✅ 配置文件验证通过")
        print(f"\n配置摘要:")
        print(f"  GitHub 用户: {', '.join(config.github.users)}")
        print(f"  WebDAV 地址: {config.webdav.url}")
        print(f"  定时任务: {config.backup.schedule}")
        print(f"  Telegram 通知: {'启用' if config.telegram.enabled else '禁用'}")
        return True
    except FileNotFoundError as e:
        print(f"❌ 配置文件不存在: {e}")
        return False
    except ValueError as e:
        print(f"❌ 配置验证失败: {e}")
        return False
    except Exception as e:
        print(f"❌ 配置加载失败: {e}")
        return False


async def main() -> int:
    """主函数"""
    args = parse_args()
    
    # 验证配置
    if args.validate_config:
        return 0 if validate_config(args.config) else 1
    
    # 加载配置
    try:
        config = init_config(args.config)
    except FileNotFoundError:
        print(f"错误: 配置文件不存在: {args.config}")
        print("请复制 config.yaml.example 为 config.yaml 并修改配置")
        return 1
    except ValueError as e:
        print(f"配置错误: {e}")
        return 1
    
    # 设置日志
    log_level = "DEBUG" if args.verbose else "INFO"
    setup_logger(config.backup.log_dir, log_level)
    
    # 测试连接
    if args.test:
        result = await test_connections(config)
        return 0 if result else 1
    
    if args.test_github:
        result = await test_github(config)
        return 0 if result else 1
    
    if args.test_webdav:
        result = await test_webdav(config)
        return 0 if result else 1
    
    if args.test_telegram:
        result = await test_telegram(config)
        return 0 if result else 1
    
    # 备份单个仓库
    if args.backup_single:
        if '/' not in args.backup_single:
            print("错误: 仓库名格式应为 owner/name")
            return 1
        result = await backup_single(config, args.backup_single)
        return 0 if result else 1
    
    # 执行备份（需要获取进程锁）
    lock = ProcessLock()
    
    if not lock.acquire():
        running_pid = lock.get_running_pid()
        print(f"⚠️ 备份任务已在运行中 (PID: {running_pid})")
        print("如果确定没有运行，可以手动删除锁文件: rm /tmp/github_backup.lock")
        return 1
    
    try:
        logger.info(f"已获取进程锁 (PID: {os.getpid()})")
        
        if args.once:
            # 只执行一次
            await run_once(config)
        else:
            # 启动定时任务
            await run_scheduler(config, run_immediately=args.run_now)
    finally:
        lock.release()
        logger.info("已释放进程锁")
    
    return 0


def entry_point():
    """入口点"""
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"程序异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    entry_point()
