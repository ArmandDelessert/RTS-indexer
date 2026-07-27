"""Lecture de ``robots.txt`` avec les wildcards de la spécification Google.

``urllib.robotparser`` de la bibliothèque standard ne gère pas correctement les
motifs ``*`` et ``$``, or ``rts.ch`` s'en sert abondamment (``/*/page/``,
``/*?*date=``, ``/medias/*.html``). D'où cette implémentation.

Règles appliquées :

* le groupe ``User-agent`` le plus spécifique correspondant à notre agent
  l'emporte, à défaut le groupe ``*`` ;
* ``*`` correspond à toute suite de caractères, ``$`` ancre la fin du chemin ;
* entre plusieurs motifs correspondants, **le plus long gagne** ; à longueur
  égale, ``Allow`` l'emporte sur ``Disallow``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import config, net

log = logging.getLogger(__name__)


def _compile(pattern: str) -> re.Pattern[str]:
    """Traduit un motif robots.txt en expression régulière ancrée au début."""
    anchored_end = pattern.endswith("$")
    if anchored_end:
        pattern = pattern[:-1]
    regex = "".join(".*" if char == "*" else re.escape(char) for char in pattern)
    return re.compile(f"^{regex}{'$' if anchored_end else ''}")


@dataclass
class RobotsRules:
    """Règles applicables à un agent pour un hôte."""

    allow: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    disallow: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    crawl_delay: float | None = None

    def allowed(self, path: str) -> bool:
        best_len, best_allowed = -1, True
        for rules, verdict in ((self.allow, True), (self.disallow, False)):
            for pattern, regex in rules:
                if not regex.match(path):
                    continue
                # Le motif le plus long gagne ; à égalité, Allow l'emporte.
                if len(pattern) > best_len or (len(pattern) == best_len and verdict):
                    best_len, best_allowed = len(pattern), verdict
        return best_allowed

    def allowed_url(self, url: str) -> bool:
        return self.allowed(urlsplit(url).path or "/")


def parse(text: str, agent: str = config.USER_AGENT) -> RobotsRules:
    """Analyse le contenu d'un ``robots.txt``.

    Les groupes sont accumulés puis départagés à la fin : un ``User-agent``
    nommant explicitement notre robot prime sur le groupe ``*``.
    """
    token = agent.split("/", 1)[0].lower()
    groups: dict[str, RobotsRules] = {}
    current: list[str] = []
    expecting_agent = True

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "user-agent":
            if not expecting_agent:
                current = []
                expecting_agent = True
            current.append(value.lower())
            groups.setdefault(value.lower(), RobotsRules())
            continue

        if not current:
            continue
        expecting_agent = False

        for name in current:
            rules = groups[name]
            if field_name == "disallow" and value:
                rules.disallow.append((value, _compile(value)))
            elif field_name == "allow" and value:
                rules.allow.append((value, _compile(value)))
            elif field_name == "crawl-delay":
                try:
                    rules.crawl_delay = float(value)
                except ValueError:
                    pass

    for name, rules in groups.items():
        if name and name != "*" and name in token:
            log.info("robots.txt: groupe spécifique %r retenu", name)
            return rules
    return groups.get("*", RobotsRules())


def fetch(host: str) -> RobotsRules:
    """Récupère et analyse le ``robots.txt`` d'un hôte.

    En cas d'échec on retourne des règles vides (tout autorisé) : un robots.txt
    injoignable ne doit pas bloquer l'indexation, mais l'incident est journalisé.
    """
    url = f"https://{host}/robots.txt"
    with net.client() as http:
        response = net.get(http, url, delay=0)
    if response is None:
        log.warning("%s injoignable : aucune restriction appliquée", url)
        return RobotsRules()
    rules = parse(response.text)
    log.info(
        "%s: %d Disallow, %d Allow, crawl-delay=%s",
        url,
        len(rules.disallow),
        len(rules.allow),
        rules.crawl_delay,
    )
    return rules
