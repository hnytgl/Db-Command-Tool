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
import csv
import getpass
import json
import os
import platform
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple


READONLY_KEYWORDS = {
    "SELECT",
    "SHOW",
    "DESC",
    "DESCRIBE",
    "EXPLAIN",
    "WITH",
}

SESSION_KEYWORDS = {
    "USE",
    "SET",
}

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
    }
    if value not in aliases:
        raise ValueError(f"不支持的数据库类型：{db_type}")
    return aliases[value]


def first_sql_keyword(sql: str) -> str:
    text = sql.strip()
    while True:
        if text.startswith("--"):
            idx = text.find("\n")
            text = "" if idx == -1 else text[idx + 1 :].lstrip()
            continue
        if text.startswith("/*"):
            idx = text.find("*/")
            text = "" if idx == -1 else text[idx + 2 :].lstrip()
            continue
        break

    match = re.match(r"([A-Za-z_]+)", text)
    return match.group(1).upper() if match else ""


def is_safe_without_write_flag(sql: str) -> bool:
    keyword = first_sql_keyword(sql)
    return keyword in READONLY_KEYWORDS or keyword in SESSION_KEYWORDS


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


def connect(args: argparse.Namespace) -> Any:
    db_type = normalize_type(args.type)
    password = prompt_password(args)
    if db_type == "mysql":
        return connect_mysql(args, password)
    if db_type == "mssql":
        return connect_mssql(args, password)
    if db_type == "oracle":
        return connect_oracle(args, password)
    raise ValueError(f"不支持的数据库类型：{args.type}")


def confirm_statement(statement: str) -> bool:
    print("\n检测到非只读 SQL：")
    print("-" * 80)
    print(statement[:1200] + ("..." if len(statement) > 1200 else ""))
    print("-" * 80)
    answer = input("确认执行？输入 yes 继续，其它任意输入取消：").strip().lower()
    return answer == "yes"


def execute_statement(conn: Any, statement: str, args: argparse.Namespace) -> QueryResult:
    if not is_safe_without_write_flag(statement):
        if not args.allow_write:
            raise PermissionError("检测到非只读 SQL。为避免误操作，请增加 --allow-write 后再执行。")
        if not args.yes and sys.stdin.isatty():
            if not confirm_statement(statement):
                raise KeyboardInterrupt("用户取消执行。")

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
    sql_text = load_sql(args)
    if not sql_text:
        raise ValueError("未提供 SQL。请使用 --query、--file 或 --interactive。")

    statements = [sql_text] if args.no_split else split_sql_statements(sql_text)
    if not statements:
        raise ValueError("未解析到可执行 SQL。")

    conn = connect(args)
    try:
        results = [execute_statement(conn, statement, args) for statement in statements]
        output_results(results, args)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_interactive(args: argparse.Namespace) -> None:
    print("已进入交互模式。输入 SQL 后用分号结尾执行；输入 .exit 退出；输入 .help 查看帮助。")
    conn = connect(args)
    buffer: List[str] = []
    try:
        while True:
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
                print("常用命令：\n  .help          显示帮助\n  .exit/.quit    退出\n  SQL;           以分号结尾执行 SQL\n说明：非只读 SQL 需启动时增加 --allow-write。")
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


