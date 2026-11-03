#!/usr/bin/env python3
"""Ariane — étape 4 : l'interface terminal qui suit Claude Code en direct.

Usage :
  python ariane.py            suit la session active (rejouée depuis le début)
  python ariane.py --direct   n'affiche que les nouvelles actions
  python ariane.py FICHIER    rejoue un fichier .jsonl précis

Touches : q quitter · p pause du défilement · v vider le fil
Clic (ou Entrée) sur un fichier : historique, modifications et contenu.
"""
import difflib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Group
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, OptionList, RichLog, Static
from textual.widgets.option_list import Option

PROJETS = Path.home() / ".claude" / "projects"
HOME = str(Path.home())

LANGAGES = {
    ".py": "Python", ".sh": "Bash", ".js": "JavaScript", ".ts": "TypeScript",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".md": "Markdown",
    ".html": "HTML", ".css": "CSS", ".gd": "GDScript", ".kt": "Kotlin",
    ".toml": "TOML", ".txt": "Texte", ".c": "C", ".cpp": "C++", ".rs": "Rust",
    ".go": "Go", ".bas": "VBA", ".ps1": "PowerShell", ".conf": "Config",
    ".ini": "Config", ".service": "systemd", ".jsonl": "JSONL", ".log": "Log",
}

# Icône, couleur et libellé de chaque type d'action sur un fichier
ACTIONS_FICHIER = {
    "lu": ("📖", "dodger_blue1", "Lu"),
    "bash": ("⚡", "grey62", "Utilisé par une commande"),
    "modifié": ("📝", "dark_orange", "Modifié"),
    "écrit": ("✨", "green3", "Écrit"),
}

# Repère les chemins dans une commande Bash : ~/... ou /...
RE_CHEMIN = re.compile(r"(?<![\w=])(~?/[\w.\-]+(?:/[\w.\-]+)+)")

APERCU_MAX = 300_000          # taille maximale d'un fichier pour l'aperçu
HOOKS = Path.home() / ".cache" / "ariane" / "hooks.jsonl"   # écrit par hook.sh
HOOKS_MAX = 5_000_000         # au-delà, le journal des hooks est vidé au démarrage


# ------------------------------------------------------------------ outils
def session_active():
    fichiers = [f for f in PROJETS.glob("*/*.jsonl")
                if not f.parent.name.startswith("-tmp-")]
    return max(fichiers, key=lambda f: f.stat().st_mtime, default=None)


def court(chemin):
    return chemin.replace(HOME, "~", 1) if chemin else ""


def tronque(texte, n=200):
    texte = " ".join(str(texte).split())
    return texte if len(texte) <= n else texte[: n - 1] + "…"


def nb_lignes(texte):
    return len(texte.splitlines()) if texte else 0


def lire_date(obj):
    try:
        return datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        return None


def heure(date):
    return date.astimezone().strftime("%H:%M:%S") if date else "--:--:--"


def milliers(n):
    return f"{n:_}".replace("_", " ")


def duree(secondes):
    secondes = int(secondes)
    h, reste = divmod(secondes, 3600)
    m, s = divmod(reste, 60)
    return f"{h}h{m:02d}" if h else f"{m:02d}:{s:02d}"


