#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""send_daily_report.py —— 「明白账」每日财务晚报批处理。

把当月消费最高的若干用户汇总成**一条**群消息（飞书 text / 钉钉 markdown），
每人配一句 AI 晚间点评；单用户 AI 失败用本地兜底文案，不影响整批。

用法：
    python send_daily_report.py [--month YYYY-MM] [--dry-run] [--force]
                                [--limit N] [--channel webhook]
                                [--anonymize | --no-anonymize]

隐私（重要）：
    · 默认**脱敏**：群里只出现「用户#{user_id}」，真实用户名既不会进入群消息，
      也不会传给 AI 提示词。
    · 需要真实用户名时显式加 `--no-anonymize`（或 ANONYMIZE_NAME=0）——
      请先确认群成员都有权看到这些用户名。
    · `--anonymize` 可显式打开脱敏（默认即开启），适合在 CI 里写明意图。
    · `--dry-run` 只打印，不发 webhook、不写 user_notifications。

退出码：
    0  成功 / dry-run；
    1  发送失败或写通知记录失败（错误已逐条写库，便于复盘）；
    2  配置错误（WEBHOOK_URL 缺失、月份格式非法、webhook 类型无法识别等）。

安全：日志只打印 user_id/状态码/异常摘要，**绝不**打印 WEBHOOK_URL、WEBHOOK_SECRET、
DEEPSEEK_API_KEY 或请求头。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

import requests

from config import get_secret

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai（中国无夏令时，固定 +08:00 足够）
DEFAULT_CHANNEL = "webhook"
WEBHOOK_TIMEOUT = 15
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_FALSY_ENV = {"0", "false", "no", "off"}


class ReportError(Exception):
    """可直接展示给运维的中文错误（不包含 URL / token / key）。"""


def _log(message: str) -> None:
    """结构化进度日志：统一前缀，flush 保证 CI 日志实时可见。"""
    print(f"[daily-report] {message}", flush=True)


def _error(message: str) -> None:
    print(f"[daily-report] 错误：{message}", file=sys.stderr, flush=True)


def _exc_brief(exc: Exception) -> str:
    """日志用异常摘要：可操作的中文提示优先，其余只给异常类型。

    绝不把 requests 异常文本/请求头拼进日志（可能带完整 URL 或 token）；
    仅对 db 层刻意抛出的 RuntimeError / ValueError 放行其 message。
    """
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return f"{type(exc).__name__}: {message.strip()[:120]}"
    if isinstance(exc, (RuntimeError, ValueError)):
        text = str(exc).strip()
        if text:
            return f"{type(exc).__name__}: {text[:200]}"
    return type(exc).__name__


def _secret_str(key: str) -> str:
    """读配置并统一成 str（secrets.toml 里可能写成数字/布尔，直接 .strip() 会炸）。"""
    value = get_secret(key)
    return "" if value is None else str(value).strip()


def _month_arg(text: str) -> str:
    text = (text or "").strip()
    if not _MONTH_RE.match(text):
        raise argparse.ArgumentTypeError("月份格式应为 YYYY-MM（例如 2026-10）")
    return text


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("--limit 必须是正整数") from None
    if value < 1:
        raise argparse.ArgumentTypeError("--limit 必须是正整数")
    return value


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="send_daily_report.py",
        description=(
            "「明白账」每日财务晚报：按当月支出降序汇总用户，逐人生成 AI 晚间点评，"
            "合并成一条飞书/钉钉群消息推送。默认按 ANONYMIZE_NAME 脱敏，"
            "只有显式 --no-anonymize 才会把真实用户名发到群里。"
        ),
        epilog=(
            "隐私说明：默认脱敏为「用户#{user_id}」；--no-anonymize（或 ANONYMIZE_NAME=0）"
            "才会显示真实用户名，请确认群成员有权查看后再关闭。"
            " --dry-run 只打印完整消息与每人点评，不发 webhook、不写库。"
        ),
    )
    parser.add_argument(
        "--month",
        type=_month_arg,
        default=None,
        help="统计月份，格式 YYYY-MM，默认当月（Asia/Shanghai）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练：只打印完整群消息与每人点评，不发 webhook、不写 user_notifications",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略「今天已发送」记录，强制重发（仍会写新的通知记录）",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="只处理当月支出最高的前 N 位用户（默认全部）",
    )
    parser.add_argument(
        "--channel",
        default=DEFAULT_CHANNEL,
        help=f"通知渠道标识，写入 user_notifications.channel（默认 {DEFAULT_CHANNEL}）",
    )
    anonymize_group = parser.add_mutually_exclusive_group()
    anonymize_group.add_argument(
        "--anonymize",
        dest="anonymize",
        action="store_true",
        help="显式开启脱敏（默认已开启，此参数用于在 CI 中声明意图）",
    )
    anonymize_group.add_argument(
        "--no-anonymize",
        dest="anonymize",
        action="store_false",
        help="⚠ 关闭脱敏：群消息会显示真实用户名（默认不允许）",
    )
    parser.set_defaults(anonymize=None)
    return parser.parse_args(argv)