def _exec_remote_cmd_mssql(conn: Any, command: str) -> RemoteCmdResult:
    """通过 MSSQL xp_cmdshell 在数据库服务器上执行系统命令。"""
    cursor = conn.cursor()
    try:
        cursor.execute("EXEC xp_cmdshell ?", (command,))
        rows = cursor.fetchall()
        # xp_cmdshell 返回的最后一行为 NULL，过滤掉
        output_lines: List[str] = []
        for row in rows:
            val = row[0]
            if val is not None:
                output_lines.append(str(val))
        return RemoteCmdResult(command=command, output="\n".join(output_lines))
    except Exception as exc:
        err = str(exc)
        if "xp_cmdshell" in err and "not found" in err:
            raise RuntimeError("xp_cmdshell 不可用。请使用 --enable-xp-cmdshell 尝试启用。") from exc
        raise RuntimeError(f"MSSQL 远程命令执行失败：{exc}") from exc
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def _exec_remote_cmd_mysql(conn: Any, command: str) -> RemoteCmdResult:
    """通过 MySQL UDF sys_exec 在数据库服务器上执行系统命令。

    需要先安装 lib_mysqludf_sys 插件：
        CREATE FUNCTION sys_exec RETURNS INTEGER SONAME 'lib_mysqludf_sys.so';
    """
    cursor = conn.cursor()
    try:
        # 检查 sys_exec 函数是否存在
        cursor.execute("SELECT COUNT(*) FROM mysql.func WHERE name = 'sys_exec'")
        row = cursor.fetchone()
        has_sys_exec = row is not None and row[0] > 0

        if not has_sys_exec:
            raise RuntimeError(
                "MySQL 服务器未安装 sys_exec UDF。\n"
                "请先安装 lib_mysqludf_sys 插件：\n"
                "  CREATE FUNCTION sys_exec RETURNS INTEGER SONAME 'lib_mysqludf_sys.so';\n"
                "或者手动执行 SQL 命令。"
            )

        # 获取命令输出：将 stdout 写入临时文件，再用 LOAD_FILE 读回
        outfile = "/tmp/_dbcli_remote_cmd_output.txt"
        shell_cmd = f"{command} > {outfile} 2>&1"

        cursor.execute("SELECT sys_exec(%s)", (shell_cmd,))
        exit_code_row = cursor.fetchone()
        exit_code = int(exit_code_row[0]) if exit_code_row else -1

        # 读取输出文件
        output = ""
        try:
            cursor.execute("SELECT LOAD_FILE(%s)", (outfile,))
            out_row = cursor.fetchone()
            if out_row and out_row[0] is not None:
                output = str(out_row[0])
        except Exception:
            pass

        # 清理
        try:
            cursor.execute("SELECT sys_exec(%s)", (f"rm -f {outfile}",))
        except Exception:
            pass

        result = RemoteCmdResult(command=command, output=output.rstrip())
        if exit_code != 0:
            result.error = f"命令退出码：{exit_code}"
        return result

    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"MySQL 远程命令执行失败：{exc}") from exc
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def _exec_remote_cmd_oracle(conn: Any, command: str) -> RemoteCmdResult:
    """通过 Oracle Java stored procedure 在数据库服务器上执行系统命令。

    需要 CREATE JAVA、CREATE PROCEDURE 权限，且 Oracle JVM 已安装。
    """
    cursor = conn.cursor()
    java_name = "DB_CLI_OS_CMD"
    func_name = "db_cli_os_exec"
    try:
        # 创建临时 Java 类
        java_src = '''
CREATE OR REPLACE JAVA SOURCE NAMED "{java_name}" AS
import java.io.*;
public class {java_name} {{
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
'''.format(java_name=java_name)

        cursor.execute(java_src)

        # 创建包装函数
        cursor.execute('''
CREATE OR REPLACE FUNCTION {func_name}(cmd VARCHAR2) RETURN VARCHAR2
AS LANGUAGE JAVA
NAME '{java_name}.exec(java.lang.String) return java.lang.String';
'''.format(func_name=func_name, java_name=java_name))

        # 执行命令
        cursor.execute("SELECT {func_name}(:cmd) FROM dual".format(func_name=func_name), cmd=command)
        row = cursor.fetchone()
        output = str(row[0]) if row and row[0] else ""

        return RemoteCmdResult(command=command, output=output.rstrip())

    except Exception as exc:
        err_str = str(exc).lower()
        if "jvm" in err_str or "java" in err_str:
            raise RuntimeError(
                "Oracle JVM 不可用或权限不足。\n"
                "可以尝试其他方式：\n"
                f"  1. DBMS_SCHEDULER:\n"
                f"     BEGIN DBMS_SCHEDULER.CREATE_JOB(job_name => 'TMP_JOB',\n"
                f"       job_type => 'EXECUTABLE', job_action => '/bin/sh',\n"
                f"       number_of_arguments => 1, enabled => FALSE, auto_drop => TRUE);\n"
                f"       DBMS_SCHEDULER.SET_JOB_ARGUMENT_VALUE('TMP_JOB', 1, '{command}');\n"
                f"       DBMS_SCHEDULER.ENABLE('TMP_JOB'); END;\n"
                f"  2. 直接通过 SQL 执行简单命令：\n"
                f"     SELECT os_command.exec('{command}') FROM dual; -- 需提前设置"
            ) from exc
        raise RuntimeError(f"Oracle 远程命令执行失败：{exc}") from exc
    finally:
        # 清理临时对象
        try:
            cursor.execute(f"DROP FUNCTION {func_name}")
        except Exception:
            pass
        try:
            cursor.execute(f"DROP JAVA SOURCE NAMED \"{java_name}\"")
        except Exception:
            pass
        try:
            cursor.close()
        except Exception:
            pass


