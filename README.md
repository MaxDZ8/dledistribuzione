# Script per dati da e-distribuzione

Nel 2023 il mio rapporto con il fotovoltaico di casa è diventato più complicato. &Egrave; diventato necessario farmi un'idea migliore dei numeri in gioco. Il sito di e-distribuzione per quanto funzionale ha mostrato diversi inconvenienti, alcuni che ricordo:

1. **Lentissimo**: d'accordo, lo si può usare per dare un'occhiata ai consumi del mese scorso e con un po' di pazienza ricavarne qualcosa in più, diventa presto tedioso;
2. **Inaffidabile**: non so da che cosa dipenda ma in più occasioni le richieste sembravano andare in timeout, spesso senza nemmeno alcun avvertimento e a volte costringendomi al logout;
3. **Incoerente**: si selezionano mesi di cui il grafico "giustamente" mostra le curve su base *giornaliera* ma il file da scaricare riporta le misure al quarto d'ora;
4. **Approssimativo**: il grafico sui giorni del mese è troppo semplificato per avere intuizioni significative.

Qualcuno che come me voglia esaminare i dati ad esempio dell'ultimo anno si trova a dover fare un sacco di click, un lavoro manuale lungo e ad alto rischio d'errori. Non bastasse, questi dati vanno poi messi assieme e gestiti magari in un foglio di calcolo cosa che ha a sua volta alcune complicazioni per non parlare di eventuali elaborazioni più fini.

Il che ci porta a queste utility. Spero che aiuteranno a migliorare la comprensione che ciascuno può e dovrebbe avere dei propri consumi e delle bollette. L'argomento energia elettrica è complesso e dati aperti e manipolabili sono la base su cui si traggono decisioni informate.

# `dl_quarters.py`

> _Intento:_
>
> Scarica i dati da e-distribuzione come farebbe un browser.

Partendo dalla pagina principale di e-distribuzione, cliccando sulla sezione "Le mie misure"

![Pagina principale di e-distribuzione](docimages/edist_main-page.png)

Si arriva alla pagina "Curve di carico" e qui iniziano i guai perchè impiega un po' a popolarsi.

