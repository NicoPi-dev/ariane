#!/usr/bin/env python3
"""Ajoute (ou retire) les hooks d'Ariane dans ~/.claude/settings.json.

  python3 installer_hooks.py            installe
  python3 installer_hooks.py --retirer  désinstalle

Une sauvegarde settings.json.avant-ariane est faite avant toute modification.
Les autres réglages et les hooks déjà présents ne sont pas touchés.
"""
import json
import shutil
import sys
from pathlib import Path

REGLAGES = Path.home() / ".claude" / "settings.json"
SCRIPT = str(Path(__file__).resolve().parent / "hook.sh")

# Événements suivis. Ceux qui concernent un outil prennent un « matcher ».
EVENEMENTS = {
    "UserPromptSubmit": False,
    "PreToolUse": True,
    "PostToolUse": True,
    "PostToolUseFailure": True,
    "PermissionRequest": True,
    "Notification": False,
    "SubagentStart": False,
    "SubagentStop": False,
    "Stop": False,
}


def est_ariane(groupe):
    return any(h.get("command") == SCRIPT for h in groupe.get("hooks", []))


def main():
    retirer = "--retirer" in sys.argv
    reglages = json.loads(REGLAGES.read_text()) if REGLAGES.exists() else {}

    sauvegarde = REGLAGES.with_name("settings.json.avant-ariane")
    if REGLAGES.exists() and not sauvegarde.exists():   # on garde la version d'origine
        shutil.copy2(REGLAGES, sauvegarde)
        print(f"💾 Sauvegarde : {sauvegarde}")

    hooks = reglages.setdefault("hooks", {})
    for evenement, outil in EVENEMENTS.items():
        groupes = [g for g in hooks.get(evenement, []) if not est_ariane(g)]
        if not retirer:
            groupe = {"hooks": [{"type": "command", "command": SCRIPT, "async": True}]}
            if outil:
                groupe = {"matcher": "*", **groupe}
            groupes.append(groupe)
        if groupes:
            hooks[evenement] = groupes
        else:
            hooks.pop(evenement, None)
    if not hooks:
        reglages.pop("hooks")

    REGLAGES.parent.mkdir(parents=True, exist_ok=True)
    REGLAGES.write_text(json.dumps(reglages, indent=2, ensure_ascii=False) + "\n")
    if retirer:
        print("🧹 Hooks d'Ariane retirés.")
    else:
        print(f"✅ Hooks d'Ariane installés pour {len(EVENEMENTS)} événements.")
        print(f"   Script appelé : {SCRIPT}")
    print("   Relance Claude Code pour que le changement soit pris en compte.")


if __name__ == "__main__":
    main()
