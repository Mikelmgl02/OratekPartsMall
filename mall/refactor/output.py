"""Where the refactor's tools write (plan §1.2.3): the main checkout's git-ignored output/refactor/<stage>/ (/app/output/refactor/<stage>
in the containers), or REFACTOR_OUTPUT when it is set. The rehearsal (scripts/refactor_rehearsal.sh) points it at its own folder, so a
rehearsal's manifest, which carries the restored copy's ids and before-values, never lands where a live --rollback would replay it.
Every stage CLI takes its manifest path from manifest_path(stage)."""
import os
from pathlib import Path


def main_checkout():
    """The repository a .claude/worktrees/<name> checkout belongs to (settings.MAIN_CHECKOUT once S0.11 lands, else derived from
    BASE_DIR), so a run from the worktree writes where a run from the main checkout does."""
    from django.conf import settings
    base = Path(getattr(settings, 'MAIN_CHECKOUT', None) or settings.BASE_DIR)
    return base.parents[2] if (base.parent.name, base.parent.parent.name) == ('worktrees', '.claude') else base


def stage_dir(stage):
    override = os.environ.get('REFACTOR_OUTPUT', '').strip()
    return Path(override) if override else main_checkout() / 'output' / 'refactor' / stage


def manifest_path(stage):
    return stage_dir(stage) / 'manifest.jsonl'