![Pagina "Curve di Carico](docimages/edist_curve-di-carico.png)

Il programma è un modo automatizzato per impostare il periodo di riferimento (nella sezione dedicata) ed eventualmente la curva desiderata (subito sopra il grafico).

Una cosa che può sembrare strana: vorrei poter scrivere che:

> il programma fa una serie di click e produce il risultato corrispondente all'usare il bottone "Scarica il dettaglio del quarto d'ora"

Ma a sorpresa, **e-distribuzione non invia quel file**. `dl_quarters.py` produce in uscita esattamente ciò che i server e-distribuzione inviano al browser. Per farlo, si presenta ai server e-distribuzione come il vostro browser ha appena fatto. Questo significa che bisogna fornire **informazioni riservate** che vanno estratte dallo stato del browser. Il programma riutilizza le informazioni di login ed OTP inserite nel browser per generare le sue richieste.

1. Aprire i _developer tools_ del browser; spesso è sufficiente premere F12 sulla tastiera;
2. Cercare la tab relativa al traffico di rete, generalmente "Network"
3. Cambiare il periodo di riferimento, ad esempio portando la "Fine" al mese precedente;
4. Nella tab del traffico, selezionare la nuova richiesta.

Da qui si possono estrarre i dati d'autenticazione. Servono i valori di

1. Dai dati della richiesta
    - cookie
    - x-sfdc-page-scope-id
2. Dalla richiesta vera e propria
    - aura.token
    - aura.context.value

![Dove trovare cookie ed x-sfdc-page-scope-id](docimages/edist_getting-credentials.png)

Si raccomanda usare "copia valore", facendo click col pulsante sinistro del mouse. Incollare quando richiesto. Punti bonus per i malfunzionamenti come in questo caso (che stranamente sembrano essere più rari usando lo script).
Per quanto riguarda la seconda coppia di valori, spostarsi sulla sotto-tab "richiesta".

![Dove trovare aura.token ed aura.context.value](docimages/edist_credentials-from-request.png)


> ⚠️ **ATTENZIONE: DATI RISERVATI**
>
> I campi indicati sono **credenziali d'accesso** associate alla vostra attività. Trattateli come una password.
> Manteneteli al sicuro. Non forniteli a terze parti.
>
> Il programma ne mantiene una copia in memoria per il solo tempo necessario all'esecuzione.

## Esecuzione

```
python3 dl_quarters.py
```
Il programma chiederà nell'ordine
 
1. cookie
2. x-sfdc-page-scope-id
3. aura.token
4. aura.context
5. la data d'inizio (primo giorno o mese incluso)
6. la data finale (ultimo giorno o mese incluso)

Per i quattro valori presi dal browser raccomando "copia valore" (pulsante destro).

### Parametri aggiuntivi

- `--start`
    - indica da linea di comando la data iniziale
    - il programma non la chiederà
- `--end`
    - omologo a `--start` per la data finale
- `--no-pod-prefix`
    - omette il codice POD dal nome dei file creati
- `--energy-types`
    - seleziona le curve da scaricare
    - default "attiva-prelevata,attiva-immessa"
    - alcune ammettono nomi brevi (R1, R2, R3, R4)
    - vedere il codice per le stringhe precise

<div style='background-color:22FF2250'>
Nota: la date sono da indicarsi nella forma di ANNO-MESE-GIORNO ma è possibile omettere il giorno. Ad esempio, indicando come data iniziale 2026-03 e finale 2026-06 il programma scarica i quarti d'ora da 2026-03-01 00:00 a 2026-06-30 23:45 estremi compresi.
</div>

## Output

Per ciascuna curva richiesta crea un file <span style="background-color:AAAAFF88>">[POD_]</span>YYYY-MM_&lt;curva&gt;.json.

Usando l'opzione `--no-pod-prefix` si può omettere il codice POD da ciascun nome.

Il programma scrive a console (su <em>stdout</em>) una riga per ciascun file creato; tutto il resto sono informazioni scritte su <em>stderr</em>.

# `quarters_to_csv.py`
Nello scaricare i dati abbiamo avuto una sorpresa sgradita: i dati ottenuti non sono quelli che avremmo potuto scaricare dal sito!

!["Scarica il dettaglio del quarto d'ora"](docimages/edist_dl_quarters.webp)

Attorno a questi file di dettaglio dei quarti d'ora si è aggregata un'aura quasi mistica. Generalmente considerati la sorgente della verità sono in realtà prodotti all'interno della pagina web di e-distribuzione, in locale sulla vostra macchina a seguire lo scaricamento.

Lo script `quarters_to_csv.py` converte i dati scaricati in un formato _simile_ (di default) o identico (usando `--original-style`).

> 🛈 **Nota: ora legale e solare**
>
> Il numero di righe nei file ottenuti è variabile. Ad esempio
> - nella notte tra sabato 25 ottobre 2025 e domenica 26 alle ore 03:00 le lancette sono state spostate indietro di un'ora
>     1. quindi domenica 26 conta un'ora in più
>     2. nel file e-distribuzione compaiono due righe 26/10/2025
> - nella notte tra sabato 28 marzo 2026 ed domenica 29 alle ore 02:00 le lancette sono state spostate in avanti di un'ora
>     1. poco male: il file presenta delle celle a 0 a riempire i quarti d'ora inesistenti
> - ricordatevene nelle elaborazioni a seguire.

## Esecuzione

Nella sua forma più semplice, si potrebbe scrivere a mano il nome dei file json ottenuti in precedenza:
```
python3 quarters_to_csv.py 2026-03_attiva_imm.json 2026-03_attiva_prel.json
```
&Egrave; tedioso e soggetto a facili errori. Se avete tutti i file in una cartella potete sfruttare la linea di comando per selezionare i files con meno preoccupazioni:

```
python3 quarters_to_csv.py *json
```
O meglio ancora visto che `dl_quarters` scrive a console i nomi dei files che crea:
```
python3 dl_quarters.py | python3 quarters_to_csv.py
```

## Parametri aggiuntivi

L'unico parametro è `--original-style`. Normalmente lo script emette un file csv identico nei contenuti a quello che avremmo scaricato da e-distribuzione ma leggermente più semplice da gestire.
Indicando questo parametro, il file .csv ottenuto dovrebbe essere identico byte-per-byte a quelli non-proprio-scaricati ufficialmente.

| Caratteristica | Output predefinito | `--original-style` |
|---|---|---|
| **Formato della data** | `YYYY-MM-DD`<br>(es. `2025-10-26`) | `DD/MM/YYYY`<br>(es. `26/10/2025`) |
| **Virgolette sulle righe** | Omesse se possibile | Ogni cella dati racchiusa tra virgolette, come nell’originale |
| **Punto e virgola finale** | Assente | Presente, come nell'originale |

In entrambi i casi
- la riga d'intestazione è priva di virgolette, come nell'originale;
- il separatore di campo è `;`
- il separatore decimale è `,` (italiano)

# `draw_quarters.py`

Trasforma i files ottenuti da `quarters_to_csv.py` in grafici mensili in formato SVG ottimizzati per manipolazioni più semplici.

In sostanza, per ciascuna riga del CSV mensile dei quarti d'ora in ingresso è creato un grafico giornaliero. Ciascuno di questi grafici giornalieri colleziona misurazioni (generalmente l'energia immessa e prelevata) da più file e aggiunge tra l'altro ulteriori informazioni e stime quali produzione attesa, arco solare e condizioni metereologiche.

