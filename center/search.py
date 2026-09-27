"""Поиск объекта по пересечению улиц, названию или адресу ближайшего дома."""
from __future__ import annotations

import re

STOP = {"улица", "ул", "проспект", "пр", "просп", "проезд", "бульвар", "б-р", "переулок", "пер", "шоссе",
        "дом", "д", "на", "у", "и", "x", "х", "переход", "перекресток", "пересечение", "с", "корпус", "к"}
KIND_TITLES = {"crossing": "Переход", "tee": "Т-образный перекрёсток", "cross": "Перекрёсток"}


def tokens(text: str) -> list[str]:
    text = text.lower().replace("ё", "е")
    return [t for t in re.split(r"[^\w/]+", text) if t and t not in STOP]


class SearchIndex:
    def __init__(self, sites: list[dict]):
        self.entries: list[dict] = []
        for s in sites:
            label = s["title"]
            self.entries.append({"type": "site", "site_id": s["id"], "label": label,
                                 "sublabel": KIND_TITLES[s["kind"]] + " · " + ", ".join(s["streets"]),
                                 "tokens": tokens(label + " " + " ".join(s["streets"])), "rank": 0})
            for a in s.get("addresses", []):
                self.entries.append({"type": "address", "site_id": s["id"], "label": a["address"],
                                     "sublabel": f"{a['distance_m']} м до объекта «{label}»",
                                     "tokens": tokens(a["address"]), "rank": 1 + a["distance_m"] / 1000})

    def search(self, query: str, limit: int = 10) -> list[dict]:
        q = tokens(query)
        if not q:
            return []
        found = []
        for e in self.entries:
            if all(any(t.startswith(x) or (x.isdigit() and t.split("/")[0] == x) for t in e["tokens"]) for x in q):
                found.append(e)
        found.sort(key=lambda e: (e["rank"], e["label"]))
        seen, out = set(), []
        for e in found:
            key = (e["type"], e["label"], e["site_id"])
            if key in seen:
                continue
            seen.add(key)
            out.append({k: e[k] for k in ("type", "site_id", "label", "sublabel")})
            if len(out) == limit:
                break
        return out
