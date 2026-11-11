"""Compare backend results and write the end-to-end report.

``run_cases.py`` calls :func:`compare`, :func:`console_rows` and :func:`write`.
To rebuild the report from an earlier run without rerunning it::

    uv run python dev/e2e/report.py dev/e2e/results/<run>
"""

import json
import platform
import statistics
import sys
from datetime import datetime
from pathlib import Path

type Record = dict[str, object]

COLORS = {"sqlite": "#2a6fdb", "postgresql": "#d9822b", "mysql": "#2f9e44"}
SAMPLE_TURNS = (1, 10, 25, 50, 75, 100, 125, 150, 175, 200, 300, 400, 500, 1000)


def _ms(value: object) -> str:
    if value is None:
        return "–"
    number = float(value)
    return f"{number:.2f}" if number < 10 else f"{number:.1f}"


def _active(summaries: list[Record]) -> list[Record]:
    return [summary for summary in summaries if "skipped" not in summary]


def compare(summaries: list[Record]) -> Record:
    """Check that every backend stored the same conversations, payload for payload.

    Sessions with a stopped turn are compared by roles only, because where a
    stop lands in a streamed answer depends on timing.
    """
    tables: dict[str, dict[str, Record]] = {}
    for summary in _active(summaries):
        sessions = summary["inspection"]["sessions"]
        tables[str(summary["backend"])] = {str(s["title"]): s for s in sessions}
    differences: list[str] = []
    compared = messages = 0
    names = list(tables)
    if len(names) < 2:
        return {
            "identical": True,
            "compared": 0,
            "messages": 0,
            "differences": [],
            "backends": names,
        }
    reference = tables[names[0]]
    for other in names[1:]:
        missing = set(reference) ^ set(tables[other])
        differences.extend(
            f"{other}: session set differs at {sorted(missing)}" for _ in [0] if missing
        )
    for title, session in reference.items():
        exact = "stopped" not in session["statuses"]
        for other in names[1:]:
            candidate = tables[other].get(title)
            if candidate is None:
                continue
            key = "hashes" if exact else "roles"
            if session[key] != candidate[key]:
                differences.append(f"{title}: {names[0]} and {other} differ ({key})")
        compared += 1
        messages += len(session["hashes"])
    return {
        "identical": not differences,
        "compared": compared,
        "messages": messages,
        "differences": differences,
        "backends": names,
    }


def _passed(summary: Record) -> str:
    cases = summary.get("cases", [])
    return f"{sum(1 for c in cases if c['passed'])}/{len(cases)}"


def console_rows(summaries: list[Record], comparison: Record) -> list[str]:
    """Plain-text comparison table for the end of the run."""
    rows = [
        f"{'backend':<11} {'cases':>5} {'load p50':>9} {'commit p50':>10} {'e2e p50':>8} "
        f"{'@last load':>10} {'@last commit':>12} {'storage':>9}"
    ]
    for summary in summaries:
        if "skipped" in summary:
            rows.append(f"{summary['label']:<11} skipped: {summary['skipped']}")
            continue
        phases, client = summary["phases"], summary["client"]
        last = summary["growth"][-1] if summary["growth"] else {}
        rows.append(
            f"{summary['label']:<11} {_passed(summary):>5} "
            f"{_ms(phases['load_ms']['p50']):>7}ms {_ms(phases['commit_ms']['p50']):>8}ms "
            f"{_ms(client['e2e_ms']['p50']):>6}ms {_ms(last.get('load_ms')):>8}ms "
            f"{_ms(last.get('commit_ms')):>10}ms "
            f"{summary['inspection']['storage_bytes'] / 1024:>6.0f}KiB"
        )
    verdict = "identical" if comparison["identical"] else "DIFFERENT"
    rows.append(
        f"Stored content across backends: {verdict} "
        f"({comparison['compared']} sessions, {comparison['messages']} messages compared)"
    )
    rows.extend(f"  - {difference}" for difference in comparison["differences"])
    return rows


