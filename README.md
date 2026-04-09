# PizzINT 自动监控

这个项目会在北京时间工作日 08:30 抓取 `https://www.pizzint.watch/` 首页公开内容，提取：

- 当前 Doughcon 等级
- 过去 12 小时 Doughcon 变化（含时间、等级、等级说明）
- 异常门店信息
- 全部门店信息

然后把结果发到邮箱，并把原始快照保存为 JSON 文件。

其中 Doughcon 等级说明按以下映射输出：

- `DOUGHCON 1: Maximum Readiness`
- `DOUGHCON 2: Next Step to Maximum Readiness`
- `DOUGHCON 3: Increase in Force Readiness`
- `DOUGHCON 4: Increased Intelligence Watch`
- `DOUGHCON 5: Lowest State of Readiness`

## 1. 本地运行

```bash
python -m venv .venv
source .venv/bin/activate   # Windows 改成 .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

把 `.env` 里的邮箱参数改掉后执行：

```bash
python pizzint_monitor.py
```

说明：脚本会自动读取当前目录下的 `.env`，无需再手工 `export` 环境变量。

## 2. GitHub Actions 自动运行

### 新建仓库并上传代码

把整个目录上传到 GitHub 仓库根目录。

### 配置 Secrets

进入仓库：
`Settings -> Secrets and variables -> Actions -> New repository secret`

创建 3 个 Secret：

- `SMTP_USER`
- `SMTP_PASS`
- `MAIL_TO`

说明：项目包含两个 workflow：

- `PizzINT Daily Monitor`：工作日北京时间 08:30 发送日报
- `PizzINT Doughcon Alert`：工作日每 15 分钟轮询，当 Doughcon 变化到 2 或 1 级时发送告警邮件

### 启用 QQ 邮箱 SMTP

1. 登录 QQ 邮箱网页版
2. 设置 -> 账户
3. 开启 SMTP/IMAP 服务
4. 生成 16 位授权码
5. 把授权码填到 `SMTP_PASS`

## 3. 手动测试

在 GitHub 仓库中打开：
`Actions -> PizzINT Daily Monitor -> Run workflow`（日报）
或
`Actions -> PizzINT Doughcon Alert -> Run workflow`（告警）

如果日志里没有报错，邮箱就会收到日报。

## 4. 重要说明

- GitHub Actions 的 cron 使用 UTC；这里已经换算成北京时间工作日 08:30。
- GitHub 定时任务通常接近设定时间执行，但不保证精确到秒。
- `pizzint.watch` 首页公开 HTML 当前可稳定解析到 `DOUGHCON` 和门店状态；过去 12 小时变化依赖页面公开历史信息中的时间表达（如 `x hours ago` 或可识别时间戳）。
- 如果将来站点结构变化，可能需要调整解析规则。

## 5. 输出示例

程序会在 `output/` 生成：

- `latest.json`
- `pizzint_snapshot_YYYYMMDDTHHMMSSZ.json`
