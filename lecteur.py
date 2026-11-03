#!/usr/bin/env python3
"""Ariane — étape 2 : lecteur des transcripts de Claude Code.

Usage :
  python3 lecteur.py            rejoue la session active puis la suit en direct
  python3 lecteur.py --direct   n'affiche que les nouvelles actions
  python3 lecteur.py FICHIER    rejoue un fichier .jsonl précis
Ctrl+C affiche le bilan et quitte.
"""
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJETS = Path.home() / ".claude" / "projects"
HOME = str(Path.home())

R, GRAS, GRIS = "\033[0m", "\033[1m", "\033[90m"
BLEU, VERT, ORANGE = "\033[94m", "\033[92m", "\033[33m"
ROUGE, VIOLET, CYAN, JAUNE = "\033[91m", "\033[95m", "\033[96m", "\033[93m"

LANGAGES = {
    ".py": "Python", ".sh": "Bash", ".js": "JavaScript", ".ts": "TypeScript",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".md": "Markdown",
    ".html": "HTML", ".css": "CSS", ".gd": "GDScript", ".kt": "Kotlin",
    ".toml": "TOML", ".txt": "Texte", ".c": "C", ".cpp": "C++", ".rs": "Rust",
    ".go": "Go", ".bas": "VBA", ".ps1": "PowerShell", ".conf": "Config",
    ".ini": "Config", ".service": "systemd", ".jsonl": "JSONL",
}


def session_active():
    """Le fichier de session le plus récent, hors dossiers temporaires."""
    fichiers = [f for f in PROJETS.glob("*/*.jsonl")
                if not f.parent.name.startswith("-tmp-")]
    return max(fichiers, key=lambda f: f.stat().st_mtime, default=None)


def court(chemin):
    return chemin.replace(HOME, "~", 1) if chemin else ""


def tronque(texte, n=90):
    texte = " ".join(str(texte).split())
    return texte if len(texte) <= n else texte[: n - 1] + "…"


def nb_lignes(texte):
    return len(texte.splitlines()) if texte else 0


def heure(obj):
    try:
        ts = obj["timestamp"].replace("Z", "+00:00")
        return datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")
    except (KeyError, AttributeError, ValueError):
        return "--:--:--"