def resolve_month(value: str | None) -> str:
    """返回 YYYY-MM；未指定时取本地（+08:00）当月。"""
    if not value:
        return datetime.now(TZ).strftime("%Y-%m")
    if not _MONTH_RE.match(value):
        raise ReportError("月份格式应为 YYYY-MM（例如 2026-10）")
    return value


def resolve_anonymize(cli_value: bool | None) -> bool:
    """脱敏默认开启；--no-anonymize / ANONYMIZE_NAME=0 才关闭（显式关闭）。"""
    if cli_value is not None:
        return bool(cli_value)
    raw = _secret_str("ANONYMIZE_NAME").lower()
    if raw in _FALSY_ENV:
        return False
    return True


def _money(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def display_name(row: dict, anonymize: bool) -> str:
    user_id = row.get("user_id")
    if anonymize:
        return f"用户#{user_id}"
    return str(row.get("username") or f"用户#{user_id}")


def fallback_comment(row: dict) -> str:
    """AI 不可用时的本地兜底文案（保证群里每个用户都有点评）。"""
    today_expense = _money(row.get("today_expense"))
    if today_expense > 0:
        return f"今天花了 {today_expense:.2f} 元，明天继续加油"
    return "今天还没花钱，继续保持"


def build_message(entries: list[dict], month: str, now: datetime) -> tuple[str, str]:
    """组装一条群消息，返回 (title, body)。"""
    today = now.strftime("%Y-%m-%d")
    title = f"📊 {month} 每日财务晚报 · {today}"
    lines = []
    for entry in entries:
        overspend = "（今天花得比平时多 ⚠️）" if entry["today_overspend"] else ""
        lines.append(
            f"{entry['name']}：本月支出 ¥{entry['month_expense']:.2f}，"
            f"结余 ¥{entry['month_balance']:.2f}；"
            f"今日 ¥{entry['today_expense']:.2f}{overspend}；{entry['comment']}"
        )
    lines.append("")
    lines.append(
        f"共 {len(entries)} 位用户 · {sum(1 for e in entries if e['ai_ok'])} 位 AI 点评成功"
        f" · {now.strftime('%Y-%m-%d %H:%M')}"
    )
    return title, "\n".join(lines)


def detect_webhook_type(url: str) -> str:
    """优先 WEBHOOK_TYPE，其次按 URL host 识别 feishu / dingtalk。"""
    explicit = _secret_str("WEBHOOK_TYPE").lower()
    if explicit in ("feishu", "lark"):
        return "feishu"
    if explicit in ("dingtalk", "dingding"):
        return "dingtalk"
    if explicit:
        raise ReportError(f"WEBHOOK_TYPE={explicit} 不受支持（仅 feishu / dingtalk）")
    host = urlparse(url).netloc.lower()
    if "feishu" in host or "larksuite" in host:
        return "feishu"
    if "dingtalk" in host:
        return "dingtalk"
    raise ReportError(
        "无法识别 webhook 类型：请设置 WEBHOOK_TYPE=feishu|dingtalk，或使用飞书/钉钉官方域名 URL"
    )


def sign_dingtalk_url(url: str, secret: str) -> str:
    """钉钉加签：HmacSHA256(timestamp + "\\n" + secret)，Base64 后 URL 编码。"""
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).digest()
    sign = quote_plus(base64.b64encode(digest).decode("utf-8"))
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}timestamp={timestamp}&sign={sign}"


def build_payload(kind: str, title: str, body: str) -> dict:
    """飞书走 text、钉钉走 markdown（各自只带自己的字段）。"""
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": f"{title}\n\n{body}"}}
    return {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": f"### {title}\n\n{body}"},
    }


def send_webhook(url: str, payload: dict) -> dict:
    """POST 群消息；HTTP 非 200 或返回体 errcode/code 非 0 均抛 ReportError。"""
    try:
        resp = requests.post(url, json=payload, timeout=WEBHOOK_TIMEOUT)
    except requests.Timeout:
        raise ReportError("发送群消息超时") from None
    except requests.RequestException as exc:
        # 只记异常类型：异常文本可能带完整 URL（含 token）。
        raise ReportError(f"网络连接失败（{type(exc).__name__}）") from None

    if resp.status_code != 200:
        raise ReportError(f"群消息接口返回 HTTP {resp.status_code}")

    try:
        data = resp.json()
    except (ValueError, TypeError):
        raise ReportError("群消息接口返回不是合法 JSON") from None

    if isinstance(data, dict):
        for key in ("errcode", "code"):
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, str):
                value = value.strip()
            if str(value) not in ("0", ""):
                raise ReportError(f"群消息接口业务错误（{key}={value}）")
    return data