def _rolling_median(values: list[float | None], window: int = 9) -> list[float | None]:
    smoothed: list[float | None] = []
    for index in range(len(values)):
        chunk = [v for v in values[max(0, index - window + 1) : index + 1] if v is not None]
        smoothed.append(statistics.median(chunk) if chunk else None)
    return smoothed


def growth_svg(summaries: list[Record]) -> str:
    """Two panels (history load, commit) of rolling-median time against turns."""
    width, height, pad_left, pad_top, panel_gap = 760, 560, 64, 40, 70
    panel_height = (height - pad_top - panel_gap - 60) / 2
    plot_width = width - pad_left - 150
    active = [s for s in _active(summaries) if s["growth"]]
    turns = max((len(s["growth"]) for s in active), default=1)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="system-ui, sans-serif" font-size="12">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]
    for panel, (key, title) in enumerate(
        (
            ("load_ms", "History load (ms, rolling median of 9)"),
            ("commit_ms", "Turn commit (ms, rolling median of 9)"),
        )
    ):
        top = pad_top + panel * (panel_height + panel_gap)
        series = {
            str(s["backend"]): _rolling_median([p.get(key) for p in s["growth"]]) for s in active
        }
        peak = (
            max((v for values in series.values() for v in values if v is not None), default=1.0)
            * 1.1
        )
        parts.append(f'<text x="{pad_left}" y="{top - 12}" font-weight="600">{title}</text>')
        parts.append(
            f'<rect x="{pad_left}" y="{top}" width="{plot_width}" height="{panel_height}" '
            'fill="none" stroke="#d0d4d9"/>'
        )
        for tick in range(5):
            value = peak * tick / 4
            y = top + panel_height - panel_height * tick / 4
            parts.append(
                f'<line x1="{pad_left}" x2="{pad_left + plot_width}" '
                f'y1="{y:.1f}" y2="{y:.1f}" stroke="#eef0f2"/>'
            )
            parts.append(
                f'<text x="{pad_left - 8}" y="{y + 4:.1f}" text-anchor="end" '
                f'fill="#5c6670">{value:.1f}</text>'
            )
        for tick in range(6):
            turn = max(1, round(turns * tick / 5))
            x = pad_left + plot_width * (turn - 1) / max(1, turns - 1)
            parts.append(
                f'<text x="{x:.1f}" y="{top + panel_height + 16:.1f}" '
                f'text-anchor="middle" fill="#5c6670">{turn}</text>'
            )
        for backend, values in series.items():
            points = " ".join(
                f"{pad_left + plot_width * i / max(1, turns - 1):.1f},"
                f"{top + panel_height - panel_height * v / peak:.1f}"
                for i, v in enumerate(values)
                if v is not None
            )
            parts.append(
                f'<polyline points="{points}" fill="none" '
                f'stroke="{COLORS[backend]}" stroke-width="2"/>'
            )
    legend_top = pad_top
    for offset, summary in enumerate(active):
        y = legend_top + offset * 20
        x = pad_left + plot_width + 20
        parts.append(
            f'<rect x="{x}" y="{y}" width="14" height="4" '
            f'fill="{COLORS[str(summary["backend"])]}"/>'
        )
        parts.append(f'<text x="{x + 20}" y="{y + 6}">{summary["label"]}</text>')
    parts.append(
        f'<text x="{pad_left + plot_width / 2}" y="{height - 16}" '
        'text-anchor="middle" fill="#5c6670">'
        "turn number (history grows by 2 messages per turn)</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def write(run_id: str, summaries: list[Record], comparison: Record, path: Path) -> Path:
    """Write the Markdown report and its growth chart next to it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "growth.svg").write_text(growth_svg(summaries), encoding="utf-8")
    active = _active(summaries)
    lines = [
        "# v0.2 端到端测试与三库对比",
        "",
        f"运行：`{run_id}`，生成于 {datetime.now():%Y-%m-%d %H:%M}。"
        "方法见[测试计划](../../plans/v0.2-e2e-test-plan.md)；"
        "本文由 `dev/e2e/report.py` 生成，请勿手改。",
        "",
        f"环境：macOS {platform.mac_ver()[0] or platform.platform()}，"
        f"Python {sys.version.split()[0]}；"
        + "，".join(f"{s['label']} {s.get('version', '?')}" for s in active)
        + "。PostgreSQL 与 MySQL 运行在 Docker 中，SQLite 为本地文件。"
        "所有模型调用由确定性假模型应答。",
        "",
        "## 结论",
        "",
    ]
    for summary in summaries:
        if "skipped" in summary:
            lines.append(f"- **{summary['label']}**：未运行（{summary['skipped']}）。")
            continue
        problems = summary["inspection"]["problems"]
        lines.append(
            f"- **{summary['label']}**：用例 {_passed(summary)} 通过；"
            f"库内一致性检查{'全部满足' if not problems else f'发现 {len(problems)} 个问题'}。"
        )
    lines.append(
        f"- 三库存储内容{'完全一致' if comparison['identical'] else '存在差异'}："
        f"比对了 {comparison['compared']} 个会话、{comparison['messages']} 条消息"
        "（含中途停止的会话只比对消息结构）。"
    )
    lines += [
        "",
        "## 用例结果",
        "",
        "| 用例 | " + " | ".join(str(s["label"]) for s in active) + " |",
    ]
    lines.append("|---|" + "---|" * len(active))
    names = [c["name"] for c in active[0]["cases"]] if active else []
    for index, name in enumerate(names):
        cells = []
        for summary in active:
            case = summary["cases"][index]
            mark = "✅" if case["passed"] else "❌"
            cells.append(f"{mark} {sum(1 for c in case['checks'] if c[1])}/{len(case['checks'])}")
        lines.append(f"| {index + 1}. {name} | " + " | ".join(cells) + " |")
    failed = [
        (s["label"], c["name"], check)
        for s in active
        for c in s["cases"]
        for check in c["checks"]
        if not check[1]
    ]
    if failed:
        lines += ["", "未通过的检查："]
        lines += [f"- {label} / {name}：{check[0]}（{check[2]}）" for label, name, check in failed]

    lines += [
        "",
        "## 各阶段耗时（毫秒）",
        "",
        "服务端在 Gateway 进程内计时（`dev/e2e/serve_gateway.py`）；客户端时间含本机 HTTP。"
        "统计范围是该库的全部轮次（用例加增长曲线）。",
        "",
        "| 阶段 | " + " | ".join(f"{s['label']} p50 / p95 / max" for s in active) + " |",
        "|---|" + "---|" * len(active),
    ]
    rows = (
        ("加载历史 `load_history`", "phases", "load_ms"),
        ("落库 `commit_turn`", "phases", "commit_ms"),
        ("Agent 运行（含假模型）", "phases", "agent_ms"),
        ("首字延迟", "phases", "ttft_ms"),
        ("客户端：发起 run", "client", "post_ms"),
        ("客户端：发起到终态", "client", "e2e_ms"),
    )
    for title, group, key in rows:
        cells = []
        for summary in active:
            item = summary[group][key]
            cells.append(f"{_ms(item['p50'])} / {_ms(item['p95'])} / {_ms(item['max'])}")
        lines.append(f"| {title} | " + " | ".join(cells) + " |")
    operations = sorted({op for s in active for op in s["db_ops"]})
    if operations:
        lines += [
            "",
            "其他存储操作 p50（毫秒，次数）：",
            "",
            "| 操作 | " + " | ".join(str(s["label"]) for s in active) + " |",
        ]
        lines.append("|---|" + "---|" * len(active))
        for op in operations:
            cells = []
            for summary in active:
                item = summary["db_ops"].get(op)
                cells.append("–" if item is None else f"{_ms(item['p50'])}（{item['n']}）")
            lines.append(f"| `{op}` | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## 历史增长曲线",
        "",
        "同一会话连续对话，每轮新增 2 条消息。图中为 9 轮滚动中位数，表中为该轮的单次值；"
        "`读全部` 为分页读取全部消息的总耗时（客户端）。",
        "",
        "![历史增长曲线](growth.svg)",
        "",
        "| 轮次 | 历史条数 | "
        + " | ".join(f"{s['label']} 加载 / 落库 / 读全部" for s in active)
        + " |",
        "|---|---|" + "---|" * len(active),
    ]
    longest = max((len(s["growth"]) for s in active), default=0)
    for turn in [t for t in SAMPLE_TURNS if t <= longest]:
        cells = []
        history = "–"
        for summary in active:
            points = summary["growth"]
            point = points[turn - 1] if turn <= len(points) else {}
            history = str(point.get("history_messages", history))
            cells.append(
                f"{_ms(point.get('load_ms'))} / {_ms(point.get('commit_ms'))} / "
                f"{_ms(point.get('read_all_ms'))}"
            )
        lines.append(f"| {turn} | {history} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## 存储空间",
        "",
        "| 后端 | 会话 | 消息 | 消息 payload | 表占用 | 占用 / payload |",
        "|---|---|---|---|---|---|",
    ]
    for summary in active:
        inspection = summary["inspection"]
        payload = sum(s["payload_bytes"] for s in inspection["sessions"])
        size = inspection["storage_bytes"]
        lines.append(
            f"| {summary['label']} | {len(inspection['sessions'])} | {inspection['messages']} | "
            f"{payload / 1024:.0f} KiB | {size / 1024:.0f} KiB | "
            f"{size / payload if payload else 0:.2f} |"
        )
    lines += [
        "",
        "SQLite 为数据库文件页数乘页大小；"
        "PostgreSQL 为 `pg_total_relation_size`（含索引与 TOAST）；"
        "MySQL 为 `ANALYZE TABLE` 后 `information_schema` 的数据与索引长度，"
        "以 16 KiB 页为单位，粒度较粗。",
        "",
        "## 内容一致性",
        "",
        "每条消息以存储的 payload 计算 SHA-256，按会话标题对齐后逐条比对。",
        "",
    ]
    lines += [f"- {d}" for d in comparison["differences"]] or ["- 无差异。"]
    lines += [
        "",
        "## 局限",
        "",
        "- PostgreSQL、MySQL 在 macOS 的 Docker 虚拟机中运行，每次访问经过虚拟网络；"
        "SQLite 在进程内读写文件。"
        "数字反映本机开发环境，不代表生产部署。",
        "- 假模型不验证真实模型的回答与流式行为；真实模型的功能验收另行进行。",
        "- 单次运行，未做多次重复取平均；少于几毫秒的差异可能来自调度抖动。",
        "",
        "## 复现",
        "",
        "```bash",
        "docker compose -f dev/databases/compose.yaml up -d --wait postgres mysql",
        "uv run python dev/e2e/run_cases.py",
        "```",
        "",
        "原始数据（每个事件的 JSONL、服务端 trace、汇总 JSON）位于被 git 忽略的 "
        f"`dev/e2e/results/{run_id}/`。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    """Rebuild the report from a results directory."""
    run_dir = Path(sys.argv[1])
    summaries = [
        json.loads((run_dir / name / "summary.json").read_text(encoding="utf-8"))
        for name in json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["backends"]
        if (run_dir / name / "summary.json").exists()
    ]
    comparison = compare(summaries)
    for row in console_rows(summaries, comparison):
        sys.stdout.write(row + "\n")
    root = Path(__file__).resolve().parents[2]
    target = write(
        run_dir.name, summaries, comparison, root / "docs" / "testing" / "v0.2-e2e" / "README.md"
    )
    sys.stdout.write(f"Report: {target}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