# ------------------------------------------------------------------ moteur
class Moteur:
    """Transforme les lignes du transcript en événements et en compteurs."""

    def __init__(self):
        self.titre = None
        self.dernier_prompt = None
        self.dernier_prompt_date = None
        self.dossier = None
        self.modele = None
        self.debut = None
        self.dernier = None
        self.date_courante = None
        self.tokens_entree = 0
        self.tokens_sortie = 0
        self.messages_vus = set()
        self.compteurs = Counter()
        self.langages = Counter()
        self.fichiers = {}          # chemin -> [action, nombre, ordre]
        self.historique = {}        # chemin -> liste des actions sur ce fichier
        self.ordre = 0

    # ----- fichiers
    def note_fichier(self, chemin, action, **details):
        if not chemin:
            return
        self.ordre += 1
        self.historique.setdefault(chemin, []).append(
            {"date": self.date_courante, "action": action, **details})
        entree = self.fichiers.get(chemin)
        if entree:
            # une modification ou une écriture prime sur une simple lecture
            if action in ("modifié", "écrit") or entree[0] in ("lu", "bash"):
                entree[0] = action
            entree[1] += 1
            entree[2] = self.ordre
        else:
            self.fichiers[chemin] = [action, 1, self.ordre]
            langage = LANGAGES.get(Path(chemin).suffix.lower())
            if langage:
                self.langages[langage] += 1

    def chemins_bash(self, commande, description):
        for brut in RE_CHEMIN.findall(commande):
            if brut.startswith(("/dev/", "/proc/", "/sys/")):
                continue
            complet = os.path.expanduser(brut)
            if os.path.isfile(complet):
                self.note_fichier(court(complet), "bash",
                                  commande=description or tronque(commande, 120))

    # ----- description d'un appel d'outil -> (icône, style, texte, détail)
    def decrire(self, nom, e):
        chemin = court(e.get("file_path") or e.get("notebook_path") or "")
        c = self.compteurs

        if nom == "Read":
            c["📖 Lus"] += 1
            self.note_fichier(chemin, "lu")
            return "📖", "dodger_blue1", f"Lit {chemin}", ""
        if nom in ("Edit", "MultiEdit"):
            c["📝 Modifiés"] += 1
            ed = e.get("edits") or [e]
            plus = sum(nb_lignes(x.get("new_string")) for x in ed)
            moins = sum(nb_lignes(x.get("old_string")) for x in ed)
            self.note_fichier(chemin, "modifié", plus=plus, moins=moins,
                              edits=[(x.get("old_string", ""), x.get("new_string", "")) for x in ed])
            return "📝", "dark_orange", f"Modifie {chemin}", f"+{plus} -{moins}"
        if nom == "Write":
            c["✨ Écrits"] += 1
            lignes = nb_lignes(e.get("content"))
            self.note_fichier(chemin, "écrit", lignes=lignes)
            return "✨", "green3", f"Écrit {chemin}", f"{lignes} lignes"
        if nom == "Bash":
            c["⚡ Bash"] += 1
            cmd = e.get("command", "")
            desc = e.get("description")
            self.chemins_bash(cmd, desc)
            if desc:
                return "⚡", "gold1", desc, "$ " + tronque(cmd)
            return "⚡", "gold1", "$ " + tronque(cmd), ""
        if nom in ("Grep", "Glob"):
            c["🔍 Recherches"] += 1
            ou = court(e.get("path", ""))
            return "🔍", "cyan", f"Cherche « {e.get('pattern', '')} »", f"dans {ou}" if ou else ""
        if nom == "WebSearch":
            c["🌐 Web"] += 1
            return "🌐", "cyan", f"Recherche web « {e.get('query', '')} »", ""
        if nom == "WebFetch":
            c["🌐 Web"] += 1
            return "🌐", "cyan", "Lit une page web", e.get("url", "")
        if nom == "Skill":
            c["🧩 Skills"] += 1
            plugin, _, skill = e.get("skill", "?").rpartition(":")
            return "🧩", "magenta", f"Skill {skill}", plugin
        if nom in ("Task", "Agent"):
            c["🤖 Sous-agents"] += 1
            return "🤖", "magenta", f"Sous-agent {e.get('subagent_type', '')}", e.get("description", "")
        if nom == "TodoWrite":
            c["📋 Tâches"] += 1
            return "📋", "grey62", "Met à jour sa liste de tâches", f"{len(e.get('todos', []))} tâches"
        if nom == "ToolSearch":
            return "🧰", "grey62", "Charge des outils", e.get("query", "")
        if nom.startswith("mcp__"):
            c["🔌 MCP"] += 1
            _, serveur, outil = (nom.split("__", 2) + ["", ""])[:3]
            serveur = serveur.removeprefix("claude_ai_").replace("_", " ")
            return "🔌", "cyan", f"{serveur} → {outil}", ""
        c["🔧 Autres"] += 1
        return "🔧", "grey62", nom, ""

    # ----- lecture d'une ligne -> liste d'événements à afficher
    def traiter(self, ligne):
        try:
            obj = json.loads(ligne)
        except json.JSONDecodeError:
            return []
        if not isinstance(obj, dict):
            return []

        date = lire_date(obj)
        self.date_courante = date
        if date:
            self.debut = self.debut or date
            self.dernier = date
        self.dossier = obj.get("cwd") or self.dossier

        t = obj.get("type")
        if t == "ai-title":
            self.titre = obj.get("aiTitle") or self.titre
        elif t == "assistant":
            return self.traiter_assistant(obj, date)
        elif t == "user" and not obj.get("isMeta"):
            return self.traiter_user(obj, date)
        return []

    def traiter_assistant(self, obj, date):
        msg = obj.get("message") or {}
        mid = msg.get("id")
        if mid and mid not in self.messages_vus:
            self.messages_vus.add(mid)
            modele = msg.get("model")
            if modele and not modele.startswith("<"):
                self.modele = modele
            u = msg.get("usage") or {}
            self.tokens_entree += (u.get("input_tokens", 0)
                                   + u.get("cache_creation_input_tokens", 0)
                                   + u.get("cache_read_input_tokens", 0))
            self.tokens_sortie += u.get("output_tokens", 0)

        evenements = []
        contenu = msg.get("content")
        if isinstance(contenu, list):
            for bloc in contenu:
                if isinstance(bloc, dict) and bloc.get("type") == "tool_use":
                    icone, style, texte, detail = self.decrire(
                        bloc.get("name", "?"), bloc.get("input") or {})
                    evenements.append(("action", date, icone, style, texte, detail))
        return evenements

    def traiter_user(self, obj, date):
        contenu = (obj.get("message") or {}).get("content")
        blocs = [{"type": "text", "text": contenu}] if isinstance(contenu, str) else contenu
        evenements = []
        for bloc in blocs if isinstance(blocs, list) else []:
            if not isinstance(bloc, dict):
                continue
            if bloc.get("type") == "tool_result" and bloc.get("is_error"):
                self.compteurs["❌ Erreurs"] += 1
                evenements.append(("erreur", date))
            elif bloc.get("type") == "text":
                texte = (bloc.get("text") or "").strip()
                if texte and not texte.startswith("<"):
                    self.compteurs["💬 Prompts"] += 1
                    self.dernier_prompt = texte
                    self.dernier_prompt_date = date
                    evenements.append(("prompt", date, tronque(texte, 300)))
        return evenements