def _safe_record(db, user_id, today, channel, status, message=None, error=None) -> bool:
    """写通知记录失败不应中断整批，但要计入退出码（可观测）。"""
    try:
        db.record_notification(
            user_id, today, channel, status, message=message, error=error
        )
        return True
    except Exception as exc:  # noqa: BLE001 —— 记录失败只降级，不阻断发送
        _log(
            f"警告：写通知记录失败（user#{user_id}，status={status}，"
            f"异常={_exc_brief(exc)}）"
        )
        return False


def _generate_comment(generate_evening_comment, row: dict, name: str, api_key: str):
    """调 AI 点评；返回 (comment, ai_ok)。任何异常都回落本地文案，绝不中断整批。"""
    if generate_evening_comment is None:
        return fallback_comment(row), False
    ai_stats = dict(row)
    # 默认传入脱敏名：真实用户名默认不出现在提示词里。
    ai_stats["username"] = name
    try:
        comment = generate_evening_comment(ai_stats, api_key)
    except Exception as exc:  # noqa: BLE001 —— EveningCommentError 及其他意外都兜底
        _log(
            f"用户#{row.get('user_id')} AI 点评失败（异常={_exc_brief(exc)}），"
            "使用本地兜底文案"
        )
        return fallback_comment(row), False
    if isinstance(comment, str) and comment.strip():
        return comment.strip(), True
    _log(f"用户#{row.get('user_id')} AI 点评为空，使用本地兜底文案")
    return fallback_comment(row), False


def _load_db():
    try:
        import db as db_module
    except Exception as exc:  # noqa: BLE001
        raise ReportError(f"无法加载 db 模块（异常={type(exc).__name__}）") from None
    missing = [
        name
        for name in ("report_user_stats", "record_notification", "notification_already_sent")
        if not hasattr(db_module, name)
    ]
    if missing:
        raise ReportError(f"db 模块缺少契约函数：{', '.join(missing)}")
    return db_module


def _load_generate_comment():
    """懒加载 ai_agent：它顶层会初始化 Supabase，失败时整批回落本地文案。

    ⚠ ai_agent.py 顶层立即 create_client(SUPABASE_URL, SUPABASE_KEY)，而本脚本只用到
    generate_evening_comment（纯 requests 调 DeepSeek，不碰 Supabase）。CI 可能只注入
    DATABASE_URL / DEEPSEEK_API_KEY，这时 get_secret 会读到空值并让 import 失败。
    因此仅在缺省时注入**占位**配置以完成导入；真实配置（secrets.toml / 环境变量）
    始终优先，本脚本不会用这个客户端发起任何请求。
    """
    if not (os.environ.get("SUPABASE_URL") or "").strip():
        os.environ["SUPABASE_URL"] = "https://placeholder.supabase.co"
    if not (os.environ.get("SUPABASE_KEY") or "").strip():
        os.environ["SUPABASE_KEY"] = "placeholder-anon-key"
    try:
        import ai_agent
    except Exception as exc:  # noqa: BLE001
        _log(f"警告：ai_agent 导入失败（异常={_exc_brief(exc)}），全部使用本地兜底文案")
        return None
    generate = getattr(ai_agent, "generate_evening_comment", None)
    if generate is None:
        _log("警告：ai_agent 缺少 generate_evening_comment，全部使用本地兜底文案")
    return generate


def _print_dry_run(title: str, body: str, entries: list[dict], kind: str) -> None:
    print(f"[daily-report] dry-run：共 {len(entries)} 位用户，不发送、不写库", flush=True)
    for entry in entries:
        print(
            f"  - {entry['name']} | AI={'ok' if entry['ai_ok'] else 'fallback'} "
            f"| {entry['comment']}",
            flush=True,
        )
    print(f"----- 完整群消息（{kind}）-----", flush=True)
    print(f"{title}\n", flush=True)
    print(body, flush=True)
    print("----- /完整群消息 -----", flush=True)


