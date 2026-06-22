# DB Command Tool

一个通用数据库命令行工具，支持连接并执行 MSSQL、MySQL、Oracle SQL 命令，以及在数据库服务器上执行系统命令。

> 用途：数据库日常运维、巡检、授权测试、批量执行 SQL。  
> 注意：不要把密码、连接串、生产库地址提交到公开仓库。本工具默认不保存密码。

## 功能

- 支持 MySQL / MariaDB
- 支持 Microsoft SQL Server（MSSQL）
- 支持 Oracle Database
- 支持 PostgreSQL
- 支持 Redis（命令模式）
- 支持单条 SQL、SQL 文件、交互模式
- 支持表格、JSON、CSV 输出
- 可通过环境变量读取密码，减少命令行明文密码泄露风险
- 支持执行本机安全诊断命令，例如 `hostname`、`whoami`、`ping`、`ipconfig`、`netstat` 等
- 支持在数据库服务器上执行系统命令（通过 `xp_cmdshell`、Java、UDF 等数据库自身机制）
- 支持远程命令交互模式（`--remote-cmd -i`），连续执行多条服务器命令

## 安装

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

MSSQL 需要系统已安装 SQL Server ODBC Driver。Windows 常见驱动名为：

```text
ODBC Driver 18 for SQL Server
```

Oracle 默认使用 `python-oracledb` thin 模式，一般不需要 Oracle Client。需要 thick 模式时再使用 `--oracle-thick`。

## 基本用法

建议先把密码放到环境变量中：

```bash
export DB_PASSWORD='your_password'
# Windows PowerShell:
# $env:DB_PASSWORD='your_password'
```

### MySQL

```bash
python dbcli.py --type mysql --host 127.0.0.1 --port 3306 -u root -d test \
  -q "select version();"
```

### MSSQL

```bash
python dbcli.py --type mssql --host 127.0.0.1 --port 1433 -u sa -d master \
  --trust-server-certificate \
  -q "select @@version;"
```

如果使用 DSN：

```bash
python dbcli.py --type mssql --dsn MyMSSQLDsn -u sa -d master \
  -q "select name from sys.databases;"
```

### Oracle

使用 service name：

```bash
python dbcli.py --type oracle --host 127.0.0.1 --port 1521 -u system \
  --service-name ORCLPDB1 \
  -q "select * from v$version"
```

使用 SID：

```bash
python dbcli.py --type oracle --host 127.0.0.1 --port 1521 -u system \
  --sid ORCL \
  -q "select sysdate from dual"
```

### PostgreSQL

```bash
python dbcli.py --type postgresql --host 127.0.0.1 --port 5432 -u postgres -d postgres \
  -q "select version();"
```

### Redis

Redis 使用 Redis 命令而非 SQL，无需 `-u` 参数：

```bash
# 执行单条命令
python dbcli.py --type redis --host 127.0.0.1 --port 6379 \
  -q "INFO server"

# 交互模式
python dbcli.py --type redis --host 127.0.0.1 --port 6379 -i
```

Redis 交互模式：

```text
redis> PING
PONG
redis> KEYS *
(empty array)
redis> SET foo bar
OK
redis> GET foo
bar
redis> .exit
```

## 执行 SQL 文件

```bash
python dbcli.py --type mysql --host 127.0.0.1 -u root -d test \
  -f ./check.sql
```

默认会按分号拆分多条 SQL。复杂存储过程、PL/SQL 块、包含特殊分隔符的脚本，建议加 `--no-split` 或拆成单条执行。

## 交互模式（SQL）

```bash
python dbcli.py --type mysql --host 127.0.0.1 -u root -d test -i
```

进入后输入 SQL，并以分号结尾执行：

```sql
sql> select now();
sql> .exit
```

## 本机安全诊断命令

本工具支持通过 `--local-cmd` 执行运行脚本所在机器的安全诊断命令，不连接数据库，也不会通过数据库在远端数据库服务器上执行系统命令。

查看允许命令：

```bash
python dbcli.py --list-local-commands
```

执行本机命令：

