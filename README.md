# GitHub Star 仓库备份工具

一个自动备份 GitHub Star 仓库到云存储的工具，支持增量备份、去重、定时任务和 Telegram 通知。

## 功能特性

- 🔄 **增量备份**：使用 Git Bundle 格式，支持完整备份和增量备份
- 📦 **多用户支持**：同时备份多个 GitHub 用户的 Star 仓库
- 🔀 **智能去重**：多用户 Star 同一仓库时只备份一次
- ⏰ **定时任务**：支持 Cron 表达式配置定时备份
- 📊 **更新检测**：只备份有更新的仓库，节省时间和空间
- ☁️ **云存储**：支持 WebDAV（兼容 Alist、坚果云等）
- 📱 **通知推送**：通过 Telegram 发送备份进度和结果
- ⚠️ **删库检测**：检测仓库删除并发送警告，保留本地备份
- 📝 **记录追踪**：记录仓库描述、备份历史等信息

## 系统要求

- Python 3.10+
- Git 2.20+
- 网络连接（访问 GitHub 和云存储）

## 安装

### 1. 克隆项目

```bash
git clone <本项目地址>
cd github_star_clone
```

### 2. 创建虚拟环境

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/macOS
source venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

## 配置

### 1. 复制配置文件

```bash
cp config.yaml.example config.yaml
```

### 2. 编辑 config.yaml

```yaml
# GitHub 配置
github:
  # 从 https://github.com/settings/tokens 获取
  # 需要权限：read:user, public_repo
  token: "ghp_xxxxxxxxxxxx"
  users:
    - "your_username"
    - "another_user"
  api_timeout: 30

# WebDAV 配置 (Alist)
webdav:
  url: "http://your-alist-server:5244/dav"
  username: "admin"
  password: "your_password"
  base_path: "/github-backup"

# Telegram 通知配置
telegram:
  # 从 @BotFather 创建 Bot 获取
  bot_token: "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
  # 发送 /start 给 @userinfobot 获取你的 Chat ID
  chat_id: "987654321"
  enabled: true

# 备份配置
backup:
  temp_dir: "./temp"
  db_path: "./data/backup.db"
  log_dir: "./logs"
  # Cron 表达式：分 时 日 月 周
  schedule: "0 6 * * *"  # 每天早上 6 点
  cleanup_temp: true
  max_retries: 3
```

### 3. 获取必要的凭据

#### GitHub Token

1. 访问 https://github.com/settings/tokens
2. 点击 "Generate new token (classic)"
3. 勾选权限：`read:user`, `public_repo`
4. 生成并保存 Token

#### Telegram Bot

1. 在 Telegram 搜索 `@BotFather`
2. 发送 `/newbot` 创建机器人
3. 获取 Bot Token
4. 搜索 `@userinfobot` 获取你的 Chat ID

#### Alist WebDAV

1. 确保 Alist 已启用 WebDAV 功能
2. WebDAV 地址通常是：`http://服务器IP:5244/dav`
3. 使用 Alist 的用户名和密码

## 使用方法

### 验证配置

```bash
python -m src.main --validate-config
```

### 测试连接

```bash
# 测试所有连接
python -m src.main --test

# 单独测试
python -m src.main --test-github
python -m src.main --test-webdav
python -m src.main --test-telegram
```

### 执行备份

```bash
# 执行一次备份
python -m src.main --once

# 备份单个仓库
python -m src.main --backup-single owner/repo
```

### 启动定时任务

```bash
# 启动定时任务（按配置的 cron 表达式执行）
python -m src.main

# 启动时立即执行一次
python -m src.main --run-now
```

### 其他选项

```bash
# 显示详细日志
python -m src.main --once -v

# 指定配置文件
python -m src.main -c /path/to/config.yaml --once
```

## 备份文件结构

云端存储结构：

```
github-backup/
├── owner1/
│   └── repo1/
│       ├── owner1_repo1_full_20241224_060000.bundle      # 完整备份
│       └── owner1_repo1_incr_20241225_060000_abc123.bundle  # 增量备份
├── owner2/
│   └── repo2/
│       └── ...
└── ...
```

## 恢复备份

### 从完整备份恢复

```bash
git clone repo_full_20241224.bundle my-repo
cd my-repo
git remote set-url origin https://github.com/owner/repo.git
```

### 应用增量备份

```bash
cd my-repo
git pull /path/to/repo_incr_20241225.bundle
```

## 部署到服务器

### 使用 systemd（Linux）

1. 创建服务文件 `/etc/systemd/system/github-backup.service`：

```ini
[Unit]
Description=GitHub Star Backup Service
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/github_star_clone
ExecStart=/path/to/venv/bin/python -m src.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

2. 启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable github-backup
sudo systemctl start github-backup
sudo systemctl status github-backup
```

### 使用 Screen/tmux

```bash
# 使用 screen
screen -S github-backup
python -m src.main
# Ctrl+A, D 离开

# 使用 tmux
tmux new -s github-backup
python -m src.main
# Ctrl+B, D 离开
```

## 通知示例

### 开始备份

> 🚀 **GitHub Star 备份开始**
>
> 📋 用户: user1, user2
> 📦 仓库数量: 50 个
> ⏰ 开始时间: 2024-12-24 06:00:00

### 备份完成

> ✅ **GitHub Star 备份完成**
>
> 📦 总仓库数: 50
> ✅ 成功: 10
> ⏭️ 跳过: 38
> ❌ 失败: 1
> ⚠️ 已删除: 1
> ⏱️ 耗时: 15.3 分钟
> ⏰ 完成时间: 2024-12-24 06:15:18

### 删库警告

> ⚠️ **仓库已删除警告**
>
> 📦 仓库: `owner/deleted-repo`
> 📝 描述: 这是一个已被删除的仓库
> 🔗 原链接: https://github.com/owner/deleted-repo
>
> 💾 本地备份已保留，不会删除。

## 故障排除

### GitHub API 限制

- 未认证：60 次/小时
- 已认证：5000 次/小时
- 如果遇到限制，工具会自动等待

### WebDAV 上传失败

1. 检查 Alist 服务是否正常运行
2. 验证用户名密码是否正确
3. 确认 base_path 目录有写入权限

### Telegram 发送失败

1. 确认 Bot Token 正确
2. 确认 Chat ID 正确
3. 确保已向 Bot 发送过 `/start` 消息

## 许可证

MIT License

## 更新日志

### v1.0.0 (2024-12-24)

- 初始版本
- 支持 GitHub Star 仓库备份
- 支持 WebDAV 云存储
- 支持 Telegram 通知
- 支持增量备份
- 支持定时任务