class Lecteur:
    def __init__(self):
        self.titre = None
        self.dossier = None
        self.modeles = Counter()
        self.cout = None
        self.tokens_entree = 0
        self.tokens_sortie = 0
        self.messages_vus = set()
        self.compteurs = Counter()
        self.langages = Counter()
        self.fichiers = set()

    # ---------- Description des actions ----------
    def note_fichier(self, chemin):
        if not chemin:
            return
        self.fichiers.add(chemin)
        langage = LANGAGES.get(Path(chemin).suffix.lower())
        if langage:
            self.langages[langage] += 1

    def decrire(self, nom, e):
        """Renvoie (couleur, icône, texte) pour un appel d'outil."""
        chemin = e.get("file_path") or e.get("notebook_path") or ""
        self.note_fichier(chemin)
        c = self.compteurs

        if nom == "Read":
            c["👁  Lus"] += 1
            return BLEU, "👁 ", f"Lit {court(chemin)}"
        if nom == "Edit":
            c["✏️  Modifiés"] += 1
            plus, moins = nb_lignes(e.get("new_string")), nb_lignes(e.get("old_string"))
            return ORANGE, "✏️ ", f"Modifie {court(chemin)}  {VERT}+{plus} {ROUGE}-{moins}"
        if nom == "MultiEdit":
            c["✏️  Modifiés"] += 1
            ed = e.get("edits", [])
            plus = sum(nb_lignes(x.get("new_string")) for x in ed)
            moins = sum(nb_lignes(x.get("old_string")) for x in ed)
            return ORANGE, "✏️ ", f"Modifie {court(chemin)} ({len(ed)} endroits)  {VERT}+{plus} {ROUGE}-{moins}"
        if nom == "Write":
            c["✨ Écrits"] += 1
            return VERT, "✨", f"Écrit {court(chemin)}  ({nb_lignes(e.get('content'))} lignes)"
        if nom == "Bash":
            c["⚡ Bash"] += 1
            cmd = tronque(e.get("command", ""), 70)
            desc = e.get("description")
            return JAUNE, "⚡", f"{desc}  {GRIS}$ {cmd}" if desc else f"$ {cmd}"
        if nom in ("Grep", "Glob"):
            c["🔍 Recherches"] += 1
            ou = court(e.get("path", ""))
            return CYAN, "🔍", f"Cherche « {e.get('pattern', '')} »" + (f" dans {ou}" if ou else "")
        if nom == "WebSearch":
            c["🌐 Web"] += 1
            return CYAN, "🌐", f"Recherche web « {e.get('query', '')} »"
        if nom == "WebFetch":
            c["🌐 Web"] += 1
            return CYAN, "🌐", f"Lit la page {e.get('url', '')}"
        if nom == "Skill":
            c["🧩 Skills"] += 1
            plugin, _, skill = e.get("skill", "?").rpartition(":")
            return VIOLET, "🧩", f"Skill {GRAS}{skill}{R}{VIOLET}" + (f" {GRIS}({plugin})" if plugin else "")
        if nom in ("Task", "Agent"):
            c["🤖 Sous-agents"] += 1
            return VIOLET, "🤖", f"Sous-agent {e.get('subagent_type', '')} : {e.get('description', '')}"
        if nom == "TodoWrite":
            c["📋 Tâches"] += 1
            return GRIS, "📋", f"Met à jour sa liste de tâches ({len(e.get('todos', []))})"
        if nom == "ToolSearch":
            return GRIS, "🧰", f"Charge des outils : {e.get('query', '')}"
        if nom.startswith("mcp__"):
            c["🔌 MCP"] += 1
            _, serveur, outil = (nom.split("__", 2) + ["", ""])[:3]
            serveur = serveur.removeprefix("claude_ai_").replace("_", " ")
            return CYAN, "🔌", f"{serveur} → {outil}"
        c["🔧 Autres"] += 1
        return GRIS, "🔧", nom

    # ---------- Lecture des lignes ----------
    def traiter(self, ligne):
        try:
            obj = json.loads(ligne)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict):
            return

        cwd = obj.get("cwd")
        if cwd and cwd != self.dossier:
            self.dossier = cwd
            print(f"{GRIS}📁 Dossier : {court(cwd)}{R}")

        t = obj.get("type")
        if t == "ai-title":
            titre = obj.get("aiTitle")
            if titre and titre != self.titre:
                self.titre = titre
                print(f"\n{GRAS}{VIOLET}📌 {titre}{R}")
        elif t == "cost-state":
            self.cout = obj.get("totalCostUSD", self.cout)
        elif t == "assistant":
            self.traiter_assistant(obj)
        elif t == "user" and not obj.get("isMeta"):
            self.traiter_user(obj)

    def traiter_assistant(self, obj):
        msg = obj.get("message") or {}
        mid = msg.get("id")
        # Une même réponse peut être écrite sur plusieurs lignes : on ne compte qu'une fois
        if mid and mid not in self.messages_vus:
            self.messages_vus.add(mid)
            self.modeles[msg.get("model", "?")] += 1
            u = msg.get("usage") or {}
            self.tokens_entree += (u.get("input_tokens", 0)
                                   + u.get("cache_creation_input_tokens", 0)
                                   + u.get("cache_read_input_tokens", 0))
            self.tokens_sortie += u.get("output_tokens", 0)

        contenu = msg.get("content")
        if not isinstance(contenu, list):
            return
        for bloc in contenu:
            if isinstance(bloc, dict) and bloc.get("type") == "tool_use":
                couleur, icone, texte = self.decrire(bloc.get("name", "?"), bloc.get("input") or {})
                print(f"{GRIS}{heure(obj)}{R}  {couleur}{icone} {texte}{R}")

    def traiter_user(self, obj):
        contenu = (obj.get("message") or {}).get("content")
        if isinstance(contenu, str):
            self.prompt(obj, contenu)
            return
        if not isinstance(contenu, list):
            return
        for bloc in contenu:
            if not isinstance(bloc, dict):
                continue
            if bloc.get("type") == "tool_result" and bloc.get("is_error"):
                self.compteurs["❌ Erreurs"] += 1
                print(f"          {ROUGE}↳ ❌ Échec{R}")
            elif bloc.get("type") == "text":
                self.prompt(obj, bloc.get("text", ""))

    def prompt(self, obj, texte):
        texte = texte.strip()
        if not texte or texte.startswith("<"):   # messages techniques internes
            return
        self.compteurs["💬 Prompts"] += 1
        print(f"\n{GRIS}{heure(obj)}{R}  {GRAS}💬 {tronque(texte, 100)}{R}")

    # ---------- Bilan ----------
    def bilan(self):
        print(f"\n{GRAS}── Bilan ──────────────────────────────────{R}")
        print(f"📌 {self.titre or 'Sans titre'}")
        if self.modeles:
            print("🧠 Modèles : " + ", ".join(f"{m} ×{n}" for m, n in self.modeles.most_common()))
        print(f"🪙 Tokens : {self.tokens_entree:_} en entrée (cache compris) / {self.tokens_sortie:_} en sortie".replace("_", " "))
        if self.cout is not None:
            print(f"💶 Coût équivalent API : {self.cout:.2f} $")
        for nom, n in self.compteurs.most_common():
            print(f"   {nom} : {n}")
        print(f"📂 Fichiers différents touchés : {len(self.fichiers)}")
        total = sum(self.langages.values())
        for langage, n in self.langages.most_common():
            print(f"   {langage:<11} {'█' * max(1, round(20 * n / total))} {100 * n // total} %")


def suivre(lecteur, fichier, depuis_debut, auto):
    """Lit le fichier puis attend la suite. Renvoie le fichier d'une nouvelle session."""
    with open(fichier, encoding="utf-8", errors="replace") as f:
        if not depuis_debut:
            f.seek(0, 2)
        tampon = ""
        derniere_verif = time.monotonic()
        while True:
            morceau = f.readline()
            if morceau:
                tampon += morceau
                if tampon.endswith("\n"):      # ligne complète seulement
                    lecteur.traiter(tampon)
                    tampon = ""
                continue
            time.sleep(0.2)
            if auto and time.monotonic() - derniere_verif > 2:
                derniere_verif = time.monotonic()
                nouveau = session_active()
                inactif = time.time() - fichier.stat().st_mtime > 10
                if nouveau and nouveau != fichier and inactif:
                    print(f"\n{JAUNE}🔀 Nouvelle session détectée{R}")
                    return nouveau


def main():
    args = sys.argv[1:]
    direct = "--direct" in args
    chemins = [a for a in args if not a.startswith("--")]
    fichier = Path(chemins[0]).expanduser() if chemins else session_active()
    auto = not chemins
    if not fichier or not fichier.exists():
        print(f"Aucune session trouvée dans {PROJETS}")
        return
    lecteur = Lecteur()
    try:
        while True:
            print(f"\n{GRAS}{CYAN}🧵 Ariane suit : {court(str(fichier))}{R}")
            fichier = suivre(lecteur, fichier, depuis_debut=not direct, auto=auto)
            lecteur.bilan()
            lecteur = Lecteur()
            direct = False            # une nouvelle session se lit en entier
    except KeyboardInterrupt:
        lecteur.bilan()


if __name__ == "__main__":
    main()