```bash
python dbcli.py --local-cmd "hostname"
python dbcli.py --local-cmd "whoami"
python dbcli.py --local-cmd "ping 127.0.0.1"
```

JSON 输出：

```bash
python dbcli.py --local-cmd "hostname" --output json
```

保存输出：

```bash
python dbcli.py --local-cmd "ipconfig" --save local_network.txt
```

## 远程命令执行（在数据库服务器上）

通过 `--remote-cmd` 可以在连接的数据库服务器上执行系统命令，命令通过数据库自身机制在服务器端运行。

### 自动诊断模式（无需指定具体命令）

只加 `--remote-cmd` 不加命令值，工具自动连接数据库服务器并执行该模块的默认诊断命令集，快速获取服务器基本信息：

```bash
# MSSQL 自动诊断
python dbcli.py --type mssql --host 192.168.1.100 -u sa -d master \
  --enable-xp-cmdshell --remote-cmd

# MySQL 自动诊断
python dbcli.py --type mysql --host 192.168.1.100 -u root -d test \
  --remote-cmd

# Oracle 自动诊断
python dbcli.py --type oracle --host 192.168.1.100 -u system \
  --service-name ORCLPDB1 --remote-cmd

# PostgreSQL 自动诊断
python dbcli.py --type postgresql --host 192.168.1.100 -u postgres -d postgres \
  --remote-cmd

# Redis 自动诊断
python dbcli.py --type redis --host 192.168.1.100 --remote-cmd
```

执行效果示例：

```
未指定 --remote-cmd 命令，自动执行 MSSQL 服务器诊断命令集：
  → hostname
  → whoami
  → systeminfo
  → ipconfig
  → netstat -an
  → tasklist

正在连接 mssql 服务器 192.168.1.100 ...

[1/6] 执行：hostname
--------------------------------------------
远程命令：hostname
============================================================
db-server-01

[2/6] 执行：whoami
...
```

各数据库类型的默认诊断命令集：

| 数据库类型 | 自动执行的诊断命令 |
|-----------|-------------------|
| MSSQL | `hostname`, `whoami`, `systeminfo`, `ipconfig`, `netstat -an`, `tasklist` |
| MySQL | `hostname`, `whoami`, `id`, `uname -a`, `uptime`, `free -m`, `df -h`, `ip addr` |
| Oracle | `hostname`, `whoami`, `id`, `uname -a`, `uptime`, `free -m`, `df -h`, `ip addr` |
| PostgreSQL | `hostname`, `whoami`, `id`, `uname -a`, `uptime`, `free -m`, `df -h`, `ip addr` |
| Redis | `hostname`, `whoami`, `id`, `uname -a`, `uptime`, `free -m`, `df -h`, `ip addr` |

> 自动诊断模式同样支持 `--output json` 和 `--save` 参数。多条命令结果以 JSON 数组保存。

### 单条命令模式

```bash
# MSSQL（xp_cmdshell）
python dbcli.py --type mssql --host 192.168.1.100 -u sa -d master \
  --remote-cmd "ipconfig /all"

# 自动启用 xp_cmdshell（需系统管理员权限）
python dbcli.py --type mssql --host 192.168.1.100 -u sa -d master \
  --enable-xp-cmdshell --remote-cmd "whoami"

# MySQL（需预装 sys_exec UDF）
python dbcli.py --type mysql --host 192.168.1.100 -u root -d test \
  --remote-cmd "hostname"

# Oracle（Java stored procedure）
python dbcli.py --type oracle --host 192.168.1.100 -u system \
  --service-name ORCLPDB1 --remote-cmd "hostname"

# PostgreSQL（COPY FROM PROGRAM，需 superuser 权限）
python dbcli.py --type postgresql --host 192.168.1.100 -u postgres -d postgres \
  --remote-cmd "hostname"

# Redis（cron 注入方式，仅 Linux，需 root 权限）
python dbcli.py --type redis --host 192.168.1.100 --remote-cmd "hostname"
```