def run(args: argparse.Namespace) -> int:
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    month = resolve_month(args.month)
    anonymize = resolve_anonymize(args.anonymize)
    channel = (args.channel or DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL

    _log(
        f"开始：month={month} channel={channel} dry_run={args.dry_run} "
        f"force={args.force} limit={args.limit} anonymize={anonymize}"
    )

    # 配置先行：非 dry-run 时没有 WEBHOOK_URL 直接退出，避免白花 AI 调用、避免半写库。
    webhook_url = _secret_str("WEBHOOK_URL")
    kind = None
    if not args.dry_run:
        if not webhook_url:
            _error("未配置 WEBHOOK_URL（config.get_secret / 环境变量），无法发送群消息")
            return 2
        # 渠道类型也在调用 AI 之前确定：类型不认识就没必要先花钱生成点评。
        try:
            kind = detect_webhook_type(webhook_url)
        except ReportError as exc:
            _error(str(exc))
            return 2

    db = _load_db()
    try:
        stats_list = db.report_user_stats(month) or []
    except Exception as exc:  # noqa: BLE001
        _error(f"读取用户统计失败（异常={_exc_brief(exc)}）")
        return 1
    stats_list = sorted(
        stats_list, key=lambda row: _money((row or {}).get("month_expense")), reverse=True
    )
    if args.limit is not None:
        stats_list = stats_list[: args.limit]
    _log(f"统计完成：用户数={len(stats_list)}")

    if not stats_list:
        _log("本月没有用户数据，跳过发送")
        return 0

    generate_evening_comment = _load_generate_comment()
    api_key = _secret_str("DEEPSEEK_API_KEY")
    if not api_key:
        _log("警告：DEEPSEEK_API_KEY 为空，全部使用本地兜底文案")

    entries: list[dict] = []
    skipped = 0
    for row in stats_list:
        if not isinstance(row, dict) or row.get("user_id") is None:
            _log("警告：跳过一条缺少 user_id 的记录")
            continue
        user_id = row["user_id"]
        name = display_name(row, anonymize)

        if not args.dry_run:
            try:
                already_sent = db.notification_already_sent(user_id, today, channel)
            except Exception as exc:  # noqa: BLE001
                _error(
                    f"查询已发送状态失败（user#{user_id}，异常={_exc_brief(exc)}），"
                    "为避免重复推送已中止"
                )
                return 1
            if already_sent and not args.force:
                # 只在内存里计数并记日志，**不写库**：db.record_notification 按
                # (user_id, sent_date, channel) upsert，写 "skipped" 会把既有
                # "sent" 覆盖掉，使同日第三次运行 notification_already_sent
                # 变回 False → 重复推送。已发送是终态，跳过时不动数据库。
                # 同日重跑请显式 --force 表达意图。
                skipped += 1
                _log(f"用户#{user_id} 今天已发送，跳过（保持 sent 状态，不写 skipped）")
                continue
            if already_sent and args.force:
                _log(f"用户#{user_id} 今天已发送，--force 强制重发")

        comment, ai_ok = _generate_comment(
            generate_evening_comment, row, name, api_key
        )
        _log(
            f"用户#{user_id} {'AI 点评成功' if ai_ok else '本地兜底文案'}（{len(comment)} 字）"
        )
        entries.append(
            {
                "user_id": user_id,
                "name": name,
                "month_expense": _money(row.get("month_expense")),
                "month_balance": _money(row.get("month_balance")),
                "today_expense": _money(row.get("today_expense")),
                "today_overspend": bool(row.get("today_overspend")),
                "comment": comment,
                "ai_ok": ai_ok,
            }
        )

    if not entries:
        _log(f"没有需要发送的用户（跳过={skipped}），不发送空消息")
        return 0

    title, body = build_message(entries, month, now)

    if args.dry_run:
        # 尽量按真实渠道格式展示；没有 URL 时按飞书 text 预览。
        kind = "feishu"
        if webhook_url:
            try:
                kind = detect_webhook_type(webhook_url)
            except ReportError:
                kind = "feishu"
        _print_dry_run(title, body, entries, kind)
        return 0

    payload = build_payload(kind, title, body)
    send_url = webhook_url
    if kind == "dingtalk":
        secret = _secret_str("WEBHOOK_SECRET")
        if secret:
            send_url = sign_dingtalk_url(webhook_url, secret)
            _log("钉钉加签已启用")
    _log(f"发送中：webhook={kind} 用户数={len(entries)}")

    try:
        send_webhook(send_url, payload)
    except ReportError as exc:
        error_text = str(exc)
        _log(f"发送失败：{error_text}")
        # 发送失败：本批所有用户记 failed（skipped 用户已单独记录，不覆盖其历史状态）。
        for entry in entries:
            _safe_record(
                db,
                entry["user_id"],
                today,
                channel,
                "failed",
                message=entry["comment"],
                error=error_text,
            )
        return 1

    _log("发送成功")
    record_failures = 0
    for entry in entries:
        ok = _safe_record(
            db,
            entry["user_id"],
            today,
            channel,
            "sent",
            message=entry["comment"],
        )
        if not ok:
            record_failures += 1
    ai_ok_count = sum(1 for entry in entries if entry["ai_ok"])
    _log(
        f"完成：用户={len(entries)} AI成功={ai_ok_count} 跳过={skipped} "
        f"记录失败={record_failures}"
    )
    return 1 if record_failures else 0


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except ReportError as exc:
        _error(str(exc))
        return 1
    except KeyboardInterrupt:
        _error("已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())
