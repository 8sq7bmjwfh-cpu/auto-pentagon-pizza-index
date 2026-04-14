# PizzINT 自动监控

这个项目会在北京时间工作日 08:30 抓取 `https://www.pizzint.watch/` 首页公开内容，提取：

- Doughcon 等级
- Doughcon 文案
- 异常门店信息
- 全部门店信息

然后把结果发到邮箱，并把原始快照保存为 JSON 文件。

## 1. 本地运行

```bash
python -m venv .venv
source .venv/bin/activate   # Windows 改成 .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

把 `.env` 里的邮箱参数改掉后执行：

```bash
export $(grep -v '^#' .env | xargs)   # Windows 请手工设置环境变量
python pizzint_monitor.py
```

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

### 启用 QQ 邮箱 SMTP

1. 登录 QQ 邮箱网页版
2. 设置 -> 账户
3. 开启 SMTP/IMAP 服务
4. 生成 16 位授权码
5. 把授权码填到 `SMTP_PASS`

## 3. 手动测试

在 GitHub 仓库中打开：
`Actions -> PizzINT Daily Monitor -> Run workflow`

如果日志里没有报错，邮箱就会收到日报。

## 4. 重要说明

- GitHub Actions 的 cron 使用 UTC；这里已经换算成北京时间工作日 08:30。
- GitHub 定时任务通常接近设定时间执行，但不保证精确到秒。
- `pizzint.watch` 首页公开 HTML 当前可稳定解析到 `DOUGHCON` 和门店状态。
- 如果将来站点结构变化，可能需要调整解析规则。

## 5. 输出示例

程序会在 `output/` 生成：

- `latest.json`
- `pizzint_snapshot_YYYYMMDDTHHMMSSZ.json`