> **Redis 说明**：Redis 无原生命令执行机制，通过 `CONFIG SET + BGSAVE` 将命令写入 cron 作业实现。
> 前提条件：Linux 系统、`/etc/cron.d/` 可写、Redis 有 CONFIG SET 权限。
> 命令将在 60 秒内通过 cron 执行，输出写入服务器 `/tmp/_dbcli_rce_out`。

### 交互模式

加 `-i` 参数进入远程命令交互模式，连续执行多条命令：

```bash
python dbcli.py --type mssql --host 192.168.1.100 -u sa -d master \
  --enable-xp-cmdshell --remote-cmd -i
```

进入后：

```
正在连接 mssql 服务器 192.168.1.100 ...

已进入远程命令交互模式（mssql@192.168.1.100）
每行输入一个系统命令，在数据库服务器上执行。
输入 .exit 退出；输入 .help 查看帮助。

remote> whoami
mssql-srv\sa
remote> hostname
db-server-01
remote> ipconfig | findstr IPv4
   IPv4 Address. . . . . . . . . . . : 192.168.1.100
remote> .exit
```

### 输出格式

支持 JSON 输出和保存到文件：

```bash
python dbcli.py --type mssql --host 192.168.1.100 -u sa -d master \
  --remote-cmd "whoami" --output json

python dbcli.py --type mssql --host 192.168.1.100 -u sa -d master \
  --remote-cmd "dir C:\\" --save server_dir.txt

# 自动诊断模式结果保存
python dbcli.py --type mssql --host 192.168.1.100 -u sa -d master \
  --enable-xp-cmdshell --remote-cmd --save server_diag.txt
```

## 输出 JSON / CSV

JSON：

```bash
python dbcli.py --type mysql --host 127.0.0.1 -u root -d test \
  -q "select * from users limit 10" \
  --output json --save result.json
```

CSV：

```bash
python dbcli.py --type mysql --host 127.0.0.1 -u root -d test \
  -q "select * from users limit 10" \
  --output csv --save result.csv
```

## 关于 SQL 执行

本工具不拦截任何 SQL 语句，`SELECT`、`INSERT`、`UPDATE`、`DELETE`、`DROP` 等命令均可直接执行。
请确保在授权环境中使用，执行前确认 SQL 语句的正确性。

## 常用参数

```text
--type                      数据库类型：mysql、mssql、oracle、postgresql、redis
--host                      数据库主机
--port                      数据库端口
-u, --user                  数据库用户名
-p, --password              数据库密码，不建议明文使用
--password-env              密码环境变量，默认 DB_PASSWORD
-d, --database              数据库名；Oracle 可作为 service_name 使用
--dsn                       DSN 或连接标识
-q, --query                 执行单条 SQL
-f, --file                  执行 SQL 文件
-i, --interactive           交互模式（SQL 或配合 --remote-cmd 使用）
--max-rows                  每条查询最多返回行数，默认 200
--output table|json|csv     输出格式
--save                      输出保存路径
--local-cmd                 执行本机安全诊断命令
--local-timeout             本机命令超时时间
--list-local-commands       列出允许的本机诊断命令
--remote-cmd                在数据库服务器上执行系统命令；不加命令值时自动执行模块默认诊断命令集；加 -i 进入交互模式
--enable-xp-cmdshell        MSSQL：自动启用 xp_cmdshell（需系统管理员权限）
```

## 安全建议

1. 只在已授权的数据库环境使用。
2. 使用只读账号进行查询巡检。
3. 不要在命令行、脚本、仓库中写死密码。
4. 生产库执行 SQL 前先确认语句正确性，尽量在事务中执行并备份数据。
5. 公开仓库中不要提交 `.env`、连接串、导出的数据文件。
6. 远程命令执行（`--remote-cmd`）有较高风险，仅在已授权环境和维护窗口使用。
   - `xp_cmdshell` 启用后，具备对应权限的用户可执行任意系统命令。
   - Oracle/MySQL 的远程命令执行同样需要较高权限，使用后建议回收。
7. 如需远程主机执行系统命令，建议使用 SSH、WinRM、堡垒机、自动化平台等正规运维通道。
