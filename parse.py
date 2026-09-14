#!/usr/bin/env python3
"""
Laag 2 — parser: leest raw/ (VLOS-XML + manifest.json) en schrijft data/apb.json,
plus apb-offline.html (viewer met ingesloten data, voor file://-gebruik).

Idempotent: draait altijd from scratch over het hele archief, nooit over het netwerk.

Gebruik:
  parse.py                        dagen uit config.json
  parse.py --dag 2026-06-03 ...   andere dag(en), herhaalbaar
  parse.py --peilmoment 2026-06-04T14:00:00Z
                                  alleen versies die op dat moment (API-tijd, UTC) gepubliceerd waren;
                                  om na te spelen wat er live zichtbaar was
  parse.py --uitvoer _site/demo --notitie "…"
                                  schrijf naar een andere map (data/apb.json + apb-offline.html),
                                  met een notitie die de viewer prominent toont (demo)

Eenheid = fragment: één aaneengesloten bijdrage van één spreker (VLOS <woordvoerder>
of <interrumpant>), of een proceduretekst zonder spreker (schorsing, agendatekst).
Moties die een spreker indient horen bij diens fragment.

Fragment-ID's ({datum}-{nnnn}) worden toegekend in volgorde van eerste verschijnen,
over alle gearchiveerde versies heen in de volgorde waarin de Kamer ze publiceerde.
Zo blijft een ID stabiel als latere versies fragmenten toevoegen of corrigeren; een
fragment dat bij correctie een nieuw VLOS-objectid krijgt, wordt herkend op spreker,
begintijd en tekstgelijkenis en houdt zijn ID. De nieuwste versie bepaalt inhoud,
volgorde en welke fragmenten er zijn.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
DATA = HERE / "data"
CONFIG = HERE / "config.json"
FRACTIES = HERE / "fracties.json"
VIEWER = HERE / "viewer.html"
OFFLINE = HERE / "apb-offline.html"
SAMENVATTINGEN = DATA / "samenvattingen.json"
OPHAALSTATUS = DATA / "ophaalstatus.json"
NS = "{http://www.tweedekamer.nl/ggm/vergaderverslag/v1.0}"
TZ = ZoneInfo("Europe/Amsterdam")
SCHEMA_VERSIE = 1

HANDELINGEN_URL = "https://www.officielebekendmakingen.nl/"
DISCLAIMER = (
    "Dit zijn stenogrammen uit het Gegevensmagazijn van de Tweede Kamer, merendeels ONGECORRIGEERD "
    "(de status staat bij elk fragment). Aan ongecorrigeerde verslagen kunnen geen rechten worden "
    "ontleend en er mag niet letterlijk uit worden geciteerd. De definitieve, citeerbare tekst staat "
    "in de Handelingen op officielebekendmakingen.nl."
)

WEEKDAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
           "september", "oktober", "november", "december"]
KABINET_SOORTEN = {"minister", "staatssecretaris", "minister-president", "viceminister-president"}
BLOKTEKST_SOORT = {"activiteit": "Agendapunt", "activiteithoofd": "Termijn", "activiteitdeel": "Spreekbeurt", "activiteititem": "Besluit"}
MATCH_MAX_DT_S = 180
MATCH_MIN_GELIJKENIS = 0.6


def local(tag: str) -> str:
    return tag.replace(NS, "")


def schoon(s: str) -> str:
    # Alleen XML-witruimte samenvouwen; verder blijft de tekst van de Kamer onaangeroerd.
    return " ".join(s.split())


def txt(el: ET.Element | None, naam: str) -> str | None:
    if el is None:
        return None
    waarde = el.findtext(NS + naam)
    return waarde.strip() if waarde and waarde.strip() else None


def seconden(iso: str | None) -> int | None:
    if not iso:
        return None
    t = datetime.fromisoformat(iso[:19])
    return int(t.timestamp())


def api_sorteersleutel(stempel: str) -> str:
    # ApiGewijzigdOp heeft wisselend aantal decimalen; normaliseer zodat stringsortering klopt.
    # Bestandsnamen hebben '-' in de tijd waar de API ':' gebruikt.
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2})[:-](\d{2})[:-](\d{2})(?:\.(\d+))?", stempel)
    if not m:
        return stempel
    return f"{m[1]}T{m[2]}:{m[3]}:{m[4]}.{(m[5] or '').ljust(7, '0')}"


def tekst_hash(alineas: list[dict]) -> str:
    return hashlib.sha1(json.dumps(alineas, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def platte_tekst(alineas: list[dict]) -> str:
    return "\n".join(a["tekst"] for a in alineas)


# ---------------------------------------------------------------- tekst uit VLOS


def item_tekst(el: ET.Element) -> str:
    return schoon("".join(el.itertext()))


def alineas_uit(tekst_el: ET.Element, soort: str | None = None) -> list[dict]:
    uit: list[dict] = []

    def voeg(t: str, s: str | None) -> None:
        if t:
            uit.append({"tekst": t, "soort": s} if s else {"tekst": t})

    for kind in tekst_el:
        tag = local(kind.tag)
        if tag == "alineagroep":
            uit.extend(alineas_uit(kind, kind.get("type") or soort))
        elif tag == "alinea":
            items: list[str] = []
            for sub in kind:
                if local(sub.tag) == "lijst":
                    voeg(" ".join(i for i in items if i), soort)
                    items = []
                    for li in sub:
                        voeg(item_tekst(li), "lijstitem")
                else:
                    items.append(item_tekst(sub))
            voeg(" ".join(i for i in items if i), soort)
        else:
            voeg(item_tekst(kind), soort)
    return uit


def sprekerlabel(tekst_el: ET.Element | None) -> str | None:
    """'De heer <nadruk>Klaver</nadruk> (GroenLinks-PvdA):' als eerste alineaitem = sprekersaanduiding."""
    if tekst_el is None:
        return None
    alinea = tekst_el.find(NS + "alinea")
    item = alinea.find(NS + "alineaitem") if alinea is not None else None
    if item is None or item.find(NS + "nadruk") is None:
        return None
    t = item_tekst(item)
    return t if t.endswith(":") else None


def spreker_uit(el: ET.Element | None) -> dict | None:
    if el is None:
        return None
    return {
        "objectid": el.get("objectid"),
        "soort": el.get("soort"),
        "aanhef": txt(el, "aanhef"),
        "verslagnaam": txt(el, "verslagnaam"),
        "voornaam": txt(el, "voornaam"),
        "achternaam": txt(el, "achternaam"),
        "weergavenaam": txt(el, "weergavenaam"),
        "functie": txt(el, "functie"),
        "fractie": txt(el, "fractie"),
    }


class Fracties:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.index: dict[str, str] = {}
        for f in cfg["fracties"]:
            for naam in [f["afkorting"], f["naam"], *f["aliassen"]]:
                self.index[self._sleutel(naam)] = f["afkorting"]
        self.onbekend: set[str] = set()

    @staticmethod
    def _sleutel(s: str) -> str:
        return re.sub(r"[\s\-–/]+", "", s).lower()

    def normaliseer(self, s: str | None) -> str | None:
        if not s:
            return None
        gevonden = self.index.get(self._sleutel(s))
        if gevonden is None:
            self.onbekend.add(s)
            return schoon(s)
        return gevonden


def categoriseer(sp: dict | None, is_voorzitter: bool, fracties: Fracties) -> tuple[str, str | None, str]:
    """(categorie, genormaliseerde fractie, groep). Voorzitter en kabinet zijn geen fractie."""
    if sp is None:
        return "procedure", None, "Procedure"
    if is_voorzitter:
        return "voorzitter", None, "Voorzitter"
    soort = (sp.get("soort") or "").lower()
    functie = (sp.get("functie") or "").lower()
    if soort in KABINET_SOORTEN or functie.startswith(("minister", "staatssecretaris")):
        return "kabinet", None, "Kabinet"
    if sp.get("fractie"):
        f = fracties.normaliseer(sp["fractie"])
        return "fractie", f, f
    return "overig", None, "Overig"


# ---------------------------------------------------------------- één document


class Document:
    """Alle fragmenten en blokken uit één VLOS-verslag, in documentvolgorde."""

    def __init__(self, root: ET.Element, fracties: Fracties):
        self.fracties = fracties
        self.fragmenten: list[dict] = []
        self.agendapunten: list[dict] = []
        self.termijnen: list[dict] = []
        self.beurten: list[dict] = []
        self.vergadering = root.find(NS + "vergadering")
        for act in self.vergadering.findall(NS + "activiteit"):
            ap = {
                "vlos_objectid": act.get("objectid"),
                "soort": act.get("soort"),
                "titel": txt(act, "titel"),
                "onderwerp": txt(act, "onderwerp"),
                "begin": txt(act, "aanvangstijd"),
                "eind": txt(act, "eindtijd"),
                "voortzetting": txt(act, "voortzetting") == "true",
                "fragment_keys": [],
            }
            self.agendapunten.append(ap)
            self._loop(act, {"agendapunt": ap, "termijn": None, "beurt": None})

    def _loop(self, el: ET.Element, ctx: dict) -> None:
        for kind in el:
            tag = local(kind.tag)
            if tag == "activiteithoofd":
                tm = {
                    "vlos_objectid": kind.get("objectid"),
                    "soort": kind.get("soort"),
                    "titel": txt(kind, "titel"),
                    "begin": txt(kind, "markeertijdbegin"),
                    "eind": txt(kind, "markeertijdeind"),
                    "agendapunt": ctx["agendapunt"]["vlos_objectid"],
                    "fragment_keys": [],
                }
                self.termijnen.append(tm)
                self._loop(kind, {**ctx, "termijn": tm, "beurt": None})
            elif tag == "activiteitdeel":
                sp = spreker_uit(kind.find(NS + "spreker"))
                eerste_wv = kind.find(f"{NS}activiteititem/{NS}woordvoerder")
                is_vz = eerste_wv is not None and txt(eerste_wv, "isvoorzitter") == "true"
                cat, fractie, groep = categoriseer(sp, is_vz, self.fracties) if sp else ("procedure", None, "Procedure")
                bt = {
                    "vlos_objectid": kind.get("objectid"),
                    "soort": kind.get("soort"),
                    "titel": txt(kind, "titel"),
                    "begin": txt(kind, "markeertijdbegin"),
                    "eind": txt(kind, "markeertijdeind"),
                    "spreker": sp,
                    "naam": (sp or {}).get("verslagnaam"),
                    "categorie": cat,
                    "fractie": fractie,
                    "groep": groep,
                    "is_voorzitter": is_vz,
                    "agendapunt": ctx["agendapunt"]["vlos_objectid"],
                    "termijn": ctx["termijn"]["vlos_objectid"] if ctx["termijn"] else None,
                    "fragment_keys": [],
                }
                self.beurten.append(bt)
                self._loop(kind, {**ctx, "beurt": bt})
            elif tag == "activiteititem":
                self._loop(kind, ctx)
            elif tag == "woordvoerder":
                self._spreker_fragment(kind, "woordvoerder", ctx)
                for intr in kind.findall(NS + "interrumpant"):
                    self._spreker_fragment(intr, "interrumpant", ctx)
            elif tag == "tekst":
                alineas = alineas_uit(kind)
                if alineas:
                    # Introtekst van een blok: de blokduur zou als spreektijd misleiden, dus geen eindtijd.
                    begin = txt(el, "markeertijdbegin") or txt(el, "aanvangstijd")
                    self._procedure_fragment(f"{el.get('objectid')}:tekst", BLOKTEKST_SOORT.get(local(el.tag), local(el.tag)), alineas, begin, None, ctx)
            elif tag == "draadboekfragment":
                t = kind.find(NS + "tekst")
                alineas = alineas_uit(t) if t is not None else []
                if alineas:
                    self._procedure_fragment(kind.get("objectid"), kind.get("soort"), alineas,
                                             txt(kind, "markeertijdbegin"), txt(kind, "markeertijdeind"), ctx)

    def _registreer(self, frag: dict, ctx: dict) -> None:
        frag["_agendapunt"] = ctx["agendapunt"]["vlos_objectid"]
        frag["_termijn"] = ctx["termijn"]["vlos_objectid"] if ctx["termijn"] else None
        frag["_beurt"] = ctx["beurt"]["vlos_objectid"] if ctx["beurt"] else None
        for blok in (ctx["agendapunt"], ctx["termijn"], ctx["beurt"]):
            if blok is not None:
                blok["fragment_keys"].append(frag["key"])
        frag["hash"] = tekst_hash(frag["alineas"])
        self.fragmenten.append(frag)

    def _spreker_fragment(self, el: ET.Element, rol: str, ctx: dict) -> None:
        sp = spreker_uit(el.find(NS + "spreker"))
        is_vz = txt(el, "isvoorzitter") == "true"
        tekst_el = el.find(NS + "tekst")
        alineas = alineas_uit(tekst_el) if tekst_el is not None else []
        for dbf in el.findall(NS + "draadboekfragment"):
            dt = dbf.find(NS + "tekst")
            if dt is not None:
                alineas.extend(alineas_uit(dt, dbf.get("soort")))
        begin = txt(el, "markeertijdbegin")
        markeer_eind = txt(el, "markeertijdeind")
        eind = markeer_eind
        if rol == "woordvoerder":
            # markeertijdeind van een woordvoerder loopt door tot na de interrupties die erop volgen.
            eerste_intr = el.find(NS + "interrumpant")
            intr_begin = txt(eerste_intr, "markeertijdbegin") if eerste_intr is not None else None
            if intr_begin and (not eind or intr_begin < eind):
                eind = intr_begin
        cat, fractie, groep = categoriseer(sp, is_vz, self.fracties)
        if is_vz:
            soort = "voorzitter"
        elif rol == "interrumpant":
            soort = "interruptie"
        else:
            soort = "hoofdtermijn"
        self._registreer({
            "key": el.get("objectid"),
            "type": soort,
            "rol": rol,
            "is_voorzitter": is_vz,
            "is_draad": txt(el, "isdraad") == "true",
            "spreker": sp,
            "naam": (sp or {}).get("verslagnaam"),
            "categorie": cat,
            "fractie": fractie,
            "fractie_origineel": (sp or {}).get("fractie"),
            "groep": groep,
            "functie": (sp or {}).get("functie"),
            "begin": begin,
            "eind": eind,
            "markeertijdeind": markeer_eind,
            "label": sprekerlabel(tekst_el),
            "alineas": alineas,
        }, ctx)

    def _procedure_fragment(self, key: str, soort: str | None, alineas: list[dict], begin: str | None, eind: str | None, ctx: dict) -> None:
        self._registreer({
            "key": key,
            "type": "procedure",
            "rol": "procedure",
            "procedure_soort": soort,
            "is_voorzitter": False,
            "is_draad": True,
            "spreker": None,
            "naam": None,
            "categorie": "procedure",
            "fractie": None,
            "fractie_origineel": None,
            "groep": "Procedure",
            "functie": None,
            "begin": begin,
            "eind": eind,
            "markeertijdeind": eind,
            "label": None,
            "alineas": alineas,
        }, ctx)


# ---------------------------------------------------------------- versies samenvoegen


def zonder_label(f: dict) -> str:
    # De sprekersaanduiding ("De voorzitter:") zou korte fragmenten van dezelfde spreker kunstmatig gelijk maken.
    tekst = platte_tekst(f["alineas"])
    return tekst[len(f["label"]):].strip() if f.get("label") and tekst.startswith(f["label"]) else tekst


def gelijkenis(a: dict, b: dict) -> float:
    ta, tb = zonder_label(a), zonder_label(b)
    if not ta and not tb:
        return 1.0
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    return sm.ratio() if sm.real_quick_ratio() >= MATCH_MIN_GELIJKENIS else 0.0


class Dag:
    """Loopt alle versies van één dag chronologisch door en houdt per fragment de geschiedenis bij."""

    def __init__(self, datum: str):
        self.datum = datum
        self.nummers: dict[str, int] = {}
        self.volgend = 1
        self.alias: dict[str, str] = {}
        self.objectids: dict[str, list[str]] = {}
        self.laatst: dict[str, dict] = {}
        self.eerdere: dict[str, list[dict]] = {}
        self.versies: dict[str, list[dict]] = {}
        self.bron: dict[str, dict] = {}
        self.vergadering_van: dict[str, str] = {}
        self.aanwezig: dict[str, list[str]] = {}
        self.laatste_doc: dict[str, tuple[dict, Document]] = {}
        self.bronverslagen: list[dict] = []

    def canon(self, key: str) -> str:
        return self.alias.get(key, key)

    def _herken(self, verdwenen: list[str], nieuw: list[dict]) -> int:
        """Koppel fragmenten die bij correctie een nieuw objectid kregen aan hun oude ID.

        Deterministisch en globaal: alle kandidaatparen worden gesorteerd op tekstgelijkenis, dan op
        volgorde binnen dezelfde spreker (twee keer "Nee." vlak na elkaar mogen niet wisselen),
        dan op tijdsverschil, en daarna gretig toegewezen.
        """
        if not verdwenen or not nieuw:
            return 0

        def rangen(sleutels: list[tuple]) -> list[int]:
            teller: dict[tuple, int] = {}
            uit = []
            for k in sleutels:
                uit.append(teller.get(k, 0))
                teller[k] = teller.get(k, 0) + 1
            return uit

        def sleutel(f: dict) -> tuple:
            return (f["rol"], (f["spreker"] or {}).get("objectid"))

        oude = [self.laatst[c] for c in verdwenen]
        rang_oud, rang_nieuw = rangen([sleutel(f) for f in oude]), rangen([sleutel(f) for f in nieuw])
        kandidaten = []
        for i, oud in enumerate(oude):
            for j, f in enumerate(nieuw):
                if sleutel(f) != sleutel(oud):
                    continue
                sa, sb = seconden(f["begin"]), seconden(oud["begin"])
                dt = abs(sa - sb) if sa is not None and sb is not None else MATCH_MAX_DT_S
                if dt > MATCH_MAX_DT_S:
                    continue
                g = gelijkenis(oud, f)
                if g < MATCH_MIN_GELIJKENIS:
                    continue
                kandidaten.append((-round(g, 2), abs(rang_oud[i] - rang_nieuw[j]), dt, i, j))
        kandidaten.sort()
        bezet_oud: set[int] = set()
        bezet_nieuw: set[int] = set()
        for _, _, _, i, j in kandidaten:
            if i in bezet_oud or j in bezet_nieuw:
                continue
            self.alias[nieuw[j]["key"]] = verdwenen[i]
            bezet_oud.add(i)
            bezet_nieuw.add(j)
        return len(bezet_oud)

    def verwerk(self, meta: dict, doc: Document) -> None:
        verg = meta["vergadering_id"]
        huidige = {self.canon(f["key"]) for f in doc.fragmenten}
        nieuw = [f for f in doc.fragmenten if self.canon(f["key"]) not in self.nummers]
        verdwenen = [c for c in self.aanwezig.get(verg, []) if c not in huidige]
        herkend = self._herken(verdwenen, nieuw)

        info = {k: meta[k] for k in ("verslag_id", "soort", "status", "api_gewijzigd_op")}
        volgorde = []
        for f in doc.fragmenten:
            c = self.canon(f["key"])
            if c not in self.nummers:
                self.nummers[c] = self.volgend
                self.volgend += 1
            ids = self.objectids.setdefault(c, [])
            if f["key"] not in ids:
                ids.append(f["key"])
            oud = self.laatst.get(c)
            if oud is not None and oud["hash"] != f["hash"] and oud["alineas"]:
                self.eerdere[c] = oud["alineas"]
            self.laatst[c] = f
            self.bron[c] = info
            self.vergadering_van[c] = verg
            hist = self.versies.setdefault(c, [])
            spreker_id = (f["spreker"] or {}).get("objectid")
            kenmerk = (info["soort"], info["status"], f["hash"], spreker_id, f["begin"])
            if not hist or hist[-1]["_kenmerk"] != kenmerk:
                hist.append({**info, "tekst_hash": f["hash"], "spreker_id": spreker_id, "begin": f["begin"], "_kenmerk": kenmerk})
            volgorde.append(c)
        self.aanwezig[verg] = volgorde
        self.laatste_doc[verg] = (meta, doc)
        self.bronverslagen.append({**meta, "fragmenten": len(doc.fragmenten), "herkend_na_nieuw_objectid": herkend})

    def resultaat(self) -> tuple[list[dict], list[dict], dict]:
        fragmenten: list[dict] = []
        vervallen: list[dict] = []
        blokken = {"agendapunten": [], "termijnen": [], "beurten": []}
        vergaderingen = sorted(self.laatste_doc.items(), key=lambda kv: kv[1][0].get("aanvangstijd") or "")
        for verg, (meta, doc) in vergaderingen:
            volgorde = list(self.aanwezig[verg])
            aanwezig = set(volgorde)
            begins = [f["begin"] for f in doc.fragmenten if f["begin"]]
            eindes = [f["eind"] or f["begin"] for f in doc.fragmenten if f["begin"]]
            bereik = (min(begins), max(eindes)) if begins else None
            for c, frag in self.laatst.items():
                if self.vergadering_van[c] != verg or c in aanwezig:
                    continue
                # Buiten het bereik van de nieuwste versie: die versie was deels, fragment blijft staan.
                if bereik and frag["begin"] and not (bereik[0] <= frag["begin"] <= bereik[1]):
                    positie = next((i for i, x in enumerate(volgorde) if (self.laatst[x]["begin"] or "") > frag["begin"]), len(volgorde))
                    volgorde.insert(positie, c)
                else:
                    vervallen.append(self._uitvoer(c, None))
            for c in volgorde:
                fragmenten.append(self._uitvoer(c, len(fragmenten) + 1))
            blokken_doc = self._blokken(doc)
            for soort in blokken:
                blokken[soort].extend(blokken_doc[soort])
        return fragmenten, vervallen, blokken

    def fid(self, c: str) -> str:
        return f"{self.datum}-{self.nummers[c]:04d}"

    def _uitvoer(self, c: str, volgorde: int | None) -> dict:
        f = self.laatst[c]
        hist = self.versies[c]
        wijzigingen = []
        hashes = [h["tekst_hash"] for h in hist if h["tekst_hash"] != tekst_hash([])]
        if len(set(hashes)) > 1:
            wijzigingen.append("tekst")
        if len({h["status"] for h in hist}) > 1:
            wijzigingen.append("status")
        if len({h["spreker_id"] for h in hist}) > 1:
            wijzigingen.append("spreker")
        if len({h["begin"] for h in hist}) > 1:
            wijzigingen.append("tijd")
        begin_s, eind_s = seconden(f["begin"]), seconden(f["eind"])
        uit = {
            "id": self.fid(c),
            "dag": self.datum,
            "volgorde": volgorde,
            **{k: f[k] for k in ("type", "rol", "is_voorzitter", "is_draad", "naam", "categorie", "fractie",
                                 "fractie_origineel", "groep", "functie", "begin", "eind", "markeertijdeind", "label")},
            "duur_s": (eind_s - begin_s) if begin_s is not None and eind_s is not None else None,
            "spreker": f["spreker"],
            "alineas": f["alineas"],
            "agendapunt_id": f["_agendapunt"],
            "termijn_id": f["_termijn"],
            "beurt_id": f["_beurt"],
            "bron": self.bron[c],
            "versies": [{k: v for k, v in h.items() if k != "_kenmerk"} for h in hist],
            "tekst_hash": f["hash"],
            "wijzigingen": wijzigingen,
            "vlos_objectid": f["key"],
        }
        if f.get("procedure_soort"):
            uit["procedure_soort"] = f["procedure_soort"]
        if len(self.objectids[c]) > 1:
            uit["vlos_objectids_eerder"] = [k for k in self.objectids[c] if k != f["key"]]
        if "tekst" in wijzigingen and c in self.eerdere:
            uit["eerdere_alineas"] = self.eerdere[c]
        return uit

    def _blokken(self, doc: Document) -> dict:
        def ids(keys: list[str]) -> list[str]:
            return [self.fid(self.canon(k)) for k in keys]

        def duur(b: dict) -> int | None:
            s, e = seconden(b.get("begin")), seconden(b.get("eind"))
            return e - s if s is not None and e is not None else None

        uit = {"agendapunten": [], "termijnen": [], "beurten": []}
        for soort, lijst in (("agendapunten", doc.agendapunten), ("termijnen", doc.termijnen), ("beurten", doc.beurten)):
            for b in lijst:
                fr = ids(b["fragment_keys"])
                item = {k: v for k, v in b.items() if k != "fragment_keys"}
                item.update({"id": b["vlos_objectid"], "dag": self.datum, "duur_s": duur(b), "fragmenten": fr})
                uit[soort].append(item)
        return uit


# ---------------------------------------------------------------- archief lezen


def laad_json(pad: Path, default):
    try:
        return json.loads(pad.read_text())
    except FileNotFoundError:
        return default


def documenten_voor(datum: str, peilmoment: str | None = None) -> list[dict]:
    manifest = laad_json(RAW / "manifest.json", {"fetches": []})
    per_bestand = {f["bestand"]: f for f in manifest["fetches"] if f.get("bestand")}
    docs = []
    for pad in sorted(RAW.glob(f"{datum}_*.xml")):
        m = per_bestand.get(pad.name)
        if m is not None and not m.get("xml_ok", True):
            print(f"  overgeslagen (ongeldige XML): {pad.name}", file=sys.stderr)
            continue
        try:
            root = ET.parse(pad).getroot()
        except ET.ParseError as e:
            print(f"  overgeslagen ({e}): {pad.name}", file=sys.stderr)
            continue
        vergadering = root.find(NS + "vergadering")
        stempel_bestand = pad.stem.split("_", 2)[2]
        vs = (m or {}).get("verslag", {})
        vg = (m or {}).get("vergadering", {})
        docs.append({
            "pad": pad,
            "root": root,
            "meta": {
                "bestand": pad.name,
                "verslag_id": vs.get("Id") or pad.stem.split("_", 2)[1],
                "vergadering_id": vg.get("Id") or vergadering.get("objectid"),
                "vergadering_titel": vg.get("Titel") or txt(vergadering, "titel"),
                "vergadering_nummer": vg.get("VergaderingNummer") or txt(vergadering, "vergaderingnummer"),
                "aanvangstijd": txt(vergadering, "aanvangstijd"),
                "sluiting": txt(vergadering, "sluiting"),
                "soort": vs.get("Soort") or root.get("soort"),
                "status": vs.get("Status") or root.get("status"),
                "gewijzigd_op": vs.get("GewijzigdOp") or root.get("Timestamp"),
                "api_gewijzigd_op": vs.get("ApiGewijzigdOp") or stempel_bestand,
                "opgehaald_op": (m or {}).get("opgehaald_op"),
                "bytes": pad.stat().st_size,
                "sha256": (m or {}).get("sha256"),
            },
        })
    if peilmoment:
        grens = api_sorteersleutel(peilmoment)
        docs = [d for d in docs if api_sorteersleutel(d["meta"]["api_gewijzigd_op"]) <= grens]
    docs.sort(key=lambda d: (api_sorteersleutel(d["meta"]["api_gewijzigd_op"]), d["meta"]["opgehaald_op"] or ""))
    return docs


def dag_label(datum: str, index: int) -> dict:
    d = date.fromisoformat(datum)
    return {
        "label": f"Dag {index + 1}",
        "lang": f"{WEEKDAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]} {d.year}",
        "kort": f"{WEEKDAGEN[d.weekday()][:2]} {d.day} {MAANDEN[d.month - 1][:3]}",
    }


def bundel(apb: dict | None = None, data_dir: Path = DATA, offline: Path = OFFLINE, met_status: bool = True) -> Path | None:
    """Schrijft apb-offline.html: de viewer met apb.json, samenvattingen en ophaalstatus ingesloten."""
    if not VIEWER.exists():
        return None
    if apb is None:
        apb = laad_json(data_dir / "apb.json", None)
        if apb is None:
            return None
    samenvattingen = laad_json(SAMENVATTINGEN, {"samenvattingen": []})
    status = laad_json(OPHAALSTATUS, {}) if met_status else {}
    html = VIEWER.read_text()

    def inbed(element_id: str, data: dict, html: str) -> str:
        # '</' ontsnappen zodat tekst als '</script>' het script-element niet kan afsluiten.
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        leeg = f'<script type="application/json" id="{element_id}"></script>'
        if leeg not in html:
            raise RuntimeError(f"placeholder {element_id} niet gevonden in viewer.html")
        return html.replace(leeg, f'<script type="application/json" id="{element_id}">{payload}</script>')

    html = inbed("apb-data", apb, html)
    html = inbed("samenvattingen-data", samenvattingen, html)
    html = inbed("status-data", status, html)
    offline.parent.mkdir(parents=True, exist_ok=True)
    tmp = offline.with_name(offline.name + ".tmp")
    tmp.write_text(html)
    os.replace(tmp, offline)
    return offline


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dag", action="append", help="datum JJJJ-MM-DD (herhaalbaar); standaard de dagen uit config.json")
    ap.add_argument("--peilmoment", help="negeer versies met ApiGewijzigdOp na dit ISO-tijdstip (UTC)")
    ap.add_argument("--uitvoer", help="map voor data/apb.json en apb-offline.html (standaard deze map)")
    ap.add_argument("--notitie", help="mededeling die de viewer prominent toont, bijv. bij een demo")
    ap.add_argument("--demo-url", help="relatieve link naar een demo, getoond zolang een dag nog leeg is")
    args = ap.parse_args(argv)
    cfg = laad_json(CONFIG, {})
    dagen = sorted(args.dag or cfg["dagen"])
    fr_cfg = laad_json(FRACTIES, {"fracties": [], "categorieën": {}})
    fracties = Fracties(fr_cfg)

    alle_fragmenten: list[dict] = []
    alle_vervallen: list[dict] = []
    dag_uitvoer: list[dict] = []
    for i, datum in enumerate(dagen):
        docs = documenten_voor(datum, args.peilmoment)
        dag = Dag(datum)
        for d in docs:
            dag.verwerk(d["meta"], Document(d["root"], fracties))
            d["root"] = None
        fragmenten, vervallen, blokken = dag.resultaat() if docs else ([], [], {"agendapunten": [], "termijnen": [], "beurten": []})
        alle_fragmenten.extend(fragmenten)
        alle_vervallen.extend(vervallen)
        vergaderingen = {}
        for b in dag.bronverslagen:
            vergaderingen.setdefault(b["vergadering_id"], {
                "id": b["vergadering_id"], "titel": b["vergadering_titel"], "nummer": b["vergadering_nummer"],
                "aanvangstijd": b["aanvangstijd"], "sluiting": b["sluiting"],
            })["sluiting"] = b["sluiting"]
        actueel = [dag.laatste_doc[v][0] for v in vergaderingen]
        statussen: dict[str, int] = {}
        for f in fragmenten:
            sleutel = f"{f['bron']['status']} / {f['bron']['soort']}"
            statussen[sleutel] = statussen.get(sleutel, 0) + 1
        dag_uitvoer.append({
            "datum": datum,
            **dag_label(datum, i),
            "vergaderingen": list(vergaderingen.values()),
            "bronverslagen": dag.bronverslagen,
            "actuele_bronnen": [{k: m[k] for k in ("verslag_id", "soort", "status", "gewijzigd_op", "api_gewijzigd_op", "opgehaald_op")} for m in actueel],
            "aantal_fragmenten": len(fragmenten),
            "statussen": statussen,
            "laatste_markeertijd": max((f["eind"] or f["begin"] or "" for f in fragmenten), default=None),
            **blokken,
        })

    gebruikt: dict[str, int] = {}
    for f in alle_fragmenten:
        gebruikt[f["groep"]] = gebruikt.get(f["groep"], 0) + 1
    fractie_tabel = []
    for f in fr_cfg["fracties"]:
        if f["afkorting"] in gebruikt:
            fractie_tabel.append({"groep": f["afkorting"], "naam": f["naam"], "kleur": f["kleur"], "categorie": "fractie", "fragmenten": gebruikt[f["afkorting"]]})
    for naam in sorted(fracties.onbekend):
        groep = fracties.normaliseer(naam)
        if groep in gebruikt and all(x["groep"] != groep for x in fractie_tabel):
            fractie_tabel.append({"groep": groep, "naam": groep, "kleur": None, "categorie": "fractie", "fragmenten": gebruikt[groep]})
    for cat, c in fr_cfg.get("categorieën", {}).items():
        if cat in gebruikt:
            fractie_tabel.append({"groep": cat, "naam": cat, "kleur": c["kleur"], "categorie": cat.lower(), "fragmenten": gebruikt[cat]})

    inhoud = json.dumps([alle_fragmenten, alle_vervallen], ensure_ascii=False, sort_keys=True)
    opgehaald = [b["opgehaald_op"] for d in dag_uitvoer for b in d["bronverslagen"] if b.get("opgehaald_op")]
    apb = {
        "meta": {
            "schema_versie": SCHEMA_VERSIE,
            "titel": "Algemene Politieke Beschouwingen — werkversie verslagen",
            "gegenereerd_op": datetime.now(TZ).isoformat(timespec="seconds"),
            "laatst_opgehaald_op": max(opgehaald) if opgehaald else None,
            "inhoud_hash": hashlib.sha1(inhoud.encode()).hexdigest()[:16],
            "bron": "Gegevensmagazijn Tweede Kamer (OData v4), VLOS-verslagen",
            "disclaimer": DISCLAIMER,
            "handelingen_url": HANDELINGEN_URL,
            "dagen": dagen,
            "peilmoment": args.peilmoment,
            "notitie": args.notitie,
            "demo_url": args.demo_url,
            "onbekende_fracties": sorted(fracties.onbekend),
        },
        "fracties": fractie_tabel,
        "dagen": dag_uitvoer,
        "fragmenten": alle_fragmenten,
        "vervallen": alle_vervallen,
    }
    data_dir = Path(args.uitvoer) / "data" if args.uitvoer else DATA
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / "apb.json.tmp"
    tmp.write_text(json.dumps(apb, ensure_ascii=False, indent=1))
    os.replace(tmp, data_dir / "apb.json")
    offline = bundel(apb, data_dir, Path(args.uitvoer) / "apb-offline.html" if args.uitvoer else OFFLINE,
                     met_status=not args.uitvoer)

    for d in dag_uitvoer:
        bron = ", ".join(f"{b['soort']}/{b['status']}" for b in d["actuele_bronnen"]) or "geen verslag"
        print(f"{d['datum']}: {d['aantal_fragmenten']} fragmenten uit {len(d['bronverslagen'])} versie(s), actueel {bron}")
    if fracties.onbekend:
        print(f"LET OP onbekende fracties (voeg toe aan fracties.json): {sorted(fracties.onbekend)}", file=sys.stderr)
    print(f"{data_dir / 'apb.json'}: {len(alle_fragmenten)} fragmenten, {len(alle_vervallen)} vervallen"
          + (f"; {offline.name} bijgewerkt" if offline else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