Un esempio a bassa risoluzione di cosa potreste ottenere:

!["Tre giorni speciali di ottobre 2025"](docimages/2025-10_25-27.svg)

- le barre sono le misure, i dati certi;
    - su queste non si discute, sono ciò che abbiamo scaricato da e-distribuzione
- tutto il resto sono sostanzialmente stime, più o meno affidabili.

I grafici mostrati come esempio sono quelli più affinati; nella sezione relativa agli esempi d'esecuzione ne mostrerò le varie versioni. 

Si osservi che i dati di e-distribuzione per gestire il passaggio all'ora invernale usano due righe per lo stesso giorno. Ad esempio la notte tra il 25 ed il 26 ottobre 2025 alle ore 03:00 le lancette degli orologi sono state spostate indietro di un'ora. Ci sono quindi istanze delle ore 02:00. È un guaio che riguarda la scelta infelice di usare l'orologio locale anzichè momenti nel tempo. Non esiste alcuna giornata da 100 quarti d'ora. Idea sinceramente molto buona e che il programma sposa in pieno, riportando i dati coerentemente ai CSV.

## Esecuzione

Per la forma più semplice consiglio partire da:

```
python3 draw_quarters.py --output-dir directory
```
Il programma cercherà di elaborare qualunque file .csv trovi nella cartella d'esecuzione (semplifico: i files "vicini" al .py). Questo produce dei grafici derivanti solo ed unicamente dai file elaborati e che tutto sommato potreste costruire senza troppi problemi anche con un foglio di calcolo. Di seguito parte del grafico di una certa giornata.

!["Parte di una giornata"](docimages/energy_example.png)

Qui vediamo ad esempio esserci un andamento altalenante dell'energia immessa. Per un sistema FV puro non ci sono molte domande da farsi: questi programmi sono stati pensati per investigare anche l'interazione con batterie.

Aggiungendo la coordinata geografica (basta prenderla da google maps) si possono aggiungere alcune informazioni:


```
python3 draw_quarters.py --output-dir with_sun --location '45.xxxxxxxxxxx, 12.xxxxxxxxxx'
```

Cambia molto l'aspetto dei grafici che sono ora migliorati con

