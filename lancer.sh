#!/bin/bash
# Lance Claude Code à gauche et Ariane à droite, dans une session tmux.
#
#   cca              Claude Code dans le dossier courant
#   cca ~/mon-projet Claude Code dans ce dossier
#
# Si la session existe déjà (connexion SSH coupée, par exemple),
# on la retrouve telle quelle au lieu d'en créer une nouvelle.

SESSION="claude"
DOSSIER="${1:-$PWD}"
ARIANE="$HOME/ariane"

# Se rattacher (ou basculer, si on est déjà dans tmux) à la session
rejoindre() {
    if [ -n "$TMUX" ]; then
        tmux switch-client -t "$SESSION"
    else
        tmux attach -t "$SESSION"
    fi
}

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "🧵 Session « $SESSION » déjà ouverte : je la reprends."
    rejoindre
    exit
fi

if [ ! -d "$DOSSIER" ]; then
    echo "Dossier introuvable : $DOSSIER"
    exit 1
fi

# Panneau de gauche : Claude Code. Quand on le quitte, on garde un terminal.
tmux new-session -d -s "$SESSION" -c "$DOSSIER" \
    -x "$(tput cols)" -y "$(tput lines)" \
    "claude; exec bash"

# Panneau de droite : Ariane, qui ne montre que la nouvelle activité.
tmux split-window -h -t "$SESSION" -l 45% -c "$ARIANE" \
    "$ARIANE/.venv/bin/python $ARIANE/ariane.py --direct; exec bash"

# Le curseur commence dans Claude Code
tmux select-pane -t "$SESSION:0.0"
rejoindre
