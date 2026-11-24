#!/usr/bin/env python3
"""Query the current model entry table in the private agents-local repository."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unicodedata


REMOTE = "git@github.com:DearCaat/agents-local.git"
REMOTE_HTTPS = "https://github.com/DearCaat/agents-local.git"  # SSH 不通的机器（只配了 https Git 认证）
SOURCE = "https://github.com/DearCaat/agents-local/blob"
TABLES = {
    "entries": "local/entries.md",
    "routes": "local/api-routes.tsv",
    "harnesses": "local/harnesses.md",
}


def read_table(path):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    ssh_config = next(
        (
            Path(env[key]) / ".git/config"
            for key in ("AGENTS_DATA_ROOT", "CLAUDE_PLUGIN_DATA", "PLUGIN_DATA", "GROK_PLUGIN_DATA")
            if env.get(key)
        ),
        None,
    )
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env and ssh_config and ssh_config.is_file():
        # Reuse the configured Git authentication without opening the local inventory.
        ssh = subprocess.run(
            ["git", "config", "--file", str(ssh_config), "--get", "core.sshCommand"],
            env=env, capture_output=True, text=True, timeout=5,
        )
        if ssh.returncode == 0 and ssh.stdout.strip():
            env["GIT_SSH_COMMAND"] = ssh.stdout.strip()
    if "GIT_SSH" not in env:
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")

    with tempfile.TemporaryDirectory(prefix="agents-query-") as directory:
        git = ["git", "--git-dir", directory]
        options = dict(env=env, capture_output=True, text=True, encoding="utf-8", timeout=20, check=True)
        subprocess.run(git + ["init", "--bare", "--quiet"], **options)
        try:
            subprocess.run(
                git + ["fetch", "--quiet", "--depth=1", "--no-tags", REMOTE, "refs/heads/main"],
                **options,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            subprocess.run(
                git + ["fetch", "--quiet", "--depth=1", "--no-tags", REMOTE_HTTPS, "refs/heads/main"],
                **options,
            )
        commit = subprocess.run(git + ["rev-parse", "FETCH_HEAD"], **options).stdout.strip()
        try:
            table = subprocess.run(git + ["show", f"{commit}:{path}"], **options).stdout
        except subprocess.CalledProcessError:
            raise RuntimeError(f"无法读取该提交的文件。\n{SOURCE}/{commit}/{path}") from None
    return commit, table


def normalize(value):
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"[\s-]+", " ", value)


def matching_entries(table, query):
    heading = ""
    fields = []
    has_entries = False
    for number, line in enumerate(table.splitlines(), 1):
        if line.startswith("## "):
            heading = line.removeprefix("## ")
            fields = []
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip("|"))]
        if cells[0] == "说法":
            fields = cells
            continue
        if not fields or re.fullmatch(r"[:\-]+", cells[0]):
            continue
        aliases = [quoted or code for quoted, code in re.findall(r"「([^」]+)」|`([^`]+)`", cells[0])]
        if len(cells) != len(fields) or not aliases:
            raise RuntimeError(f"入口表第 {number} 行格式无法识别。")
        has_entries = True
        if query in {normalize(alias) for alias in aliases}:
            yield number, heading, dict(zip(fields, cells))
    if not has_entries:
        raise RuntimeError("入口表缺少可识别的登记行。")


def matching_pending_notes(table, query):
    in_pending_section = False
    for number, line in enumerate(table.splitlines(), 1):
        if line.startswith("## "):
            in_pending_section = line == "## 待核"
        if not in_pending_section or not line.startswith("- "):
            continue
        label, separator, detail = line[2:].partition("：")
        if separator and normalize(label.removesuffix(" 裸名").strip("`「」")) == query:
            yield number, detail.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="look up a registered name or alias")
    ask.add_argument("name")
    show = commands.add_parser("show", help="read a reference table")
    show.add_argument("table", choices=TABLES)
    args = parser.parse_args()

    path = TABLES["entries" if args.command == "ask" else args.table]
    try:
        commit, table = read_table(path)
        source = f"{SOURCE}/{commit}/{path}"
        if args.command == "show":
            print(source, file=sys.stderr)
            print(table, end="")
            return 0

        query = normalize(args.name)
        try:
            matches = list(matching_entries(table, query))
        except RuntimeError as error:
            raise RuntimeError(f"{error}\n{source}") from None
        pending = list(matching_pending_notes(table, query))
        if not matches:
            if pending:
                for number, detail in pending:
                    print(f"待核  {detail}\n{source}#L{number}", file=sys.stderr)
            else:
                print(f"入口表未登记该名称。\n{source}", file=sys.stderr)
            return 1
        if len(matches) > 1:
            print("该名称对应多条入口。请用其中更具体的登记名查询。", file=sys.stderr)
            for number, _, entry in matches:
                print(f"{entry['说法']}  {source}#L{number}", file=sys.stderr)
            return 1

        number, heading, entry = matches[0]
        print(f"状态  {heading}")
        for key, value in entry.items():
            if key != "说法" and value:
                print(f"{key}  {value}")
        print(f"来源  {source}#L{number}")
        for number, detail in pending:
            print(f"待核  {detail}\n{source}#L{number}")
        return 0
    except FileNotFoundError:
        print("当前环境找不到 Git，无法查询入口表。", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("读取入口表超时。", file=sys.stderr)
    except subprocess.CalledProcessError as error:
        if "Permission denied (publickey)" in (error.stderr or ""):
            print("GitHub SSH 认证失败。请检查私有仓库的读取授权。", file=sys.stderr)
        else:
            print("读取 GitHub 私有仓库失败。请检查网络和仓库读取授权。", file=sys.stderr)
    except UnicodeError:
        print("入口表无法按 UTF-8 读取。", file=sys.stderr)
    except RuntimeError as error:
        print(error, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
