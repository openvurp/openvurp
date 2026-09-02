# Diario delle versioni

## 5.0.0 — openvurp è un portafoglio di agenti

Il cambio grosso: openvurp non è più *un* agente con una dashboard di
contorno. È una rubrica di agenti che fai tu, che si conoscono fra loro e che
si parlano — e l'interfaccia è la pagina web, non il terminale.

### La rubrica

- `openvurp` apre il **portafoglio nel browser**. Il terminale resta, ma su
  richiesta: `openvurp --cli`.
- **Nessun agente precotto.** La rubrica nasce vuota: li crei tu, con nome,
  ruolo e carattere. Anche openvurp sparisce dall'elenco — non era un agente,
  era l'ospite.
- **Gli agenti hanno gli strumenti veri**: leggono file, eseguono comandi,
  aprono pagine. Quello che fanno si vede mentre lo fanno.
- **Si conoscono.** Ogni agente sa chi altro c'è e cosa sa fare, e `ask_peer`
  porta la rubrica *dentro* lo strumento, non in una riga di prompt lontana:
  i nomi sono un elenco chiuso, i mestieri stanno nella descrizione. Con
  `ask_everyone` si chiede a tutti in una volta, e chi non c'entra tace.
  `who_is_there` risponde a rubrica cambiata, anche a metà lavoro.

### La stanza

- **Discutono finché hanno qualcosa da dire**, non per un numero di giri
  deciso a tavolino. Si chiude da sola quando in un giro intero nessuno apre
  bocca, e la fermi tu quando vuoi.
- **Arrivano a una conclusione.** Chiude chi ha aperto, dicendo su cosa siete
  d'accordo, su cosa no e per nome, e cosa serve per decidere — con il divieto
  esplicito di inventare un accordo che non c'è.
- Il silenzio è una risposta ammessa. Nessuno parla per obbligo.

### I canali

- **Un solo cuore di conversazione** (`core/conversation.py`): Telegram,
  Discord, Slack e WhatsApp non hanno una loro idea di conversazione, passano
  per la stessa strada della pagina web. Un test impedisce a un canale di
  toccare da solo rubrica, stanze o sciame.
- Telegram in entrata è passato da 1.064 a 150 righe, senza dipendenze in più.
- **Un canale senza lista di autorizzati non parte.** Vuoto vale nessuno.
- WhatsApp funziona solo a webhook (è come è fatta l'API di Meta) e resta
  chiuso senza `WHATSAPP_APP_SECRET`: la firma HMAC è ciò che protegge
  l'unico ingresso che non può presentare il token della dashboard.

### La pagina

- **Impostazioni e pannelli sono pagine**, non finestre sopra la chat.
- «Cosa hanno fatto i tuoi agenti»: ogni comando, ricerca e file toccato, dal
  registro di controllo che c'era già su disco e che niente mostrava.
- Approvazioni chieste **dove hai chiesto l'azione**: un agente aperto dal
  browser non fa più comparire la domanda in un terminale che non guardi.
- Streaming, animazione di due agenti che si consultano, trascinamento di file
  ovunque nella chat, barra laterale richiudibile.
- Quello che sta accadendo vive nello stato, non nel DOM: cambiando chat non
  si perde più niente.

### Tolto

- **Telegram in entrata vecchio** (~1.870 righe): parlava al vecchio openvurp
  singolo e non sapeva niente di rubrica, stanze e approvazioni. Sostituito.
- Le liste di parole che indovinavano se un messaggio fosse un saluto: quel
  mestiere è del modello, e sbagliavano.

## 4.x

Vedi la cronologia git.