def run_remote_cmd(args: argparse.Namespace) -> None:
    """连接数据库并在数据库服务器上执行系统命令。"""
    if not args.remote_cmd:
        raise ValueError("请指定 --remote-cmd 要执行的命令。")

    db_type = normalize_type(args.type)
    print(f"正在连接 {db_type} 服务器 {args.host or args.dsn} ...")
    conn = connect(args)

    try:
        # MSSQL：可选启用 xp_cmdshell
        if db_type == "mssql" and args.enable_xp_cmdshell:
            _enable_xp_cmdshell_mssql(conn)

        # 按数据库类型分发
        if db_type == "mssql":
            result = _exec_remote_cmd_mssql(conn, args.remote_cmd)
        elif db_type == "mysql":
            result = _exec_remote_cmd_mysql(conn, args.remote_cmd)
        elif db_type == "oracle":
            result = _exec_remote_cmd_oracle(conn, args.remote_cmd)
        else:
            raise ValueError(f"不支持的数据库类型：{args.type}")

        # 输出结果
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
            text = json.dumps(result.__dict__, ensure_ascii=False, indent=2)
            if args.save:
                with open(args.save, "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"\n结果已保存：{args.save}")
            else:
                print(text)

        elif args.save:
            with open(args.save, "w", encoding="utf-8") as f:
                f.write(result.output)
                if result.error:
                    f.write(f"\n[error]\n{result.error}")
            print(f"\n结果已保存：{args.save}")

    finally:
        try:
            conn.close()
        except Exception:
            pass


def validate_args(args: argparse.Namespace) -> None:
    if args.list_local_commands or args.local_cmd:
        return
    if args.remote_cmd:
        if not args.type:
            raise ValueError("远程命令模式必须指定 --type 数据库类型。")
        if not args.user:
            raise ValueError("远程命令模式必须指定 -u/--user。")
        if not args.host and not args.dsn:
            raise ValueError("远程命令模式必须指定 --host 或 --dsn。")
        return
    if not args.type:
        raise ValueError("数据库模式必须指定 --type；本机命令模式可使用 --local-cmd。")
    if not args.user:
        raise ValueError("数据库模式必须指定 -u/--user。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MSSQL / MySQL / Oracle 数据库连接与命令执行工具",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--type", help="数据库类型：mysql、mssql、oracle")
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
    parser.add_argument("--allow-write", action="store_true", help="允许执行非只读 SQL")
    parser.add_argument("--yes", action="store_true", help="非只读 SQL 不再二次确认")

    parser.add_argument("--local-cmd", help="执行本机安全诊断命令，不连接数据库")
    parser.add_argument("--local-timeout", type=int, default=15, help="本机命令超时时间，秒")
    parser.add_argument("--list-local-commands", action="store_true", help="列出允许执行的本机诊断命令")

    parser.add_argument("--remote-cmd", help="在数据库服务器上执行系统命令（通过数据库自身机制，如 xp_cmdshell）")
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
        elif args.remote_cmd:
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
