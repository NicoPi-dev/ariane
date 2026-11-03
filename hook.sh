#!/bin/bash
# Hook Ariane : reçoit l'événement de Claude Code (JSON sur l'entrée standard),
# n'en garde que l'essentiel et l'ajoute au journal que lit Ariane.
# N'affiche jamais rien : rien n'est ajouté au contexte de Claude.

DOSSIER="$HOME/.cache/ariane"
mkdir -p "$DOSSIER"

jq -c '{
  t: now,
  e: .hook_event_name,
  s: .session_id,
  tp: .transcript_path,
  id: .tool_use_id,
  tool: .tool_name,
  desc: .tool_input.description,
  cmd: ((.tool_input.command // "") | .[0:200]),
  file: .tool_input.file_path,
  agent: (.agent_type // .subagent_type),
  msg: .message
}' >> "$DOSSIER/hooks.jsonl" 2>/dev/null

exit 0
