# DB Command Tool

一个通用数据库命令行工具，支持连接并执行 MSSQL、MySQL、Oracle SQL 命令。

> 用途：数据库日常运维、巡检、授权测试、批量执行 SQL。  
> 注意：不要把密码、连接串、生产库地址提交到公开仓库。本工具默认不保存密码。

## 功能

- 支持 MySQL / MariaDB
- 支持 Microsoft SQL Server（MSSQL）
- 支持 Oracle Database
- 支持单条 SQL、SQL 文件、交互模式
- 支持表格、JSON、CSV 输出
- 默认拦截 `UPDATE`、`DELETE`、`INSERT`、`DROP` 等非只读 SQL，避免误操作
- 可通过环境变量读取密码，减少命令行明文密码泄露风险
- 支持执行本机安全诊断命令，例如 `hostname`、`whoami`、`ping`、`ipconfig`、`netstat` 等
- 支持在数据库服务器上执行系统命令（通过 `xp_cmdshell`、Java、UDF 等数据库自身机制）

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

## 执行 SQL 文件

```bash
python dbcli.py --type mysql --host 127.0.0.1 -u root -d test \
  -f ./check.sql
```

默认会按分号拆分多条 SQL。复杂存储过程、PL/SQL 块、包含特殊分隔符的脚本，建议加 `--no-split` 或拆成单条执行。

## 交互模式

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

### MSSQL（xp_cmdshell）

```bash
# 基本用法（需要 xp_cmdshell 已启用）
python dbcli.py --type mssql --host 192.168.1.100 -u sa -d master \
  --remote-cmd "ipconfig /all"

# 自动启用 xp_cmdshell（需要系统管理员权限）
python dbcli.py --type mssql --host 192.168.1.100 -u sa -d master \
  --enable-xp-cmdshell \
  --remote-cmd "whoami"
```

### MySQL（sys_exec UDF）

需要数据库已安装 `lib_mysqludf_sys` 插件：

```sql
-- 在 MySQL 服务器上提前安装
CREATE FUNCTION sys_exec RETURNS INTEGER SONAME 'lib_mysqludf_sys.so';
```

然后使用：

```bash
python dbcli.py --type mysql --host 192.168.1.100 -u root -d test \
  --remote-cmd "hostname"
```

### Oracle（Java stored procedure）

需要数据库已安装 JVM 并有 `CREATE JAVA` / `CREATE PROCEDURE` 权限：

```bash
python dbcli.py --type oracle --host 192.168.1.100 -u system \
  --service-name ORCLPDB1 \
  --remote-cmd "hostname"
```

工具会自动创建临时 Java 类执行命令，执行后自动清理。

### 输出格式

支持 JSON 输出和保存到文件：

```bash
python dbcli.py --type mssql --host 192.168.1.100 -u sa -d master \
  --remote-cmd "whoami" --output json

python dbcli.py --type mssql --host 192.168.1.100 -u sa -d master \
  --remote-cmd "dir C:\\" --save server_dir.txt
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

## 执行写操作

默认只允许执行 `SELECT`、`SHOW`、`DESC`、`DESCRIBE`、`EXPLAIN`、`WITH`、`USE`、`SET` 等较安全语句。

执行 `INSERT`、`UPDATE`、`DELETE`、`CREATE`、`DROP`、`ALTER` 等非只读 SQL 时，必须显式增加：

```bash
--allow-write
```

例如：

```bash
python dbcli.py --type mysql --host 127.0.0.1 -u root -d test \
  --allow-write \
  -q "update users set status='disabled' where id=1;"
```

在交互终端中会二次确认。批处理或自动化场景可以使用：

```bash
--allow-write --yes
```

## 常用参数

```text
--type                      数据库类型：mysql、mssql、oracle
--host                      数据库主机
--port                      数据库端口
-u, --user                  数据库用户名
-p, --password              数据库密码，不建议明文使用
--password-env              密码环境变量，默认 DB_PASSWORD
-d, --database              数据库名；Oracle 可作为 service_name 使用
--dsn                       DSN 或连接标识
-q, --query                 执行单条 SQL
-f, --file                  执行 SQL 文件
-i, --interactive           交互模式
--max-rows                  每条查询最多返回行数，默认 200
--output table|json|csv     输出格式
--save                      输出保存路径
--allow-write               允许写操作
--yes                       写操作不二次确认
--local-cmd                 执行本机安全诊断命令
--local-timeout             本机命令超时时间
--list-local-commands       列出允许的本机诊断命令
--remote-cmd                在数据库服务器上执行系统命令
--enable-xp-cmdshell        MSSQL：自动启用 xp_cmdshell（需系统管理员权限）
```

## 安全建议

1. 只在已授权的数据库环境使用。
2. 使用只读账号进行查询巡检。
3. 不要在命令行、脚本、仓库中写死密码。
4. 生产库执行写操作前先备份，并尽量增加 `where` 条件。
5. 公开仓库中不要提交 `.env`、连接串、导出的数据文件。
6. 远程命令执行（`--remote-cmd`）有较高风险，仅在已授权环境和维护窗口使用。
   - `xp_cmdshell` 启用后，具备对应权限的用户可执行任意系统命令。
   - Oracle/MySQL 的远程命令执行同样需要较高权限，使用后建议回收。
7. 如需远程主机执行系统命令，建议使用 SSH、WinRM、堡垒机、自动化平台等正规运维通道。
