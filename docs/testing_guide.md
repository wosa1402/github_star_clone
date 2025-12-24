# 测试指南

本文档提供了 GitHub Star 备份工具的完整测试指南。

## 测试前准备

### 1. 确保依赖已安装

```bash
pip install -r requirements.txt
```

### 2. 确保 Git 已安装

```bash
git --version
# 应该输出类似: git version 2.40.0
```

### 3. 准备测试配置

复制 `config.yaml` 并填入真实的配置信息。

## 连接测试

### 测试所有连接

```bash
python -m src.main --test
```

预期输出：

```
测试 GitHub 连接...
GitHub API 连接成功，当前用户: your_username
测试 WebDAV 连接...
WebDAV 连接成功
测试 Telegram 连接...
Telegram Bot 连接成功: @your_bot

连接测试结果:
----------------------------------------
  ✅ github: 成功
  ✅ webdav: 成功
  ✅ telegram: 成功
----------------------------------------
✅ 所有连接测试通过!
```

### 单独测试各组件

```bash
# GitHub API
python -m src.main --test-github

# WebDAV
python -m src.main --test-webdav

# Telegram
python -m src.main --test-telegram
```

## 配置验证测试

```bash
python -m src.main --validate-config
```

预期输出：

```
✅ 配置文件验证通过

配置摘要:
  GitHub 用户: user1, user2
  WebDAV 地址: http://your-server:5244/dav
  定时任务: 0 6 * * *
  Telegram 通知: 启用
```

### 配置错误测试

故意使用错误的配置测试错误处理：

1. **无效的 GitHub Token**
   ```yaml
   github:
     token: "invalid_token"
   ```
   预期：配置验证失败，提示 Token 无效

2. **缺少必填项**
   ```yaml
   github:
     users: []
   ```
   预期：配置验证失败，提示需要配置用户

## 单仓库备份测试

选择一个小型仓库进行测试：

```bash
# 选择一个小型公开仓库
python -m src.main --backup-single octocat/Hello-World
```

预期输出：

```
✅ 备份成功: octocat/Hello-World
   类型: full
   云端路径: /github-backup/octocat/Hello-World/octocat_Hello-World_full_20241224_120000.bundle
```

### 验证备份文件

1. 登录 Alist 网盘
2. 导航到 `/github-backup/octocat/Hello-World/`
3. 确认存在 `.bundle` 文件

### 验证 Bundle 完整性

下载 Bundle 文件到本地，然后验证：

```bash
git bundle verify octocat_Hello-World_full_20241224_120000.bundle
```

预期输出：

```
The bundle contains these 1 ref:
refs/heads/master
The bundle records a complete history.
octocat_Hello-World_full_20241224_120000.bundle is okay
```

## 完整备份流程测试

### 首次运行

```bash
python -m src.main --once -v
```

预期行为：

1. 获取所有用户的 Star 列表
2. 去重处理
3. 对每个仓库创建完整备份
4. 上传到 WebDAV
5. 发送 Telegram 通知

### 验证 Telegram 通知

检查 Telegram 是否收到：

1. 开始备份通知
2. 备份完成通知

### 第二次运行测试（增量备份）

等待几分钟后再次运行：

```bash
python -m src.main --once -v
```

预期行为：

1. 大部分仓库应该被跳过（无更新）
2. 只有有更新的仓库会创建增量备份
3. 日志中显示 "仓库无更新，跳过"

## 删库检测测试

由于无法真正删除 GitHub 仓库进行测试，可以通过以下方式模拟：

1. 在数据库中添加一个不存在的仓库记录
2. 运行备份，观察是否触发删库警告

```bash
# 备份一个不存在的仓库（会触发删库警告）
python -m src.main --backup-single nonexistent-user/nonexistent-repo
```

预期：

1. 输出显示仓库已删除
2. Telegram 收到删库警告通知

## 定时任务测试

### 短期测试

修改配置文件，设置一个近期的执行时间：

```yaml
backup:
  schedule: "*/5 * * * *"  # 每 5 分钟执行一次
```

启动服务：

```bash
python -m src.main
```

等待触发并观察日志。

### 验证定时任务配置

启动后查看日志输出：

```
定时任务已配置: */5 * * * *
下次执行时间: 2024-12-24 12:05:00
调度器已启动
```

## 错误处理测试

### 网络错误

1. 断开网络连接
2. 运行备份
3. 观察错误处理和重试机制

### WebDAV 服务不可用

1. 停止 Alist 服务
2. 运行备份
3. 观察错误通知

### GitHub API 限流

使用无效或过期的 Token 测试限流处理。

## 性能测试

### 大量仓库测试

如果有大量 Star（100+），运行完整备份观察：

1. 内存使用情况
2. 磁盘空间使用
3. 备份总耗时
4. 网络带宽使用

### 大仓库测试

选择一个较大的仓库（如 linux 内核）测试：

```bash
# 警告：这会消耗大量时间和空间
python -m src.main --backup-single torvalds/linux
```

## 数据库验证

备份完成后，验证数据库记录：

```bash
sqlite3 data/backup.db
```

```sql
-- 查看仓库记录
SELECT * FROM repositories LIMIT 10;

-- 查看备份记录
SELECT * FROM backup_records ORDER BY backup_time DESC LIMIT 10;

-- 查看 Star 来源
SELECT r.full_name, s.github_user 
FROM repositories r 
JOIN star_sources s ON r.id = s.repo_id 
LIMIT 10;
```

## 日志验证

检查日志文件：

```bash
# 查看今日日志
cat logs/backup_$(date +%Y-%m-%d).log

# 查看错误日志
cat logs/error_$(date +%Y-%m-%d).log
```

## 测试检查清单

- [ ] 配置验证通过
- [ ] GitHub API 连接正常
- [ ] WebDAV 连接正常
- [ ] Telegram 发送正常
- [ ] 单仓库备份成功
- [ ] Bundle 文件验证通过
- [ ] 完整备份流程正常
- [ ] 增量备份正常（跳过无更新仓库）
- [ ] 删库检测和通知正常
- [ ] 定时任务配置正确
- [ ] 数据库记录正确
- [ ] 日志记录正常

## 常见问题

### Q: 备份很慢怎么办？

A: 首次备份需要克隆所有仓库，耗时较长。后续增量备份会快很多。

### Q: 磁盘空间不足？

A: 确保 `cleanup_temp: true`，备份后会自动清理 Bundle 文件。镜像目录会保留以支持增量备份。

### Q: GitHub API 限流？

A: 确保使用有效的 Token（5000 次/小时）。遇到限流时工具会自动等待。

### Q: WebDAV 上传超时？

A: 大文件上传可能超时，考虑：
1. 提高网络带宽
2. 检查 Alist 配置
3. 分批备份（减少单次备份数量）
