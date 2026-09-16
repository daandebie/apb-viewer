#!/usr/bin/env python3
"""
Laag 5 — de dagteksten in Google Docs zetten, zodat Gemini ze via Drive kan lezen.

Gemini opent geen webadressen, maar leest wel Google Docs uit Drive ("auto-synced after import").
Dit script schrijft dezelfde tekst als de exportknop naar bestaande Docs: per dag één of meer,
want een Doc houdt op rond 1,02 miljoen tekens.

De Docs moeten al bestaan en gedeeld zijn met het serviceaccount (bewerker). Een serviceaccount
heeft geen eigen opslagruimte en kan dus zelf geen Doc aanmaken.

  docs_sync.py --droog                     wat zou er gebeuren (omvang, delen, controlecode)
  docs_sync.py --uit /tmp/docs             de teksten naar bestanden schrijven i.p.v. naar Docs
  docs_sync.py                             de Docs bijwerken

Welke Docs: JSON in de omgevingsvariabele APB_DOCS of in --map (buiten git houden — met de
document-ID's kan iedereen met de link mee lezen):

  {"2026-09-16": ["<doc-id deel 1>", "<doc-id deel 2>"], "2026-09-17": ["<doc-id>"]}

Bovenaan en onderaan elk Doc staat dezelfde controlecode. Die verandert alleen als de tekst
verandert, zodat je in Gemini kunt zien of je de nieuwste versie voor je hebt.
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
# Google Docs stopt rond 1,02 miljoen tekens; marge voor de kop, de staart en groei tijdens het schrijven.
MAX_TEKENS = 900_000


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
        f"Dit deel bevat de fragmenten {eerste} t/m {laatste} ({export.getal(len(fragmenten))} stuks). "
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


def bouw_delen(apb: dict, datum: str, aantal_docs: int, nu: datetime) -> list[tuple[str, str, int]]:
    """(tekst, controlecode, aantal fragmenten) per Doc."""
    dag = next(d for d in apb["dagen"] if d["datum"] == datum)
    fragmenten = [f for f in apb["fragmenten"] if f["dag"] == datum]
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

    afgekapt = []
    if len(groepen) > aantal_docs:
        # Te weinig Docs: de laatste bevat wat er nog in past, de rest staat alleen in de viewer.
        afgekapt = [f for g in groepen[aantal_docs:] for f in g]
        groepen = groepen[:aantal_docs]

    uit = []
    for i, groep in enumerate(groepen, 1):
        romp = "".join(fragment_tekst(f) for f in groep)
        # De code hangt aan de inhoud, niet aan de tijd: zo blijft een Doc ongemoeid als er niets veranderde.
        code = hashlib.sha256(romp.encode()).hexdigest()[:6].upper()
        staart = f"\nEINDE {'DEEL ' + str(i) if len(groepen) > 1 else 'DOCUMENT'} — controlecode {code}\n"
        if afgekapt and i == len(groepen):
            staart = (f"\nLET OP: {export.getal(len(afgekapt))} latere fragmenten ({afgekapt[0]['id']} en verder) passen niet meer "
                      f"in dit document. Die staan wel in de viewer op https://daandebie.github.io/apb-viewer/\n") + staart
        uit.append((kop(dag, i, len(groepen), groep, code, nu, apb) + romp + staart, code, len(groep)))
    return uit


def huidige_code(sessie, doc_id: str) -> str | None:
    r = sessie.get(f"{API}/{doc_id}", params={"fields": "body.content"}, timeout=60)
    r.raise_for_status()
    for element in r.json().get("body", {}).get("content", []):
        for stuk in element.get("paragraph", {}).get("elements", []):
            tekst = stuk.get("textRun", {}).get("content", "")
            if "controlecode" in tekst:
                return tekst.split("controlecode", 1)[1].strip()
    return None


def eind_index(sessie, doc_id: str) -> int:
    r = sessie.get(f"{API}/{doc_id}", params={"fields": "body.content(endIndex)"}, timeout=60)
    r.raise_for_status()
    inhoud = r.json().get("body", {}).get("content", [])
    return max((e.get("endIndex", 1) for e in inhoud), default=1)


def schrijf_doc(sessie, doc_id: str, tekst: str) -> None:
    eind = eind_index(sessie, doc_id)
    verzoeken = []
    # De afsluitende alinea-einde van een Doc mag niet weg; vandaar eind - 1.
    if eind > 2:
        verzoeken.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": eind - 1}}})
    verzoeken.append({"insertText": {"location": {"index": 1}, "text": tekst}})
    r = sessie.post(f"{API}/{doc_id}:batchUpdate", json={"requests": verzoeken}, timeout=300)
    if not r.ok:
        raise SystemExit(f"Doc {doc_id} bijwerken mislukt ({r.status_code}): {r.text[:500]}")


def maak_sessie():
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        sys.exit("google-auth en requests ontbreken: python3 -m pip install google-auth requests")
    sleutel = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sleutel:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON ontbreekt (JSON van het serviceaccount).")
    info = json.loads(Path(sleutel).read_text() if Path(sleutel).exists() else sleutel)
    return AuthorizedSession(service_account.Credentials.from_service_account_info(info, scopes=SCOPES))


def laad_map(pad: str | None) -> dict[str, list[str]]:
    rauw = Path(pad).read_text() if pad else os.environ.get("APB_DOCS", "")
    if not rauw.strip():
        sys.exit("Geen document-ID's: zet APB_DOCS of gebruik --map. Zie de kop van dit bestand.")
    kaart = json.loads(rauw)
    return {dag: ([ids] if isinstance(ids, str) else list(ids)) for dag, ids in kaart.items()}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(HERE / "data" / "apb.json"))
    ap.add_argument("--map", help="JSON-bestand met {datum: [doc-id, …]}; standaard uit APB_DOCS")
    ap.add_argument("--dag", action="append", help="alleen deze dag(en) bijwerken")
    ap.add_argument("--droog", action="store_true", help="alleen tonen wat er zou gebeuren")
    ap.add_argument("--uit", help="de teksten naar deze map schrijven in plaats van naar Docs")
    ap.add_argument("--altijd", action="store_true", help="ook schrijven als de controlecode gelijk is")
    args = ap.parse_args(argv)

    apb = export.laad_json(Path(args.data), None)
    if apb is None:
        sys.exit(f"{args.data} ontbreekt — draai eerst parse.py.")
    kaart = laad_map(args.map)
    nu = datetime.now(TZ)
    sessie = None if args.droog or args.uit else maak_sessie()

    for datum, doc_ids in sorted(kaart.items()):
        if args.dag and datum not in args.dag:
            continue
        delen = bouw_delen(apb, datum, len(doc_ids), nu)
        if not delen:
            print(f"{datum}: nog geen verslag, Docs ongemoeid gelaten")
            continue
        for i, ((tekst, code, aantal), doc_id) in enumerate(zip(delen, doc_ids), 1):
            wat = f"{datum} deel {i}/{len(delen)}: {export.getal(aantal)} fragmenten, {export.getal(len(tekst))} tekens, code {code}"
            if args.uit:
                pad = Path(args.uit) / f"{datum}-deel{i}.txt"
                pad.parent.mkdir(parents=True, exist_ok=True)
                pad.write_text(tekst)
                print(f"{wat} → {pad}")
            elif args.droog:
                print(f"{wat} → Doc {doc_id}")
            else:
                if not args.altijd and huidige_code(sessie, doc_id) == code:
                    print(f"{wat} → ongewijzigd, overgeslagen")
                    continue
                schrijf_doc(sessie, doc_id, tekst)
                print(f"{wat} → bijgewerkt")
        if len(delen) < len(doc_ids):
            print(f"{datum}: {len(doc_ids) - len(delen)} Doc(s) niet nodig, ongemoeid gelaten")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
