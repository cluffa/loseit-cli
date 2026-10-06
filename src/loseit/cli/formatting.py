"""Output formatting for CLI commands."""
from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table

console = Console()


def print_table(headers: list[str], rows: list[list[Any]], title: str = ""):
    """Print a formatted table."""
    table = Table(title=title)
    for h in headers:
        table.add_column(h)
    for row in rows:
        table.add_row(*[str(c) for c in row])
    console.print(table)


def print_status(summary: dict):
    """Print a nice status bar for daily calories."""
    cal_budget = summary.get("calorie_budget", 0)
    cal_consumed = summary.get("calories_consumed", 0)
    exercise = summary.get("exercise_calories", 0)
    cal_remaining = summary.get("calories_remaining", cal_budget + exercise - cal_consumed)

    if cal_budget == 0:
        console.print("[yellow]No calorie budget available. Run 'loseit summary' for details.[/yellow]")
        return

    allowance = cal_budget + exercise
    pct = min(max(cal_consumed / allowance * 100, 0), 100)
    bar_width = 30
    filled = int(bar_width * pct / 100)
    bar = "█" * filled + "░" * (bar_width - filled)

    color = "green" if pct < 80 else "yellow" if pct < 100 else "red"

    from datetime import date
    day = summary.get("date")
    label = "Today" if day in (None, date.today().isoformat()) else day
    console.print(f"\n[bold]{label} — Calories[/bold]")
    console.print(f"[{color}]{bar}[/{color}]")
    left = f"{cal_remaining:.0f} remaining" if cal_remaining >= 0 else f"{-cal_remaining:.0f} over"
    console.print(f"{cal_consumed:.0f} / {allowance:.0f} cal ({pct:.0f}%) — {left}")
    if exercise:
        console.print(f"[bold]Exercise:[/bold] +{exercise:.0f} cal (budget {cal_budget:.0f})")
    burn = summary.get("calorie_burn_target")
    if burn:
        console.print(f"[bold]Plan:[/bold] burn {burn:.0f} − budget {cal_budget:.0f} = "
                      f"{summary.get('calorie_deficit', burn - cal_budget):.0f} cal deficit")

    n = summary.get("nutrients") or {}
    if n:
        console.print(f"[bold]Macros:[/bold] protein {n.get('protein_g', 0):.0f}g · "
                      f"carbs {n.get('carbs_g', 0):.0f}g · fat {n.get('fat_g', 0):.0f}g")

    weight = summary.get("weight")
    if weight:
        goal = summary.get("goal_weight")
        goal_txt = f" (goal {goal:.1f})" if goal else ""
        console.print(f"[bold]Weight:[/bold] {weight:.1f} lbs{goal_txt}")