# ------------------------------------------------------------------ fenêtre d'un fichier
class EcranFichier(ModalScreen):
    """Fenêtre qui s'ouvre au clic sur un fichier."""

    CSS = """
    EcranFichier { align: center middle; background: $background 60%; }
    #boite {
        width: 92%; height: 90%;
        border: round #4caf50; border-title-color: #4caf50; border-title-style: bold;
        border-subtitle-color: $text-muted;
        background: $surface; padding: 0 1;
    }
    #contenu { height: 1fr; }
    """
    BINDINGS = [("escape", "fermer", "Fermer"), ("q", "fermer", "Fermer")]

    def __init__(self, chemin, historique):
        super().__init__()
        self.chemin = chemin
        self.historique = historique

    def compose(self) -> ComposeResult:
        with Vertical(id="boite"):
            with VerticalScroll(id="contenu"):
                yield Static(self.construire())

    def on_mount(self):
        boite = self.query_one("#boite")
        boite.border_title = f"📄 {Path(self.chemin).name}"
        boite.border_subtitle = "Échap pour fermer · molette pour défiler"

    def construire(self):
        morceaux = [Text(self.chemin, style="grey62"), Text("")]

        # 1. Historique
        morceaux.append(Text("🕓 Historique dans cette session", style="bold cyan"))
        for h in self.historique:
            icone, style, libelle = ACTIONS_FICHIER[h["action"]]
            ligne = Text("   ")
            ligne.append(f"{heure(h['date'])}  ", style="grey50")
            ligne.append(f"{icone} {libelle}", style=style)
            if h["action"] == "modifié":
                ligne.append(f"  +{h['plus']}", style="green")
                ligne.append(f" -{h['moins']}", style="red")
            elif h["action"] == "écrit":
                ligne.append(f"  {h['lignes']} lignes", style="grey50")
            elif h["action"] == "bash":
                ligne.append(f"  · {h['commande']}", style="grey50")
            morceaux.append(ligne)

        # 2. Modifications faites par Claude
        diffs = [h for h in self.historique if h["action"] == "modifié"]
        if diffs:
            morceaux += [Text(""), Text("📝 Modifications de Claude", style="bold dark_orange")]
            for h in diffs:
                for ancien, nouveau in h["edits"]:
                    morceaux.append(Text(f"── {heure(h['date'])} ", style="grey42"))
                    for l in difflib.unified_diff(ancien.splitlines(), nouveau.splitlines(),
                                                  lineterm="", n=2):
                        if l.startswith(("---", "+++")):
                            continue
                        style = ("green" if l.startswith("+") else
                                 "red" if l.startswith("-") else
                                 "cyan" if l.startswith("@@") else "grey62")
                        morceaux.append(Text(l, style=style))

        # 3. Contenu actuel du fichier
        morceaux += [Text(""), Text("📄 Contenu actuel", style="bold green")]
        complet = os.path.expanduser(self.chemin)
        try:
            taille = os.path.getsize(complet)
            if taille > APERCU_MAX:
                morceaux.append(Text(f"Fichier trop gros pour l'aperçu ({taille // 1024} Ko)", style="grey50"))
            else:
                with open(complet, encoding="utf-8", errors="replace") as f:
                    code = f.read()
                lexer = Syntax.guess_lexer(complet, code)
                morceaux.append(Syntax(code, lexer, theme="monokai", line_numbers=True,
                                       word_wrap=True, background_color="default"))
        except OSError:
            morceaux.append(Text("Fichier introuvable (supprimé, ou situé sur une autre machine)", style="grey50"))

        return Group(*morceaux)

    def action_fermer(self):
        self.dismiss()


