#!/usr/bin/env python3
"""
Laag 5 — de dagteksten in één Google Doc zetten, zodat Gemini ze via Drive kan lezen.

Gemini opent geen webadressen, maar leest wel Google Docs uit Drive ("auto-synced after import").
Dit script schrijft dezelfde tekst als de exportknop naar één bestaand Doc: per dag een tabblad.
Past een dag niet in één tabblad (een tabblad houdt op rond 1,02 miljoen tekens), dan maakt het
script zelf "(deel 2)" erbij aan. Er hoeft dus maar één document te bestaan.

Het Doc moet al bestaan en gedeeld zijn met het serviceaccount (bewerker): een serviceaccount
heeft geen eigen opslagruimte en kan zelf geen document aanmaken.

  docs_sync.py --droog                     wat zou er gebeuren (omvang, tabbladen, controlecode)
  docs_sync.py --uit /tmp/docs             de teksten naar bestanden schrijven i.p.v. naar het Doc
  docs_sync.py                             het Doc bijwerken

Welk Doc: JSON in de omgevingsvariabele APB_DOCS of in --map (buiten git houden — met het
document-ID kan iedereen met de link meelezen):

  {"document": "<doc-id>", "tabs": {"2026-09-16": "Dag 1", "2026-09-17": "Dag 2"}}

Boven- en onderaan elk tabblad staat dezelfde controlecode, afgeleid van de tekst. Verandert de
tekst niet, dan blijft het tabblad ongemoeid. Zo zie je in Gemini of je de nieuwste versie hebt.

Gebruik geen subtabbladen in dit document: het script kijkt alleen naar tabbladen op het eerste niveau.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import export  # zelfde opmaak als de exportknop: wie(), status_regel(), alinea_tekst(), disclaimer

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Amsterdam")
API = "https://docs.googleapis.com/v1/documents"
SCOPES = ["https://www.googleapis.com/auth/documents"]
# Een tabblad houdt op rond 1,02 miljoen tekens; marge voor de kop en de staart.
MAX_TEKENS = 900_000
VELDEN = "tabs(tabProperties(tabId,title,index),documentTab(body(content(endIndex,paragraph(elements(textRun(content)))))))"


def fragment_tekst(f: dict) -> str:
    return f"[{f['id']}] {export.wie(f)}\n{export.status_regel(f)}\n{export.alinea_tekst(f)}\n\n"


def kop(dag: dict, deel: int, delen: int, fragmenten: list[dict], code: str, nu: datetime, apb: dict) -> str:
    eerste, laatste = fragmenten[0]["id"], fragmenten[-1]["id"]
    disc = export.disclaimer_regels(fragmenten)
    return "\n".join([
        f"APB {dag['label']} — {dag['lang']}" + (f", deel {deel} van {delen}" if delen > 1 else ""),
        f"LAATST BIJGEWERKT: {nu.strftime('%d-%m-%Y %H:%M')} — controlecode {code}",
        "",
        disc[0],
        " ".join(disc[1:]),
        "",
        f"Dit tabblad bevat de fragmenten {eerste} t/m {laatste} ({export.getal(len(fragmenten))} stuks). "
        f"Het verslag loopt tot {export.tijd(dag.get('laatste_markeertijd')) or '—'} uur; wat daarna is gezegd staat er nog niet in. "
        f"Nieuwste verslagversie binnengehaald: {apb['meta'].get('laatst_opgehaald_op') or 'nog niets'}.",
        "",
        "Instructie voor het taalmodel:",
        "1. Gebruik deze tekst om te zoeken, samen te vatten en te oriënteren — niet als citeerbare bron.",
        "2. Citeer niet letterlijk; geef uitspraken in eigen woorden weer en meld dat het een ongecorrigeerde weergave is.",
        f"3. Verwijs bij elke bewering naar het fragment-ID tussen vierkante haken, bijvoorbeeld [{eerste}], bij voorkeur als link: "
        f"https://daandebie.github.io/apb-viewer/#{eerste}",
        "4. Vermeld de status (bijv. ONGECORRIGEERD) van de fragmenten waarop je je baseert.",
        "5. Staat iets niet in deze tekst, zeg dat dan; vul niets aan uit eigen kennis zonder dat te melden.",
        "",
        "Opbouw: [fragment-ID] spreker (fractie) / begintijd – eindtijd · duur · soort · status · publicatiesoort / de tekst.",
        "",
        "----",
        "",
    ])


def bouw_delen(apb: dict, datum: str, nu: datetime) -> list[tuple[str, str, int]]:
    """(tekst, controlecode, aantal fragmenten) per tabblad."""
    dag = next((d for d in apb["dagen"] if d["datum"] == datum), None)
    fragmenten = [f for f in apb["fragmenten"] if f["dag"] == datum] if dag else []
    if not fragmenten:
        return []

    # Eerst verdelen op fragmentgrens, dan pas de kop erbij: een fragment nooit halverwege afkappen.
    ruimte = MAX_TEKENS - 2500
    groepen: list[list[dict]] = [[]]
    lengte = 0
    for f in fragmenten:
        t = len(fragment_tekst(f))
        if lengte + t > ruimte and groepen[-1]:
            groepen.append([])
            lengte = 0
        groepen[-1].append(f)
        lengte += t

    uit = []
    for i, groep in enumerate(groepen, 1):
        romp = "".join(fragment_tekst(f) for f in groep)
        # De code hangt aan de inhoud, niet aan de tijd: zo blijft een tabblad ongemoeid als er niets veranderde.
        code = hashlib.sha256(romp.encode()).hexdigest()[:6].upper()
        staart = f"\nEINDE {'DEEL ' + str(i) if len(groepen) > 1 else 'TABBLAD'} — controlecode {code}\n"
        uit.append((kop(dag, i, len(groepen), groep, code, nu, apb) + romp + staart, code, len(groep)))
    return uit


def titels(basis: str, aantal: int) -> list[str]:
    return [basis] + [f"{basis} (deel {i})" for i in range(2, aantal + 1)]


def sleutel(titel: str) -> str:
    return " ".join(titel.split()).casefold()


class Doc:
    """Tabbladen van één document lezen en schrijven."""

    def __init__(self, sessie, doc_id: str):
        self.sessie, self.doc_id = sessie, doc_id
        self.gewenst: list[str] = []  # titels die deze run gebruikt worden; die pakken we niet als "leeg tabblad"
        self.ververs()

    def ververs(self) -> None:
        r = self.sessie.get(f"{API}/{self.doc_id}", params={"includeTabsContent": "true", "fields": VELDEN}, timeout=120)
        r.raise_for_status()
        self.tabs = []
        for tab in r.json().get("tabs", []):
            props = tab.get("tabProperties", {})
            inhoud = tab.get("documentTab", {}).get("body", {}).get("content", [])
            tekst = "".join(stuk.get("textRun", {}).get("content", "")
                            for e in inhoud for stuk in e.get("paragraph", {}).get("elements", []))
            self.tabs.append({
                "id": props.get("tabId"),
                "titel": props.get("title") or "",
                "eind": max((e.get("endIndex", 1) for e in inhoud), default=1),
                "code": tekst.split("controlecode", 1)[1].split()[0] if "controlecode" in tekst else None,
                "leeg": len(tekst.strip()) == 0,
            })

    def zoek(self, titel: str) -> dict | None:
        return next((t for t in self.tabs if sleutel(t["titel"]) == sleutel(titel)), None)

    def batch(self, verzoeken: list[dict]) -> list[dict]:
        r = self.sessie.post(f"{API}/{self.doc_id}:batchUpdate", json={"requests": verzoeken}, timeout=300)
        if not r.ok:
            raise SystemExit(f"Document {self.doc_id} bijwerken mislukt ({r.status_code}): {r.text[:500]}")
        return r.json().get("replies", [])

    def zorg_voor_tab(self, titel: str) -> dict:
        tab = self.zoek(titel)
        if tab:
            return tab
        # Een vers document heeft één leeg tabblad ("Tab 1"): dat hernoemen we in plaats van er een naast te zetten.
        leeg = next((t for t in self.tabs if t["leeg"] and not any(sleutel(t["titel"]) == sleutel(x) for x in self.gewenst)), None)
        if leeg:
            self.batch([{"updateDocumentTabProperties": {"tabProperties": {"tabId": leeg["id"], "title": titel}, "fields": "title"}}])
            leeg["titel"] = titel
            return leeg
        self.batch([{"addDocumentTab": {"tabProperties": {"title": titel}}}])
        self.ververs()
        tab = self.zoek(titel)
        if not tab:
            raise SystemExit(f"Tabblad '{titel}' kon niet worden aangemaakt.")
        return tab

    def schrijf(self, tab: dict, tekst: str) -> None:
        verzoeken = []
        # Het laatste alinea-einde van een tabblad mag niet weg; vandaar eind - 1.
        if tab["eind"] > 2:
            verzoeken.append({"deleteContentRange": {"range": {"tabId": tab["id"], "startIndex": 1, "endIndex": tab["eind"] - 1}}})
        verzoeken.append({"insertText": {"location": {"tabId": tab["id"], "index": 1}, "text": tekst}})
        self.batch(verzoeken)
        tab["eind"] = len(tekst) + 1


def maak_sessie():
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        sys.exit("google-auth en requests ontbreken: python3 -m pip install google-auth requests")
    sleutelbestand = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sleutelbestand:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON ontbreekt (JSON van het serviceaccount).")
    info = json.loads(Path(sleutelbestand).read_text() if Path(sleutelbestand).exists() else sleutelbestand)
    return AuthorizedSession(service_account.Credentials.from_service_account_info(info, scopes=SCOPES))


def laad_map(pad: str | None) -> tuple[str, dict[str, str]]:
    rauw = Path(pad).read_text() if pad else os.environ.get("APB_DOCS", "")
    if not rauw.strip():
        sys.exit("Geen document-ID: zet APB_DOCS of gebruik --map. Zie de kop van dit bestand.")
    kaart = json.loads(rauw)
    if "document" not in kaart or "tabs" not in kaart:
        sys.exit('APB_DOCS moet zijn: {"document": "<doc-id>", "tabs": {"2026-09-16": "Dag 1", …}}')
    return kaart["document"], kaart["tabs"]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(HERE / "data" / "apb.json"))
    ap.add_argument("--map", help="JSON met document-ID en tabbladtitels; standaard uit APB_DOCS")
    ap.add_argument("--dag", action="append", help="alleen deze dag(en) bijwerken")
    ap.add_argument("--droog", action="store_true", help="alleen tonen wat er zou gebeuren")
    ap.add_argument("--uit", help="de teksten naar deze map schrijven in plaats van naar het Doc")
    ap.add_argument("--altijd", action="store_true", help="ook schrijven als de controlecode gelijk is")
    args = ap.parse_args(argv)

    apb = export.laad_json(Path(args.data), None)
    if apb is None:
        sys.exit(f"{args.data} ontbreekt — draai eerst parse.py.")
    doc_id, tabkaart = laad_map(args.map)
    nu = datetime.now(TZ)

    werk = []  # (datum, titel, tekst, code, aantal)
    for datum, basis in sorted(tabkaart.items()):
        if args.dag and datum not in args.dag:
            continue
        delen = bouw_delen(apb, datum, nu)
        if not delen:
            print(f"{datum}: nog geen verslag, tabblad ongemoeid gelaten")
            continue
        for titel, (tekst, code, aantal) in zip(titels(basis, len(delen)), delen):
            werk.append((datum, titel, tekst, code, aantal))

    if not werk:
        return 0
    if args.uit or args.droog:
        for datum, titel, tekst, code, aantal in werk:
            wat = f"{datum} → tabblad '{titel}': {export.getal(aantal)} fragmenten, {export.getal(len(tekst))} tekens, code {code}"
            if args.uit:
                pad = Path(args.uit) / f"{titel.replace('/', '-')}.txt"
                pad.parent.mkdir(parents=True, exist_ok=True)
                pad.write_text(tekst)
                print(f"{wat} → {pad}")
            else:
                print(f"{wat} (document {doc_id})")
        return 0

    doc = Doc(maak_sessie(), doc_id)
    doc.gewenst = [titel for _, titel, _, _, _ in werk]
    for datum, titel, tekst, code, aantal in werk:
        wat = f"{datum} → tabblad '{titel}': {export.getal(aantal)} fragmenten, {export.getal(len(tekst))} tekens, code {code}"
        tab = doc.zorg_voor_tab(titel)
        if not args.altijd and tab["code"] == code:
            print(f"{wat} — ongewijzigd, overgeslagen")
            continue
        doc.schrijf(tab, tekst)
        print(f"{wat} — bijgewerkt")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
