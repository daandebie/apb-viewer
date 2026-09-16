# APB-viewer — Algemene Politieke Beschouwingen 2026

Leesbare, doorzoekbare schil om de (ongecorrigeerde) verslagen van de APB op **woensdag 16 en donderdag 17 september 2026** (beide vanaf 10.15 uur). De Dienst Verslag en Redactie publiceert tijdens de vergadering tussenversies in het Gegevensmagazijn; wij halen die op, archiveren ze ongewijzigd en bouwen er een viewer en exports omheen. Geen spraakherkenning, geen audio.

```
fetcher.py   → raw/{datum}_{verslag_id}_{ApiGewijzigdOp}.xml  + raw/manifest.json   (archief, nooit wijzigen)
parse.py     → data/apb.json  + apb-offline.html                                   (afgeleid, altijd opnieuw te maken)
summarize.py → data/samenvattingen.json  (+ opnieuw bundelen)                        (los, optioneel)
viewer.html  → leest data/apb.json (via http) of de ingesloten data (apb-offline.html)
```

Alles is Python-stdlib (systeem-`python3` 3.9 volstaat), de viewer is één HTML-bestand zonder externe resources. Alleen `summarize.py --backend api` heeft de `anthropic`-SDK nodig.

## Online

- **Viewer:** https://daandebie.github.io/apb-viewer/ — openbaar via de link, niet geïndexeerd (`noindex` + `robots.txt`)
- **Demo met testdata** (3–4 juni 2026, stand 4 juni 16:00): https://daandebie.github.io/apb-viewer/demo/
- **Voor Gemini:** uploaden via **Exporteer → Voor Gemini** (zie [Export](#export-voor-een-extern-taalmodel-gemini)). Prompt, eenmalig in een Gem plakken: https://daandebie.github.io/apb-viewer/gemini-prompt.txt (bron: `gemini-prompt-apb.md`). Gemini-chat kan **geen enkele** URL openen: getest op 14-09-2026 op twee accounts, met onze site en met de API van de Tweede Kamer. Links geven werkt dus niet. Gemini Notebooks kan een URL wel als bron importeren, maar dat is een momentopname en gaf in de test foute antwoorden (een aanwezig fragment "niet gevonden", verkeerd laatste fragment).
- **Voor taalmodellen die zelf webadressen openen:** https://daandebie.github.io/apb-viewer/gemini/index.md — leesbare bestanden per dag, termijn en beurt (zelfde opmaak als de exportknop, ook als `.txt`), elke run opnieuw gemaakt door `export.py`.
- **Runs:** https://github.com/daandebie/apb-viewer/actions

GitHub Actions (`.github/workflows/bijwerken.yml`) doet het werk, je laptop hoeft niet aan:

| Wanneer | Wat |
|---|---|
| 16–18 sep, elke 5 min | `fetcher.py` → nieuwe versies in `raw/` committen → `parse.py` → publiceren op GitHub Pages |
| daarna dagelijks 08:23 | idem, voor late tussenversies en de gecorrigeerde Eindpublicatie |
| bij elke push naar `main` | opnieuw bouwen en publiceren (bijv. na nieuwe samenvattingen) |

**Het schema van 5 minuten vuurt niet betrouwbaar af.** Op 16-09-2026 zaten er gaten van uren tussen de runs; GitHub knijpt korte schema's in publieke repo's af. Daarom jaagt een launchd-job op de Mac de runs aan: `apb-trigger.sh` start elke 5 minuten een run via `gh workflow run`, maar alleen op 16–18 sep tussen 08:00 en 23:59, en niet als er al een run loopt. Log: `trigger.log`.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/nl.daandebie.apb-trigger.plist   # aan
launchctl bootout   gui/$(id -u)/nl.daandebie.apb-trigger                                # uit
```

Daarvoor moet de Mac wakker zijn; slaapt hij, dan valt het terug op wat GitHub zelf afvuurt en kleurt de statusknop na 25 minuten oranje. Het ophalen blijft in de cloud, dus `raw/` houdt één schrijver.

Een storing bij de Kamer blokkeert de publicatie niet: de viewer toont dan "Laatste controle mislukt". Geplande GitHub-runs starten bij zo'n kort interval regelmatig 10–15 minuten later dan gepland; pas als er 25 minuten geen controle is geweest kleurt de statusknop oranje ("Bijwerken loopt achter"). Handmatig een run starten: `gh workflow run bijwerken.yml -R daandebie/apb-viewer`.

**Let op, rond half november:** GitHub zet geplande workflows in een publieke repo uit na 60 dagen zonder activiteit. Komt er na 18 september niets nieuws binnen, dan stopt de dagelijkse run rond 17 november, en dat is net de periode waarin de gecorrigeerde Eindpublicatie verwacht wordt. Controleer dan *Actions*; zet de workflow zo nodig weer aan en start een run met het commando hierboven.

**Hoe actueel is het?** De statusknop rechtsboven toont wanneer er voor het laatst bij de Kamer is gecontroleerd, wanneer de Kamer de nieuwste versie publiceerde en tot hoe laat de tekst loopt. De dagkop herhaalt dat per dag, en exports vermelden het ook.

## Draaiboek woensdag 16 september

Niets te starten. Wel doen:

1. Rond 10:00 de viewer openen: de statusknop moet groen zijn ("Gecontroleerd …").
2. **Verwacht vertraging.** Bij de test op 3 juni (aanvang 10.15) verscheen de eerste tussenpublicatie om 12.02, met tekst tot 10.28. Daarna kwam er elke 30–90 minuten een nieuwe versie, en de laatste van die dag pas de volgende ochtend. Tot de Kamer de `Vergadering` aanmaakt, staat er "nog geen verslag".
3. **Gemini:** vooraf één keer oefenen op `/demo/`: Gem aanmaken met `gemini-prompt.txt`, *Exporteer → Voor Gemini → Alles tot nu* uploaden. De aanvulling geeft in de demo altijd "niets nieuw": de testdata verandert niet.
4. Blijft de knop oranje of rood: kijk bij *Actions*. Noodroute op de laptop: `git pull && caffeinate -i python3 fetcher.py --watch` plus `python3 -m http.server 8765 --bind 127.0.0.1`, en achteraf `git add raw && git commit && git pull --rebase && git push`.

## Lokaal werken

```bash
git pull && python3 parse.py                      # archief van GitHub, data opnieuw afleiden
python3 -m http.server 8765 --bind 127.0.0.1      # http://127.0.0.1:8765/viewer.html
python3 fetcher.py --status                       # wat staat er in het archief
```

Draai de fetcher niet tegelijk lokáál én in GitHub Actions: beide vullen `raw/manifest.json` aan en dat geeft mergeconflicten. De `raw/` in GitHub is het archief.

Bij de juni-test kwam de gecorrigeerde Eindpublicatie ~8 weken later (31 juli). De dagelijkse run haalt die vanzelf op; gewijzigde fragmenten krijgen dan het label **gewijzigd** met een woorddiff, en hun ID blijft gelijk.

## Viewer

- **Tabs** Dag 1 / Dag 2 / Alle teksten; agendapunt → termijn → beurt, elk inklapbaar en exporteerbaar (⤓).
- **Sprekersvolgorde** rechts (naam, fractie, begin–eind, duur); klik = springen, rechtsklik = exporteren. Voorzitter optioneel.
- **Zoeken** (`/`): accent- en hoofdletterongevoelig, meerdere woorden = EN, `"exacte zin"` tussen aanhalingstekens; `Enter`/`Shift+Enter` springt door treffers. "alleen treffers" uit = alles tonen en treffers markeren.
- **Filters**: soort (hoofdtermijn/interruptie/voorzitter/procedure, met snelknop *alleen hoofdtermijnen*), tijdvak, fracties en sprekers (fractie óf spreker). Actieve filters staan als chips bovenaan, elk los of allemaal tegelijk wisbaar.
- **Permalink**: `viewer.html#2026-09-16-0143` (klik op de begintijd of het ID).
- **Weergave** (Aa): tekstgrootte, licht/donker, samenvattingen aan/uit.

## Disclaimers — waar ze zitten

| Pad naar buiten | Wat er gebeurt |
|---|---|
| Scherm | Vaste rode balk bovenaan, niet wegklikbaar; statusbadge per fragment (Ongecorrigeerd opvallend oranje) plus publicatiesoort |
| Kopiëren (Cmd+C, contextmenu) | Klembord krijgt alleen `text/plain`: disclaimer bovenaan, per geraakt fragment `[ID] spreker · tijden · STATUS`, gemarkeerd als "(deel van fragment)" bij een deelselectie |
| Knop "Kopieer" bij fragment | Idem |
| Slepen van geselecteerde tekst | Sleepdata vervangen door dezelfde tekst mét disclaimer |
| Knippen | Uitgeschakeld buiten invoervelden |
| Export .md / .txt | Kop met juridische status, instructie voor het taalmodel en bronverslagen; status bij elk fragment |
| Afdrukken | Waarschuwing in de paginakop van elke pagina (Chrome/Edge), plus bij elk fragment de status en "niet letterlijk citeren"; altijd zwart op wit, ook in donkere modus |
| Samenvattingen | Eigen blauw kader "samenvatting — geen bron", altijd boven de ruwe tekst; bij kopiëren als SAMENVATTING — GEEN BRON gemarkeerd |
| `data/apb.json` / `samenvattingen.json` | `meta.disclaimer` resp. `meta.waarschuwing` en een label per samenvatting |

Wat niet af te dichten is vanuit een webpagina: schermafbeeldingen, "Bron weergeven", macOS-diensten/"Zoek op" in het contextmenu, en het lezen van de JSON-bestanden zelf.

## Export voor een extern taalmodel (Gemini)

**Voor Gemini** (bovenaan het menu Exporteer), omdat Gemini de site niet zelf kan lezen:

1. **Alles tot nu** (of *Alleen Dag N* zodra er twee dagen zijn) → uploaden in een nieuwe chat. Het menu toont een tokenschatting; een hele dag is ~170k tokens bij de juni-test.
2. **Alleen wat erbij kwam sinds HH:MM** → uploaden in dezelfde chat. De browser bewaart per pad (`localStorage`, sleutel `apb-gemini-basis:<pad>`) wat er voor Gemini is gedownload: per fragment-ID een handtekening van tekst-hash, status, spreker en begintijd. De aanvulling bevat nieuwe fragmenten plus fragmenten waarvan die handtekening veranderde (gemarkeerd `VERVANGT DE VERSIE UIT EEN EERDERE UPLOAD`), en een lijst vervallen ID's. Staat er een nieuwere `apb.json` klaar, dan wordt die eerst toegepast.
3. **Instructies voor Gemini** → opent `gemini-prompt.txt` om in een Gem te plakken.

De aanvulling rekent vanaf de laatste Gemini-download in díe browser, dus: één chat tegelijk, en een nieuwe chat altijd beginnen met *Alles tot nu*. In een privévenster werkt de aanvulling niet (geen opslag).

Verder: hele dag, huidige selectie, of via een blok (⤓) / fragment (⋯) / sprekersfilter: één beurt, termijn, agendapunt of alle bijdragen van één spreker. Formaat Markdown of platte tekst, één bestand. Per fragment:

```
## [2026-09-16-0143] Naam spreker (fractie)
10:47:12 – 10:49:55 · 2m43s · interruptie · ONGECORRIGEERD · Tussenpublicatie

<tekst, alinea's intact>
```

De kop instrueert het model om niet letterlijk te citeren en altijd naar het fragment-ID te verwijzen; met dat ID vind je het origineel terug in de viewer (online: volledige link naar de viewer).

## Samenvattingen

```bash
python3 summarize.py --lijst --dag 2026-09-16             # blokken met hun eerste fragment-ID
python3 summarize.py --beurt 2026-09-16-0012 --droog      # wat zou er gebeuren (woorden/tokens)
python3 summarize.py --beurt 2026-09-16-0012 --backend claude-code   # via lokale `claude -p`
python3 summarize.py --alle-beurten --dag 2026-09-16 --backend claude-code
python3 summarize.py --kies --dag 2026-09-16               # interactief nummers kiezen
git add data/samenvattingen.json && git commit -m "Samenvattingen" && git pull --rebase && git push   # online zetten
```

- `--backend api` (standaard) gebruikt de Anthropic-SDK met `claude-opus-5`, adaptive thinking en server-side refusal-fallback (`fallbacks: "default"`); vereist `python3.12 -m pip install anthropic` en `ANTHROPIC_API_KEY` of `ant auth login`. `--backend claude-code` loopt via het Claude Code-abonnement, zonder sleutel.
- Bestaande, actuele samenvattingen worden overgeslagen; **verouderd** (brontekst sindsdien gewijzigd) wordt opnieuw gemaakt, en de viewer markeert verouderde samenvattingen.
- Ontbrekende samenvattingen zijn geen fout.

## Ontwerpkeuzes

- **Fragment** = één aaneengesloten bijdrage van één spreker: VLOS `<woordvoerder>` (de beurthouder) of `<interrumpant>`, plus proceduretekst zonder spreker (schorsing, agendatekst, besluit). Een motie die iemand indient zit in diens fragment (alinea's met soort `Motietekst`/`Motieinfo`).
- **Type**: `hoofdtermijn` (beurthouder), `interruptie`, `voorzitter` (`isvoorzitter=true`), `procedure`. Het antwoord van de beurthouder op een interruptie is `hoofdtermijn`: het hoort bij zijn termijn.
- **Eindtijd** van een woordvoerder = begin van de eerste interruptie die volgt (VLOS' `markeertijdeind` loopt door tot na die interrupties; die staat als `markeertijdeind` ook in de JSON).
- **Fracties**: canonieke vorm = `Afkorting` uit het Gegevensmagazijn (`fracties.json`, met aliassen en kleur); origineel blijft in `fractie_origineel`. `PRO` en `GroenLinks-PvdA` zijn formeel verschillende fracties en worden níet samengevoegd. Voorzitter en kabinet zijn eigen groepen. Een onbekende fractienaam komt ongewijzigd door en `parse.py` waarschuwt.
- **Tekst**: alleen XML-witruimte wordt samengevouwen; verder staat er wat de Kamer schrijft, inclusief de sprekersaanduiding ("De heer **Klaver** (GroenLinks-PvdA):") die de viewer vet toont.
- **Stabiele ID's** (`{datum}-{nnnn}`): nummer = volgorde van eerste verschijnen, over alle gearchiveerde versies heen in publicatievolgorde. Nieuwe of bij correctie toegevoegde fragmenten krijgen het volgende vrije nummer, dus bestaande ID's schuiven nooit. Maakt een correctie een nieuw VLOS-objectid voor hetzelfde fragment (gezien op 4 juni), dan wordt het herkend op spreker, rol, begintijd (±3 min) en tekstgelijkenis (≥0,6) en houdt het zijn ID (`vlos_objectids_eerder`). Gevolg: ID's zijn stabiel zolang `raw/` intact blijft; wie het archief kwijtraakt en alleen de eindversie ophaalt, krijgt andere nummers.
- **Versies**: tussenpublicaties zijn cumulatief. De nieuwste versie bepaalt inhoud, volgorde en welke fragmenten er zijn; een fragment dat eruit verdwijnt en niet herkend wordt, komt in `vervallen`. Per fragment staan de versies waarin inhoud, status, spreker of begintijd veranderde.
- **Naspelen**: `parse.py --peilmoment 2026-09-16T12:00:00Z` negeert versies die daarna gepubliceerd zijn — handig om na te gaan wat er op een moment zichtbaar was.

## JSON-schema `data/apb.json` (schema_versie 1)

```jsonc
{
  "meta": {
    "schema_versie": 1, "titel": "…", "gegenereerd_op": "ISO", "laatst_opgehaald_op": "ISO|null",
    "inhoud_hash": "16 hex — verandert alleen als fragmenten veranderen (viewer pollt hierop)",
    "bron": "…", "disclaimer": "…", "handelingen_url": "https://www.officielebekendmakingen.nl/",
    "dagen": ["2026-09-16", "2026-09-17"], "peilmoment": "ISO|null", "onbekende_fracties": []
  },
  "fracties": [ { "groep": "PVV", "naam": "Partij voor de Vrijheid", "kleur": "#334e7a", "categorie": "fractie|voorzitter|kabinet|procedure|overig", "fragmenten": 67 } ],
  "dagen": [ {
    "datum": "2026-09-16", "label": "Dag 1", "lang": "woensdag 16 september 2026", "kort": "wo 16 sep",
    "vergaderingen": [ { "id", "titel", "nummer", "aanvangstijd", "sluiting" } ],
    "bronverslagen": [ { "bestand", "verslag_id", "vergadering_id", "soort", "status", "gewijzigd_op", "api_gewijzigd_op", "opgehaald_op", "bytes", "sha256", "fragmenten", "herkend_na_nieuw_objectid" } ],
    "actuele_bronnen": [ { "verslag_id", "soort", "status", "api_gewijzigd_op", "opgehaald_op" } ],
    "aantal_fragmenten": 0, "statussen": { "Ongecorrigeerd / Tussenpublicatie": 0 }, "laatste_markeertijd": "ISO|null",
    "agendapunten": [ { "id": "VLOS-objectid", "soort", "titel", "onderwerp", "begin", "eind", "duur_s", "voortzetting", "dag", "fragmenten": ["ID", …] } ],
    "termijnen":    [ { "id", "soort", "titel", "begin", "eind", "duur_s", "agendapunt", "dag", "fragmenten": [] } ],
    "beurten":      [ { "id", "soort": "Spreekbeurt|Stemming item", "titel", "begin", "eind", "duur_s", "spreker": {…}, "naam", "categorie", "fractie", "groep", "is_voorzitter", "agendapunt", "termijn", "dag", "fragmenten": [] } ]
  } ],
  "fragmenten": [ {
    "id": "2026-09-16-0143", "dag": "2026-09-16", "volgorde": 143,
    "type": "hoofdtermijn|interruptie|voorzitter|procedure", "rol": "woordvoerder|interrumpant|procedure", "procedure_soort": "Schorsing|Agendapunt|…",
    "is_voorzitter": false, "is_draad": false,
    "naam": "Klaver", "categorie": "fractie", "fractie": "GroenLinks-PvdA", "fractie_origineel": "GroenLinks-PvdA", "groep": "GroenLinks-PvdA", "functie": "lid Tweede Kamer",
    "spreker": { "objectid", "soort", "aanhef", "verslagnaam", "voornaam", "achternaam", "weergavenaam", "functie", "fractie" },
    "begin": "2026-09-16T10:47:12", "eind": "2026-09-16T10:49:55", "duur_s": 163, "markeertijdeind": "…",
    "label": "De heer Klaver (GroenLinks-PvdA):",
    "alineas": [ { "tekst": "…" }, { "tekst": "…", "soort": "Motietekst|Motieinfo|lijstitem|…" } ],
    "agendapunt_id": "…", "termijn_id": "…|null", "beurt_id": "…|null",
    "bron": { "verslag_id", "soort": "Tussenpublicatie", "status": "Ongecorrigeerd", "api_gewijzigd_op" },
    "versies": [ { "verslag_id", "soort", "status", "api_gewijzigd_op", "tekst_hash", "spreker_id", "begin" } ],
    "tekst_hash": "12 hex", "wijzigingen": ["tekst", "status", "spreker", "tijd"],
    "eerdere_alineas": [ … ],            // alleen bij gewijzigde tekst: de versie vóór de laatste wijziging
    "vlos_objectid": "…", "vlos_objectids_eerder": [ … ]
  } ],
  "vervallen": [ /* fragmenten die in een eerdere versie stonden maar niet meer in de nieuwste */ ]
}
```

`data/samenvattingen.json`: `{ "meta": {…}, "samenvattingen": [ { "id": "beurt:<blok_id>", "scope": "beurt|termijn|agendapunt", "blok_id", "dag", "titel", "fragment_ids": [], "bron_hashes": {"ID": "tekst_hash"}, "bron_statussen": {}, "tekst", "model", "backend", "prompt_versie", "gegenereerd_op", "label" } ] }`

## Bestanden

| Bestand | |
|---|---|
| `config.json` | dagen, pollinterval, API-basis, User-Agent |
| `fracties.json` | canonieke fractienamen, aliassen, kleuren |
| `raw/` | archief: XML per fetch + `manifest.json` (alleen aanvullen) |
| `data/apb.json`, `data/samenvattingen.json` | afgeleid |
| `apb-offline.html` | viewer met ingesloten data (afgeleid; online te downloaden via *Bronnen*) |
| `data/ophaalstatus.json` | stand van de laatste controle (afgeleid, niet in git) |
| `.github/workflows/bijwerken.yml` | ophalen, verwerken en publiceren |
| `export.py` | leesbare bestanden voor taalmodellen op vaste adressen (`/gemini/`) |
| `apb-trigger.sh` | jaagt vanaf de Mac elke 5 min een run aan (launchd), omdat GitHub het schema afknijpt |
| `gemini-prompt-apb.md` | prompt voor Gemini (in een Gem): uploadroute, hoe uploads en aanvullingen te combineren, hoe te antwoorden; online als `gemini-prompt.txt`/`.md` |
| `fetcher.log` | ophaallog (niet in git) |

Wat in git staat: code, `config.json`, `fracties.json`, `raw/` (archief) en `data/samenvattingen.json` (niet af te leiden). Alles wat `parse.py` maakt, wordt in de workflow opnieuw gebouwd.

`raw/` bevat ook de testvergaderingen van 3 en 4 juni 2026 (Verantwoordingsdebat met de premier; ultimatum vakbeweging). `parse.py` neemt standaard alleen de dagen uit `config.json` mee, dus die zitten de APB-data niet in de weg.
