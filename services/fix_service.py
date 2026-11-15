"""
/fix logic — turn the problems a scan found into sheet changes.

Kinds of fix:
  add     — player isn't on any tab: add them to Ancillary as RCT
  rename  — username isn't on the sheet but their exact lore name is, on one
            player: they changed username, so update the USERNAME cell
  lore    — username matched but the lore name differs: update the LORE NAME cell

Anything we shouldn't guess at (a likely typo of an existing name, no lore
name in the log, conflicting logs) goes in `review` for a human to look at.
Nothing is written until apply_fixes() is called with the chosen fixes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from models.player import PlayerRow, ScanIssue
from parser.names import clean_name, name_key
from services import sheets_service as sheets

logger = logging.getLogger("bot.fix")

NEW_PLAYER_RANK = "RCT"


@dataclass
class Fix:
    kind: str                      # "add" | "rename" | "lore"
    username: str                  # name to end up with on the sheet
    lore_name: str                 # lore name to end up with on the sheet
    rows: list = field(default_factory=list)   # list[PlayerRow] to change (rename/lore)
    old_value: str = ""            # what the sheet had (rename/lore)
    logs: int = 1                  # how many logs this fixes

    def describe(self) -> str:
        if self.kind == "add":
            where = sheets.ancillary_sheet_name()
            return f"➕ Add **{self.username}** ({self.lore_name}) to {where} as {NEW_PLAYER_RANK}"
        if self.kind == "rename":
            return f"✏️ Rename **{self.old_value}** → **{self.username}** ({self.lore_name})"
        return f"🪪 Lore for **{self.username}**: *{self.old_value}* → *{self.lore_name}*"

    def short(self) -> str:
        """≤100 chars, for a Discord select option."""
        if self.kind == "add":
            text = f"Add {self.username} ({self.lore_name}) to Ancillary"
        elif self.kind == "rename":
            text = f"Rename {self.old_value} → {self.username}"
        else:
            text = f"{self.username}: lore {self.old_value} → {self.lore_name}"
        return text if len(text) <= 100 else text[:99] + "…"


@dataclass
class FixPlan:
    fixes: list = field(default_factory=list)   # list[Fix]
    review: list = field(default_factory=list)  # list[str] — needs a human


def _plural(n: int) -> str:
    return f"{n} log" + ("" if n == 1 else "s")


async def build_plan(issues: list[ScanIssue], cache: sheets.SheetCache | None = None) -> FixPlan:
    plan = FixPlan()
    cache = cache or sheets.SheetCache()

    # Group logs by player so 3 logs from one new player = 1 fix
    not_found: dict[str, list[ScanIssue]] = {}
    mismatched: dict[tuple, list[ScanIssue]] = {}
    for issue in issues:
        if issue.kind == "not_found":
            not_found.setdefault(name_key(issue.username), []).append(issue)
        elif issue.kind == "lore_mismatch" and issue.player is not None:
            key = (issue.player.sheet_name, name_key(issue.player.username))
            mismatched.setdefault(key, []).append(issue)

    # ── Not on the sheet ─────────────────────────────────────────
    for group in not_found.values():
        username = clean_name(group[0].username)
        lores = {name_key(i.lore_name): clean_name(i.lore_name) for i in group if i.lore_name}

        if len(lores) > 1:
            plan.review.append(
                f"❓ **{username}** — logs use different lore names "
                f"({', '.join(sorted(lores.values()))}). Use `/addlog` with the right one."
            )
            continue
        lore = next(iter(lores.values()), "")

        # Same lore name already on the sheet under another username → they renamed
        if lore:
            rows = await sheets.find_players_by_lore(lore, cache=cache)
            owners = {name_key(r.username) for r in rows}
            if len(owners) == 1:
                plan.fixes.append(Fix(
                    kind="rename", username=username, lore_name=lore,
                    rows=rows, old_value=rows[0].username, logs=len(group),
                ))
                continue
            if len(owners) > 1:
                names = ", ".join(sorted({r.username for r in rows}))
                plan.review.append(
                    f"❓ **{username}** ({lore}) — that lore name belongs to several players ({names})."
                )
                continue

        suggestions = group[0].suggestions or await sheets.suggest_usernames(username, cache=cache)
        if suggestions:
            close = ", ".join(f"**{s}**" for s in suggestions)
            plan.review.append(
                f"❓ **{username}** — not added, looks like a typo of {close}. "
                f"Fix the log, or use `/addlog` if they're really new."
            )
            continue
        if not lore:
            plan.review.append(
                f"❓ **{username}** — no lore name in the log, so I can't add them. "
                f"Use `/addlog username:{username}`."
            )
            continue

        plan.fixes.append(Fix(kind="add", username=username, lore_name=lore, logs=len(group)))

    # ── Wrong lore name on the sheet ─────────────────────────────
    for group in mismatched.values():
        player: PlayerRow = group[0].player
        lores = {name_key(i.lore_name): clean_name(i.lore_name) for i in group if i.lore_name}
        if len(lores) != 1:
            plan.review.append(
                f"❓ **{player.username}** — logs use different lore names "
                f"({', '.join(sorted(lores.values()))}); sheet has *{player.lore_name}*."
            )
            continue
        plan.fixes.append(Fix(
            kind="lore", username=player.username, lore_name=next(iter(lores.values())),
            rows=[player], old_value=player.lore_name, logs=len(group),
        ))

    return plan


async def apply_fixes(fixes: list[Fix]) -> tuple[list[str], list[str]]:
    """Write the chosen fixes to the sheet. Returns (done lines, failed lines)."""
    done: list[str] = []
    failed: list[str] = []
    for fix in fixes:
        try:
            if fix.kind == "add":
                await sheets.add_player_to_sheet(
                    username=fix.username,
                    lore_name=fix.lore_name,
                    sheet_name=sheets.ancillary_sheet_name(),
                    rank=NEW_PLAYER_RANK,
                )
            elif fix.kind == "rename":
                for row in fix.rows:
                    await sheets.update_player_names(row, username=fix.username)
            elif fix.kind == "lore":
                for row in fix.rows:
                    await sheets.update_player_names(row, lore_name=fix.lore_name)
            done.append(f"{fix.describe()} — {_plural(fix.logs)}")
        except sheets.PlayerAlreadyExists:
            done.append(f"{fix.describe()} — already there")
        except Exception as exc:
            logger.exception("Fix failed: %s", fix.short())
            failed.append(f"❌ {fix.short()} — {exc}")
    return done, failed