# ------------------------------------------------------------------ interface
class Ariane(App):
    TITLE = "Ariane"
    CSS = """
    Screen { background: $background; }
    #bandeau { height: 4; padding: 0 1; border: round $accent; }
    #bandeau Static { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    .panneau { padding: 0 1; border-title-style: bold; }
    #haut { height: 10; }
    #compteurs { width: 1fr; height: 100%; border: round #00bcd4; border-title-color: #00bcd4; }
    #fichiers  {
        width: 1fr; height: 100%; border: round #4caf50; border-title-color: #4caf50;
        background: $background; scrollbar-size-vertical: 1;
    }
    #fichiers > .option-list--option-highlighted { background: #2e4d30; }
    #fil       { height: 1fr; border: round #42a5f5; border-title-color: #42a5f5; }
    """
    BINDINGS = [
        ("q", "quit", "Quitter"),
        ("p", "pause", "Pause défilement"),
        ("v", "vider", "Vider le fil"),
    ]
    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, fichier, direct, auto):
        super().__init__()
        self.fichier = fichier
        self.direct = direct
        self.auto = auto
        self.moteur = Moteur()
        self.flux = None
        self.tampon = ""
        self.derniere_verif = 0.0
        self.derniere_activite = 0.0
        self.tour = 0
        self.liste_fichiers = []    # ordre affiché, pour ne reconstruire que si besoin
        self.hooks = None           # flux du journal des hooks
        self.hooks_tampon = ""
        self.reinitialiser_statut()

    def compose(self) -> ComposeResult:
        with Vertical(id="bandeau"):
            yield Static(id="ligne1")
            yield Static(id="ligne2")
        with Horizontal(id="haut"):
            yield Static(id="compteurs", classes="panneau")
            yield OptionList(id="fichiers", classes="panneau")
        yield RichLog(id="fil", classes="panneau", markup=False, wrap=True, auto_scroll=True)
        yield Footer()

    def w(self, selecteur, genre=None):
        """Cherche un élément sur l'écran principal, même si une fenêtre est ouverte par-dessus."""
        ecran = self.screen_stack[0] if self.screen_stack else self.screen
        return ecran.query_one(selecteur, genre) if genre else ecran.query_one(selecteur)

    def on_mount(self):
        self.w("#compteurs").border_title = "📊 Compteurs"
        self.w("#fichiers").border_title = "📂 Fichiers"
        self.w("#fichiers").border_subtitle = "clic pour le détail"
        self.w("#fil").border_title = "🧵 Fil des actions"
        self.ouvrir(self.fichier, depuis_debut=not self.direct)
        self.ouvrir_hooks()
        self.set_interval(0.2, self.sans_plantage(self.lire))
        self.set_interval(0.1, self.sans_plantage(self.animer))

    @staticmethod
    def sans_plantage(fonction):
        """Évite une erreur si l'écran est en train de se fermer."""
        def protegee():
            try:
                fonction()
            except NoMatches:
                pass
        return protegee

    def on_resize(self):
        self.rafraichir_panneaux()

    # ----- lecture du transcript
    def ouvrir(self, fichier, depuis_debut):
        if self.flux:
            self.flux.close()
        self.fichier = fichier
        self.moteur = Moteur()
        self.tampon = ""
        self.liste_fichiers = []
        self.reinitialiser_statut()
        self.flux = open(fichier, encoding="utf-8", errors="replace")
        if not depuis_debut:
            self.flux.seek(0, 2)
        fil = self.w("#fil", RichLog)
        fil.write(Text(f"🧵 Ariane suit : {court(str(fichier))}", style="bold cyan"))
        self.rafraichir_panneaux()

    def lire(self):
        fil = self.w("#fil", RichLog)
        lu = False
        for _ in range(2000):          # limite par tour pour ne pas figer l'écran
            morceau = self.flux.readline()
            if not morceau:
                break
            self.tampon += morceau
            if not self.tampon.endswith("\n"):
                continue
            ligne, self.tampon = self.tampon, ""
            lu = True
            for evt in self.moteur.traiter(ligne):
                self.afficher(fil, evt)
        if lu:
            self.derniere_activite = time.monotonic()
            self.rafraichir_panneaux()
        self.lire_hooks()
        self.verifier_nouvelle_session()

    def verifier_nouvelle_session(self):
        if not self.auto or time.monotonic() - self.derniere_verif < 2:
            return
        self.derniere_verif = time.monotonic()
        nouveau = session_active()
        try:
            inactif = time.time() - self.fichier.stat().st_mtime > 10
        except OSError:
            inactif = True
        if nouveau and nouveau != self.fichier and inactif:
            fil = self.w("#fil", RichLog)
            fil.write(Text("\n🔀 Nouvelle session détectée", style="bold yellow"))
            self.ouvrir(nouveau, depuis_debut=True)

    # ----- affichage d'un événement dans le fil
    def afficher(self, fil, evt):
        genre, date = evt[0], evt[1]
        ligne = Text(no_wrap=True, overflow="ellipsis")
        if genre == "prompt":
            fil.write(Text(""))
            ligne.append(f"{heure(date)}  ", style="grey50")
            ligne.append(f"💬 {evt[2]}", style="bold white")
        elif genre == "erreur":
            ligne.append("          ↳ ❌ Échec", style="bold red")
        else:
            _, _, icone, style, texte, detail = evt
            ligne.append(f"{heure(date)}  ", style="grey50")
            ligne.append(f"{icone} {texte}", style=style)
            if detail:
                if detail.startswith("+") and " -" in detail:
                    plus, moins = detail.split(" ")
                    ligne.append(f"  {plus}", style="green")
                    ligne.append(f" {moins}", style="red")
                else:
                    ligne.append(f"  · {detail}", style="grey42")
        fil.write(ligne)

    # ----- panneaux
    def rafraichir_panneaux(self):
        m = self.moteur
        self.rafraichir_compteurs(m)
        self.rafraichir_fichiers(m)

    def rafraichir_compteurs(self, m):
        elements = m.compteurs.most_common()
        if not elements:
            self.w("#compteurs", Static).update(Text("En attente d'actions…", style="grey50"))
            return
        # Grille à 2 colonnes : Rich calcule les largeurs, rien ne déborde
        grille = Table.grid(expand=True, padding=(0, 1))
        grille.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grille.add_column(justify="right", no_wrap=True, style="bold")
        grille.add_column(width=2)
        grille.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grille.add_column(justify="right", no_wrap=True, style="bold")
        moitie = (len(elements) + 1) // 2
        for i in range(moitie):
            g = elements[i]
            d = elements[i + moitie] if i + moitie < len(elements) else ("", "")
            grille.add_row(g[0], str(g[1]), "", d[0], str(d[1]))
        self.w("#compteurs", Static).update(grille)

    def rafraichir_fichiers(self, m):
        liste = self.w("#fichiers", OptionList)
        recents = sorted(m.fichiers.items(), key=lambda kv: -kv[1][2])
        if not recents:
            liste.clear_options()
            liste.add_option(Option(Text("Aucun fichier touché", style="grey50"), disabled=True))
            return

        options = []
        for chemin, (action, nombre, ordre) in recents:
            icone, style, _ = ACTIONS_FICHIER[action]
            neuf = ordre == m.ordre             # le dernier touché est mis en avant
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append(f"{icone} ", style=style)
            t.append(f" {Path(chemin).name} ", style=f"bold {style}" + (" reverse" if neuf else ""))
            if nombre > 1:
                t.append(f" ×{nombre}", style="grey50")
            options.append(Option(t, id=chemin))

        # On garde la ligne sélectionnée et la position de défilement
        selection = liste.highlighted_option.id if liste.highlighted_option else None
        defilement = liste.scroll_y
        liste.clear_options()
        liste.add_options(options)
        if selection:
            try:
                liste.highlighted = liste.get_option_index(selection)
            except Exception:
                pass
        liste.scroll_to(y=defilement, animate=False)

    def on_option_list_option_selected(self, evt: OptionList.OptionSelected):
        chemin = evt.option.id
        if chemin in self.moteur.historique:
            self.push_screen(EcranFichier(chemin, list(self.moteur.historique[chemin])))

    # ----- hooks : ce que Claude fait en ce moment
    def reinitialiser_statut(self):
        self.etat = None            # None tant qu'aucun hook n'est reçu pour la session
        self.etat_depuis = time.time()
        self.etat_texte = ""
        self.outils_actifs = {}     # tool_use_id -> (libellé, début)
        self.agents_actifs = 0

    def ouvrir_hooks(self):
        HOOKS.parent.mkdir(parents=True, exist_ok=True)
        try:
            if HOOKS.stat().st_size > HOOKS_MAX:
                HOOKS.write_text("")
        except FileNotFoundError:
            HOOKS.touch()
        self.hooks = open(HOOKS, encoding="utf-8", errors="replace")
        self.hooks.seek(0, 2)       # seuls les nouveaux événements comptent

    def lire_hooks(self):
        if not self.hooks:
            return
        for _ in range(500):
            morceau = self.hooks.readline()
            if not morceau:
                return
            self.hooks_tampon += morceau
            if not self.hooks_tampon.endswith("\n"):
                continue
            ligne, self.hooks_tampon = self.hooks_tampon, ""
            try:
                h = json.loads(ligne)
            except json.JSONDecodeError:
                continue
            self.traiter_hook(h)

    @staticmethod
    def libelle_hook(h):
        if h.get("desc"):
            return h["desc"]
        if h.get("file"):
            return f"{h.get('tool', '')} {Path(h['file']).name}"
        if h.get("cmd"):
            return "$ " + h["cmd"]
        nom = h.get("tool") or "?"
        if nom.startswith("mcp__"):
            _, serveur, outil = (nom.split("__", 2) + ["", ""])[:3]
            return f"{serveur.removeprefix('claude_ai_').replace('_', ' ')} → {outil}"
        return nom

    def traiter_hook(self, h):
        evt = h.get("e")
        # Un prompt dans une autre session : on bascule dessus immédiatement
        if h.get("s") != self.fichier.stem:
            chemin = Path(h.get("tp") or "")
            if (self.auto and evt == "UserPromptSubmit" and chemin.exists()
                    and not chemin.parent.name.startswith("-tmp-")):
                fil = self.w("#fil", RichLog)
                fil.write(Text("\n🔀 Nouvelle session détectée", style="bold yellow"))
                self.ouvrir(chemin, depuis_debut=True)
            else:
                return

        moment = h.get("t") or time.time()

        def changer(etat, texte=""):
            self.etat, self.etat_texte, self.etat_depuis = etat, texte, moment

        if evt == "UserPromptSubmit":
            self.outils_actifs.clear()
            changer("reflexion")
        elif evt == "PreToolUse":
            libelle = self.libelle_hook(h)
            if h.get("agent"):
                libelle = f"🤖 {libelle}"
            self.outils_actifs[h.get("id")] = (libelle, moment)
            changer("outil", libelle)
        elif evt in ("PostToolUse", "PostToolUseFailure"):
            self.outils_actifs.pop(h.get("id"), None)
            if self.outils_actifs:      # encore des outils en parallèle
                libelle, debut = list(self.outils_actifs.values())[-1]
                self.etat, self.etat_texte, self.etat_depuis = "outil", libelle, debut
            else:
                changer("reflexion")
        elif evt == "PermissionRequest":
            changer("permission", self.libelle_hook(h))
        elif evt == "Notification":
            if self.etat != "permission":
                changer("attente", h.get("msg") or "")
        elif evt == "SubagentStart":
            self.agents_actifs += 1
        elif evt == "SubagentStop":
            self.agents_actifs = max(0, self.agents_actifs - 1)
        elif evt == "Stop":
            self.outils_actifs.clear()
            self.agents_actifs = 0
            changer("termine")

    def afficher_statut(self, t, actif):
        roue = self.SPINNER[self.tour % len(self.SPINNER)]
        ecoule = duree(max(0, time.time() - self.etat_depuis))
        if self.etat == "outil":
            t.append(f"{roue} ⚙️ {tronque(self.etat_texte, 60)} · {ecoule}  ", style="bold gold1")
        elif self.etat == "reflexion":
            t.append(f"{roue} 🧠 Réfléchit · {ecoule}  ", style="bold green")
        elif self.etat == "permission":
            clignote = "reverse " if (self.tour // 5) % 2 else ""
            t.append(f"✋ Attend ton autorisation : {tronque(self.etat_texte, 50)}", style=f"{clignote}bold red")
            t.append("  ")
        elif self.etat == "attente":
            t.append("💬 Attend ta réponse  ", style="bold cyan")
        elif self.etat == "termine":
            fin = datetime.fromtimestamp(self.etat_depuis).strftime("%H:%M")
            t.append(f"✅ Terminé à {fin}  ", style="green")
        elif actif:                  # sans hooks
            t.append(f"{roue} Claude travaille  ", style="bold green")
        else:
            t.append("💤 En attente  ", style="grey50")
        if self.agents_actifs:
            t.append(f"🤖×{self.agents_actifs}  ", style="magenta")

    # ----- bandeau
    def animer(self):
        m = self.moteur
        self.tour += 1
        if self.etat:
            actif = self.etat in ("outil", "reflexion")
        else:                        # pas de hooks : on devine d'après le transcript
            actif = time.monotonic() - self.derniere_activite < 4

        # Ligne 1 : la session
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("🧵 ARIANE  ", style="bold cyan")
        t.append(f"📌 {m.titre or 'Session sans titre'}  ", style="magenta")
        if m.modele:
            t.append(f"🤖 {m.modele.removeprefix('claude-')}  ", style="white")
        if m.debut:
            fin = datetime.now(timezone.utc) if actif else (m.dernier or m.debut)
            t.append(f"⏳ {duree(max(0, (fin - m.debut).total_seconds()))}  ", style="white")
        if m.dossier:                # en dernier : c'est lui qui est raccourci
            t.append(f"📁 {court(m.dossier)}", style="grey62")
        self.w("#ligne1", Static).update(t)

        # Ligne 2 : statut et tokens toujours visibles, puis la dernière demande
        t = Text(no_wrap=True, overflow="ellipsis")
        self.afficher_statut(t, actif)
        t.append(f"🪙 {milliers(m.tokens_entree + m.tokens_sortie)} tokens   ", style="gold1")
        if m.dernier_prompt:
            if m.dernier_prompt_date:
                t.append(m.dernier_prompt_date.astimezone().strftime("%H:%M "), style="grey50")
            t.append(f"💬 {tronque(m.dernier_prompt, 400)}", style="bold white")
        else:
            t.append("💬 Pas encore de demande", style="grey50")
        self.w("#ligne2", Static).update(t)

    # ----- touches
    def action_pause(self):
        fil = self.w("#fil", RichLog)
        fil.auto_scroll = not fil.auto_scroll
        self.notify("Défilement en pause" if not fil.auto_scroll else "Défilement repris")

    def action_vider(self):
        self.w("#fil", RichLog).clear()


def main():
    args = sys.argv[1:]
    direct = "--direct" in args
    chemins = [a for a in args if not a.startswith("--")]
    fichier = Path(chemins[0]).expanduser() if chemins else session_active()
    if not fichier or not fichier.exists():
        print(f"Aucune session trouvée dans {PROJETS}")
        return
    Ariane(fichier, direct=direct, auto=not chemins).run()


if __name__ == "__main__":
    main()