1. orari di alba e tramonto astronomici
2. arco solare
3. interpolazione (stima) della copertura nuvolosa ed eventuale pioggia su base oraria.
    1. Così come fornita da [Open Meteo](https://open-meteo.com/)

La copertura nuvolosa è solo indicativa, un dato grossolano su base oraria e a rinforzare queste idee sia le nuvole (grigie) che la pioggia (blu) scende dall'alto verso il basso con gradini evidenti. A volte i dati sembrano corrispondere abbastanza bene:

!["Produzione impattata dalle nuvole"](docimages/weather_good_match.png)

ma esiste un numero limitato di stazioni meteorologiche, la copertura nuvolosa ha un impatto non-lineare quindi ricordate sempre che per quanto dati storici d'archivio **sono solo indicazioni**!

Ad esempio, la giornata vista per prima è completamente nuvolosa ma evidentemente il dato è inaffidabile visto che c'è stata comunque uno buona produzione:

!["Produzione in una giornata nuvolosa"](docimages/weather_mismatch.png)

**Usate il vostro buon senso.**

> ❤️‍🩹 Non posso mostrarvi un esempio di pioggia.
>
> Nei mesi che ho considerato nella scrittura di questo documento (2025-10 ... 2026-04 inclusi) non ha sostanzialmente piovuto se non per pochi pixel qualche giorno. Questo corrisponde ai miei ricordi e alla difficile situazione idraulica che incontriamo sempre più spesso negli anni. 
> Oggi sappiamo che primavera ed estate furono segnate dalla siccità.

Infine, è possibile aggiungere stime di produzione di uno o più impianti fotovoltaici. Il parametro `--pv` è da indicarsi in una di queste forme:

1. _&lt;azimuth&gt;;&lt;tilt&gt;;&lt;kwp&gt;_
    1. le informazioni di base di un impianto FV, credo non abbiano bisogno di presentazioni
2. _&lt;azimuth&gt;;&lt;tilt&gt;;&lt;kwp&gt;;&lt;coDate&gt;_
    1. aggiungere una data di costruzione abilita il calcolo del _derating_
    2. è da indicarsi in formato ISO YYYY-MM-DD
    3. è accettabile omettere il giorno e scrivere YYYY-MM, il programma lo considera equivalente a YYYY-MM-01
3. _&lt;azimuth&gt;;&lt;tilt&gt;;&lt;kwp&gt;;&lt;coDate&gt;;&lt;label&gt;_
    1. Nome dell'impianto

Quindi ad esempio per simulare due impianti fotovoltaici:

```
python3 draw_quarters.py --output-dir final_test --location '45.xxxxxxxxxxx, 12.xxxxxxxxxx' --pv '210;38;5.76;2011-03;Old' --pv '60;38;5.72;2023-09;New'
```

Crea dei grafici con nuovi archi ad indicare la produzione attesa di ciascun impianto fotovoltaico usando un _derating_ di default dello 0.5%. Ad esempio, il risultato potrebbe essere:

!["Stime di produzione impianti"](docimages/pv_arcs.png)

Si noti che la produzione in questo caso è in kWh, che idealmente dovrebbe allinearsi alla scala usata per le barre. A me però fa strano; preferisco ragionare in termini di potenze instantanee, che mi rende di più l'idea di un comportamento teorico. È possibile mostrare potenze istantanee ricorrendo al parametro `--pv-pp`. L'effetto, nel dettaglio:

| Default (kWh) | Con `--pv-pp` (kW) |
|----|----|
|!["Picchi produzione in kWh"](docimages/kwh_peak.svg)|!["Picchi di produzione istantanei"](docimages/instant_kw_peak.svg)|

## Usi avanzati

Il coefficiente di _derating_ è tipicamente oscuro quindi non fa parte dei parametri d'impianto forniti da `--pv` ma è possibile indicarlo tramite il parametro `--pv-derate`. Il valore è una serie di valori numerici separati da `,`. Il valore i-esimo sarà usato per l'impianto i-esimo, sovrascrivendo il valore di derating di default. Si può fornire più o meno numeri di quanti impianti (l'eccesso è ignorato). Inoltre è possibile mantenere il derating di default indicando come valore la stringa vuota, ad esempio: `0.01,,0.008` imposta il derating solo per il primo e il terzo impianto mantenendo quello di mezzo al valore di default. Potrebbe esserci un quarto impianto; non importa.

Come i programmi precedenti, è possibile comporre i comandi. La creazione degli SVG è un'operazione veloce e non dovrebbe essercene bisogno se non magari per limitare il fetching di dati ad esempio da OpenMeteo:

```
echo "2025-10_attiva_imm.csv\\n2025-10_attiva_prel.csv" | python3 draw_quarters.py --location '45.xxx, 12.xxx' --pv '210;38;5.76;YYYY-MM;Old' --pv '60;38;5.72;YYYY-MM;New' --pv-pp --pv-derate 0.009
```

Quindi è possibile anche fare tutto in un colpo:


```
python3 dl_quarters.py | python3 quarters_to_csv.py | python3 draw_quarters.py --location '45.xxx, 12.xxx' --pv '210;38;5.76;YYYY-MM;Old' --pv '60;38;5.72;YYYY-MM;New' --pv-pp --pv-derate 0.009
```

# Fine
Per me purtroppo il decorso non è stato quello che speravo. In ogni caso vista la situazione - terribile - dalla _data ownership_, chissà quando in Italia ne parleremo sentivo di dover lasciar libero questo lavoro.

Spero vi tornerà utile.