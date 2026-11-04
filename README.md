# Football EPG

Catalogo calcistico consultabile su GitHub, indipendente dall'app e dai risultati
live. Segue solo le famiglie **Sky Calcio, Sky Sport, DAZN/Zona DAZN, TV8 e RAI**
nella guida italiana di https://iptv-epg.org/guides. La presenza di una famiglia
non garantisce che tutti i suoi canali abbiano dati nella sorgente.

## Caricamento e avvio

1. Crea un repository GitHub `football-epg`, con branch principale `main`.
2. Carica il contenuto di questa cartella, comprese `aliases`, `tests` e
   `.github/workflows/update-epg.yml`, mantenendo i percorsi. Non caricare una
   cartella contenitore aggiuntiva. Lo ZIP di consegna contiene già la struttura.
3. In **Settings → Actions → General → Workflow permissions**, consenti
   **Read and write permissions** se il repository non permette già ai workflow
   di aggiornare i dati. Le regole del repository devono consentire al bot di
   scrivere su `main`.
4. In **Actions → Update Football EPG → Run workflow**, avvia la prima acquisizione.
5. Apri [PROGRAMMAZIONE.md](PROGRAMMAZIONE.md), [data/channels.json](data/channels.json)
   e [data/status.json](data/status.json).

Il workflow è pianificato **ogni ora, al minuto 17**, in UTC. GitHub può avviarlo
con ritardo: questa è una frequenza programmata, non una garanzia al minuto.
Parte anche quando cambiano codice o configurazione su `main`; le modifiche ai
soli dati non generano cicli. Nei repository pubblici GitHub può sospendere gli
schedule dopo 60 giorni senza attività nel repository: controllare Actions se
il catalogo non si aggiorna. Gli aggiornamenti dati producono normalmente commit.

Non sono necessarie chiavi per scaricare l'EPG pubblico. Nessun URL di streaming,
playlist personale o credenziale viene aggiunto al catalogo.

## Configurazione

- `sources`: file XMLTV dei paesi; attualmente solo Italia. Sono supportati XML e gzip.
- `channel_families`: prefissi dei nomi/identificativi da seguire. RAI comprende
  tutti i canali RAI presenti, ma vengono tenuti solo i programmi calcistici.
- `channel_ids_include` e `channel_ids_exclude`: eccezioni per ID EPG esatti;
  l'esclusione prevale sull'inclusione.
- `football_categories` e `football_keywords`: categorie e parole da conservare.
  La parola isolata `Champions` non è usata: includerebbe anche altri sport.
- `competitions`: sezioni da riconoscere e calendari da associare. Il catalogo
  conserva anche altro calcio dei canali seguiti; questa lista limita le
  associazioni strutturate alle competizioni configurate.
- `days_ahead`: oggi e i prossimi 7 giorni, con la copertura disponibile nella guida.
- `aliases/teams.json`: equivalenze tra squadre del calendario e nomi EPG.
- `aliases/channels.json`: nomi alternativi per una futura associazione ai canali
  della playlist. Gli ID EPG distinti restano distinti; nessuna fusione automatica
  di DAZN 1 e DAZN 1 Linear o delle varianti SD/HD/4K.

Gli orari XMLTV vengono convertiti usando il loro offset; i JSON usano UTC e la
tabella leggibile usa Europe/Rome, compresi i cambi di ora legale.

## File prodotti

| File | Contenuto |
| --- | --- |
| `PROGRAMMAZIONE.md` | Tabella consultabile direttamente su GitHub |
| `data/channels.json` | Canali seguiti, ID EPG, nomi, famiglia e alias |
| `data/programmes.json` | Programmi calcistici, orari, categorie, titolo e descrizione |
| `data/matches.json` | Partite del calendario e trasmissioni associate |
| `data/status.json` | Ultimo controllo, sorgenti, copertura, errori e stato calendari |

`broadcastType` è `live`, `replay` o `unknown`. Un programma senza indicazioni
esplicite non diventa automaticamente una diretta. Rubriche e repliche restano
consultabili nel catalogo, ma quelle riconosciute non sono associate come partite
da vedere in diretta. `confidence` misura l'associazione delle squadre e non
certifica la disponibilità dello stream né la correttezza della guida.

## Collegare il calendario delle partite (facoltativo)

La configurazione iniziale **funziona senza token** e produce il catalogo EPG.
Le associazioni sono inizialmente disabilitate: `matches.json` indica
`calendarState: disabled`. Per abilitarle:

1. Crea un token GitHub con **Contents: Read-only** sul solo repository privato
   `debonair-bender/football-api` e salvalo in **Settings → Secrets and variables →
   Actions → New repository secret**, nome `FOOTBALL_CALENDAR_TOKEN`.
2. Imposta `calendars.enabled` a `true` in `config.json`.
3. Verifica repository e percorsi; il lettore usa il formato attuale
   `data/<competizione>/<stagione>.json`, per esempio
   `data/champions-league/2026-2027.json`.
4. Avvia nuovamente il workflow e controlla `calendarState: ok`.

Non scrivere il token in `config.json`. Il token è inviato soltanto all'API
GitHub e non alla sorgente EPG. Vengono pubblicati solo i riferimenti alle partite
nel periodo scelto, non l'intero calendario stagionale.

Le associazioni richiedono entrambe le squadre, un orario compatibile e nessun
conflitto di competizione. Le menzioni ambigue di più partite non sono associate.
Un titolo generico può restare senza associazione. È quindi normale che alcune
partite abbiano `broadcasts: []` anche se esiste una trasmissione.

## Errori e validazione

Gli errori di download/XML non cancellano l'ultimo catalogo valido. Il workflow
pubblica lo stato d'errore e risulta fallito, così il problema resta visibile.
Un errore nel calendario consente di aggiornare l'EPG ma conserva il precedente
`matches.json` con il suo timestamp: controllare sempre `status.json` prima di
usare le associazioni. Il successivo avvio orario ritenta automaticamente.

Avvio locale con Python 3.12:

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python sync_epg.py
```

Per verificare calendari locali senza credenziali:

```powershell
python sync_epg.py --calendar-dir C:/percorso/calendari --output C:/percorso/prova
```

La cartella deve contenere `serie-a.json`, `serie-b.json` e
`champions-league.json`, nello stesso formato di `football-api`.

L'app non è stata modificata o compilata per questo progetto.
