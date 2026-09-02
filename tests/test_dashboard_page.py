"""La pagina servita dalla dashboard deve essere eseguibile, non solo contenere
le stringhe giuste.

Regressione reale: un `try{` rimasto senza `catch` ha reso l'INTERO script non
parsabile, e la dashboard mostrava una pagina vuota — nessun agente, nessuna
chat. Tutti i controlli fatti fino a quel momento erano ricerche di sottostringhe
sull'HTML, e una ricerca di sottostringhe non puo' accorgersi di un errore di
sintassi: le stringhe c'erano tutte, il codice non partiva.
"""

import json
import re
import shutil
import subprocess
import tempfile

import pytest

import dashboard as D


def _page() -> str:
    with tempfile.TemporaryDirectory() as workspace:
        server = D.DashboardServer(port=0, workspace_dir=workspace, token="t")
        handler = server.handler_class
        # _serve_html non tocca la rete: costruiamo la pagina senza aprire porte.
        captured: dict[str, bytes] = {}

        class _Fake(handler):  # type: ignore[misc, valid-type]
            def __init__(self):  # noqa: D107 - niente socket
                self.wfile = type("W", (), {"write": lambda _s, b: captured.setdefault("body", b)})()

            def send_response(self, *_a, **_k):
                pass

            def send_header(self, *_a, **_k):
                pass

            def end_headers(self):
                pass

        _Fake()._serve_html()
        return captured["body"].decode("utf-8")


def _script(page: str) -> str:
    """Il codice della pagina.

    Attenzione: il primo <script> e' il blocco di stato iniziale (la rubrica
    servita insieme alla pagina). Quello che interessa qui e' il programma,
    cioe' il piu' lungo.
    """
    blocchi = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert blocchi, "la pagina non contiene uno <script>"
    return max(blocchi, key=len)


def test_page_has_a_script():
    assert len(_script(_page())) > 5000


@pytest.mark.skipif(shutil.which("node") is None, reason="node non disponibile")
def test_served_javascript_actually_parses(tmp_path):
    """Il controllo che sarebbe servito: il browser deve poterlo eseguire."""
    script = tmp_path / "dash.js"
    script.write_text(_script(_page()), encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(script)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        "JavaScript della dashboard non parsabile — la pagina resterebbe vuota:\n"
        + (result.stderr or result.stdout)[:1500]
    )


def test_every_element_the_script_wires_exists_in_the_markup():
    """Un `$("#x").onclick` su un id inesistente esplode a runtime.

    Il parser non lo vede: la sintassi e' valida, l'elemento no.
    """
    page = _page()
    script = _script(page)
    wired = set(re.findall(r'\$\("#([a-zA-Z0-9_-]+)"\)', script))
    present = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', page))
    # I campi delle impostazioni nascono a runtime dai costruttori di scelte
    # (`setSelect`, `setField`, `setSwitch`): il loro id non compare nel markup
    # perche' viene composto, non scritto. Vanno controllati per un'altra via.
    generati = {i for i in wired if i.startswith(("s-", "sw-"))}
    missing = sorted(wired - present - generati)
    assert not missing, f"lo script cerca id che non esistono nella pagina: {missing}"

    # E i generati devono venire davvero da un costruttore, non da un refuso.
    for costruttore in ('id="s-\'+id+\'"', 'id="sw-\'+id+\'"'):
        assert costruttore in script, f"manca il costruttore {costruttore}"


def test_a_missed_drop_cannot_navigate_away_from_the_chat():
    """Il drop valeva solo sopra .thread.

    Sbagliare mira di un centimetro — sul composer, sulla colonna degli agenti,
    sul bordo — faceva scattare il comportamento predefinito del browser: apre
    il file e la chat sparisce. L'annullamento va messo sulla finestra, non su
    un riquadro.
    """
    js = _script(_page())
    for event in ("dragenter", "dragover", "dragleave", "drop"):
        assert f'window.addEventListener("{event}"' in js, f"{event} non e' sulla finestra"
    assert '$("#thread").addEventListener("drop"' not in js, "e' rimasto il drop sul solo thread"


def test_the_drop_area_is_visible_and_says_what_it_accepts():
    page = _page()
    assert 'id="dropveil"' in page and ".dropveil.on{display:flex}" in page
    js = _script(page)
    assert "Drop it here" in page
    # Senza contatore, dragleave scatta passando su un figlio e il velo lampeggia.
    assert "dragDepth" in js
    # E se non c'e' una chat aperta il file non ha dove andare: va detto.
    assert "open a chat first" in js
    assert 'setErr("open a chat before attaching")' in js


def test_leaving_a_chat_counts_as_having_read_it():
    """Il pallino tornava proprio sulla chat da cui stavi uscendo.

    Nascondere il badge mentre sei dentro non basta: appena apri un'altra chat
    quella di prima smette di essere «corrente» e i messaggi arrivati sotto i
    tuoi occhi ricompaiono come non letti. Chi esce ha letto.
    """
    js = _script(_page())
    assert "function leaveChat()" in js
    assert "leaveChat();" in js, "nessuno la chiama"
    # Va chiamata sia cambiando chat sia tornando alla schermata vuota.
    assert js.count("leaveChat();") >= 2
    # E un messaggio della stanza visto arrivare non e' da leggere.
    assert "markRead(e.chat_id)" in js


def test_you_can_stop_the_room_from_the_page():
    """La discussione non finisce piu' a giri contati: serve poterla fermare."""
    page = _page()
    assert 'id="roomstop"' in page and 'id="roombar"' in page
    js = _script(page)
    assert '/stop","POST"' in js
    # Il pulsante deve dire cosa sta succedendo, non restare muto.
    assert "stopping them" in js
    # E quando finisce, va detto perche'.
    assert "discussion stopped by you" in js
    assert "nobody had anything left to add" in js


def test_what_is_happening_now_lives_in_state_not_in_the_dom():
    """Cambiando chat l'animazione spariva, e non tornava piu'.

    Le due facce che si parlano, il turno della stanza e la riga «sta
    scrivendo» erano nodi appesi direttamente a #inner. Ma paint() ricostruisce
    #inner da zero: bastava un ridisegno — cambiare chat, un messaggio nuovo —
    per cancellarli, e tornando indietro non restava niente perche' non erano
    scritti da nessuna parte.
    """
    js = _script(_page())
    # Lo stato per chat deve prevedere cio' che e' in corso.
    assert "peers:[],turn:null" in js
    # Nessuno deve piu' cercare o appendere quei nodi per id.
    assert '"#peer-"' not in js
    assert '"#roomturn"' not in js
    # E ogni ridisegno deve rimettere l'effimero al suo posto.
    assert js.count("paintLive();") >= 2
    inner = js[js.index('$("#inner").innerHTML=out.join("")'):]
    assert inner.index("paintLive()") < 200, "paint() non ridisegna cio' che e' in corso"


def test_a_peer_exchange_survives_leaving_and_coming_back():
    js = _script(_page())
    # L'evento va nello stato della SUA chat anche se stai guardando altrove.
    assert "const st=liveOf(e.chat_id);" in js
    assert "st.peers.push(" in js
    # La risposta aggiorna lo stato, non il nodo: se sei via, la ritrovi.
    assert "p.answer=e.answer" in js
    assert "si ridisegna al rientro" in js


def test_the_closing_line_of_a_discussion_is_not_lost_on_repaint():
    js = _script(_page())
    assert "roomEnds={}" in js
    assert "roomEnds[e.chat_id]=perche" in js
    assert "delete roomEnds[currentChat]" in js, "una chiusura vecchia resterebbe appesa"


def test_no_javascript_leaks_outside_the_script_tag():
    """Una funzione intera e' finita stampata in cima alla pagina, come testo.

    Causa: una sostituzione con stringa vuota. `html.replace("", nuovo, 1)` non
    fallisce — inserisce il testo all'INIZIO del documento. Il controllo con
    `node --check` non poteva accorgersene: guarda dentro <script>, e del testo
    sciolto prima del doctype e' HTML perfettamente valido.

    Qui si guarda quello che il browser DISEGNA: prima del doctype non ci deve
    essere niente, e fuori da <script>/<style> non ci deve essere codice.
    """
    page = _page()
    assert page.startswith("<!DOCTYPE html>"), repr(page[:120])

    visibile = re.sub(r"<script>.*?</script>", "", page, flags=re.S)
    visibile = re.sub(r"<style>.*?</style>", "", visibile, flags=re.S)
    for spia in ("function ", "=>", "const ", "document.querySelector"):
        assert spia not in visibile, f"codice sfuggito nel markup: {spia!r}"


def test_the_page_defines_each_function_once():
    """Due copie della stessa funzione = una sostituzione andata a vuoto."""
    js = _script(_page())
    for nome in ("paintLive", "showPeer", "answerPeer", "roomTurn", "roomSaid",
                 "roomEnd", "openChat", "paint", "leaveChat"):
        n = js.count(f"function {nome}(")
        assert n == 1, f"{nome} definita {n} volte"


def test_settings_are_a_page_not_a_modal():
    """Erano un avviso sopra la chat: una finestrella con dentro un riquadro
    scrollabile con dentro le sezioni. Le impostazioni sono una pagina."""
    page = _page()
    assert 'id="setpage"' in page and 'id="setbody"' in page
    assert 'id="setback"' in page, "manca il modo di tornare indietro"
    assert 'id="setsave"' in page, "il salvataggio deve stare in vista, non in fondo"
    js = _script(page)
    assert "function showSettings(" in js
    # Nessun residuo del modale: due strade per la stessa cosa divergono.
    for morto in ("#s-cancel", "#s-ok", "closeModal,700"):
        assert morto not in js, f"residuo del modale: {morto}"


def test_settings_are_reachable_without_hunting_in_a_menu():
    page = _page()
    assert 'id="gear"' in page
    assert '$("#gear").onclick' in _script(page)


def test_only_one_view_can_be_on_at_a_time():
    """Due viste accese insieme sono un modale mascherato.

    Prima ogni vista si accendeva e spegneva da sola: bastava dimenticare una
    riga in un punto per ritrovarsi la chat sotto le impostazioni. Ora decide
    un posto solo, e nessun altro tocca il `display` di quei riquadri.
    """
    js = _script(_page())
    assert "function showView(" in js

    corpo = js[js.index("function showView("):]
    corpo = corpo[:corpo.index("\nfunction ")]
    for riquadro in ("#setpage", "#panelpage", "#blank", "#thread", "#composer"):
        assert riquadro in corpo, f"showView non governa {riquadro}"

    # Fuori da showView nessuno deve accendere o spegnere una vista a mano.
    fuori = js.replace(corpo, "")
    for riquadro in ("#setpage", "#panelpage", "#blank", "#thread"):
        assert f'$("{riquadro}").style.display' not in fuori, (
            f"{riquadro} viene acceso anche fuori da showView")

    # E ogni ingresso passa di li'.
    for entrata in ("function openChat(", "function showBlank(",
                    "async function openPanel(", "async function openSettings("):
        blocco = js[js.index(entrata):]
        blocco = blocco[:blocco.index("\n}\n") + 3]
        assert "showView(" in blocco or "showSettings(" in blocco, entrata


def test_the_settings_page_explains_the_dangerous_switch():
    """Una lista di autorizzati vuota e' l'unica cosa fra te e chiunque."""
    js = _script(_page())
    assert "means <b>nobody</b>" in js
    assert "anyone can command my computer" in js
    # WhatsApp c'e' via Baileys, e il rischio va detto DOVE si accende:
    # client non ufficiale, Meta puo' bannare il numero, quindi secondario.
    assert "UNOFFICIAL" in js
    assert "can ban the paired" in js
    assert "spare number" in js
    # E il QR arriva da solo nella pagina, senza ricaricare.
    assert "aggiornaWA" in js and "/api/whatsapp/status" in js



def test_the_panels_are_a_page_too():
    """«Cosa hanno fatto i tuoi agenti» si sfoglia, non si sbircia."""
    page = _page()
    assert 'id="panelpage"' in page and 'id="panelbody"' in page
    assert 'id="ptabs"' in page, "senza schede si torna indietro per cambiare vista"
    js = _script(page)
    assert "PANNELLI" in js
    # Nessun residuo del modale.
    assert "#p-close" not in js
    # E ogni scheda dice cosa mostra, non solo come si chiama.
    assert "dal registro di controllo" in js


def test_an_empty_panel_says_what_would_fill_it():
    js = _script(_page())
    assert "Appena un agente" in js
    assert "Nessuna sessione aperta" in js


def test_the_empty_screen_says_what_to_do_next():
    """Dopo un clone la rubrica e' vuota per scelta, ma c'era solo un polpo.

    Chi apre openvurp la prima volta non sa che deve creare un agente, ne'
    cosa scriverci dentro: il ruolo non e' un'etichetta, e' cio' che permette
    agli altri di capire che quella cosa e' roba sua.
    """
    page = _page()
    assert 'id="bsteps"' in page and 'id="bcta"' in page
    for pezzo in ("+ New", "All together", "know each other", "hunts Amazon deals"):
        assert pezzo in page, f"la guida non dice: {pezzo}"
    js = _script(page)
    assert '$("#bcta").onclick' in js, "il bottone non fa niente"
    # Con degli agenti gia' fatti, la guida ai primi passi diventa rumore.
    assert "function paintBlank(" in js
    assert "paintBlank()" in js.replace("function paintBlank()", ""), "nessuno la chiama"
    assert "Choose who to talk to" in js


def test_the_page_arrives_with_the_roster_already_inside():
    """Aprendo openvurp si leggeva per un attimo «non hai ancora nessun agente».

    La pagina era statica e la rubrica veniva chiesta dopo: il browser
    disegnava il vuoto, poi a rete finita gli agenti. Ma il server la rubrica
    ce l'ha gia' quando serve la pagina.
    """
    import tempfile
    import dashboard as D
    from core.chat_store import ChatStore

    lavoro = tempfile.mkdtemp()
    store = ChatStore(f"{lavoro}/memory")
    for nome in ("amanda", "ciccio"):
        store.create_agent(nome, "ruolo", "", "", "")

    server = D.DashboardServer(port=0, workspace_dir=lavoro, token="t")
    catturato: dict = {}

    class _Fake(server.handler_class):
        def __init__(self):
            self.wfile = type("W", (), {
                "write": lambda _s, b: catturato.setdefault("body", b)})()

        def send_response(self, *_a, **_k): pass
        def send_header(self, *_a, **_k): pass
        def end_headers(self): pass

    _Fake()._serve_html()
    pagina = catturato["body"].decode("utf-8")

    assert "__BOOT__" in pagina
    dati = json.loads(re.search(r"window\.__BOOT__=(.*?);</script>", pagina, re.S)
                      .group(1).replace("\\u003c", "<").replace("\\u003e", ">"))
    assert sorted(a["name"] for a in dati["roster"]) == ["amanda", "ciccio"]
    # E deve arrivare PRIMA dello script che lo legge, o non serve a niente.
    assert pagina.index("__BOOT__") < pagina.index("const BOOT=window.__BOOT__")


def test_a_name_that_looks_like_a_tag_cannot_break_the_page():
    """Un agente chiamato «</script>» chiuderebbe il tag: il resto della
    pagina finirebbe stampato come testo."""
    import tempfile
    import dashboard as D
    from core.chat_store import ChatStore

    lavoro = tempfile.mkdtemp()
    store = ChatStore(f"{lavoro}/memory")
    store.create_agent("</script><b>ciao", "ruolo", "", "", "")

    server = D.DashboardServer(port=0, workspace_dir=lavoro, token="t")
    catturato: dict = {}

    class _Fake(server.handler_class):
        def __init__(self):
            self.wfile = type("W", (), {
                "write": lambda _s, b: catturato.setdefault("body", b)})()

        def send_response(self, *_a, **_k): pass
        def send_header(self, *_a, **_k): pass
        def end_headers(self): pass

    _Fake()._serve_html()
    pagina = catturato["body"].decode("utf-8")
    blocco = re.search(r"window\.__BOOT__=(.*?);</script>", pagina, re.S).group(1)
    assert "</script>" not in blocco and "<b>" not in blocco
    assert "\\u003c" in blocco, "il carattere pericoloso non e' stato neutralizzato"


def test_a_file_can_be_looked_at_without_downloading_it():
    """Scaricare, aprire altrove e tornare indietro sono tre gesti per vedere
    una cosa che l'agente ha appena prodotto."""
    page = _page()
    assert 'id="prev"' in page and 'id="prevbody"' in page
    js = _script(page)
    assert "async function apriFile(" in js
    # PDF e immagini si mostrano, il resto si legge come codice numerato.
    assert 'est==="pdf"' in js and "<iframe" in js
    assert "IMMAGINI" in js
    assert "function codiceHTML(" in js
    # E se non si puo', va detto PERCHE', non solo che non si puo'.
    assert "mai per .env, database o chiavi" in js


def test_code_blocks_can_be_opened_and_copied():
    js = _script(_page())
    assert 'data-code="' in js and 'data-copy="' in js
    # I messaggi si ridisegnano di continuo: agganciare ogni bottone a ogni
    # ridisegno e' una perdita garantita. Serve la delega.
    assert '$("#inner").addEventListener("click"' in js


def test_a_file_path_inside_a_command_is_clickable():
    js = _script(_page())
    assert "function percorsiApribili(" in js
    assert 'class="fileref"' in js
    assert "RX_PERCORSO" in js


def test_leaving_the_chat_closes_the_preview():
    js = _script(_page())
    corpo = js[js.index("function showView("):]
    corpo = corpo[:corpo.index("\nfunction ")]
    assert "chiudiPrev()" in corpo


def test_a_folded_sidebar_still_says_who_is_who():
    """Restavano solo i polpi: distinti fra loro, ma senza un nome."""
    page = _page()
    js = _script(page)
    assert 'class="rmini"' in js, "manca il nome sotto l'avatar"
    assert 'class="rtip"' in js, "manca la targhetta al passaggio"
    # Il nome in piccolo si vede solo a colonna chiusa, non sempre.
    assert ".rmini,.rtip{display:none}" in page
    assert "body.folded .rmini{display:block" in page
    # La targhetta non deve allargare la colonna: aprendo l'overflow comparivano
    # una barra orizzontale e una pagina piu' larga dello schermo. Esce dal
    # flusso e la posiziona il codice.
    assert "body.folded .rtip{position:fixed" in page
    assert "overflow-x:hidden" in page
    assert "function seguiTarghette(" in js
    # E l'ingranaggio deve restare raggiungibile proprio a colonna chiusa.
    assert "body.folded .rfoot{display:flex" in page
    # E deve valere anche per la stanza, non solo per gli agenti.
    assert "All together</b>" in js



def test_only_the_agent_list_scrolls_not_the_whole_column():
    """Scorrendo tutta la colonna, cercando un agente in fondo sparivano la
    ricerca, il «+ Nuovo» e l'ingranaggio: proprio le cose che devono restare
    a portata mentre scorri."""
    page = _page()
    assert ".left{flex:0 0 420px;border-right:1px solid var(--border);overflow:hidden" in page
    assert ".left #rrows{flex:1;min-height:0;overflow-y:auto" in page
    assert ".left .rtop,.left .rsearch,.left .rheads,.left .rfoot{flex:0 0 auto}" in page


def test_the_settings_icon_is_a_gear_not_the_actions_sun():
    """Stessa forma in due posti diversi: nessuna delle due dice piu' niente."""
    page = _page()
    inizio = page.index('id="gear"')
    bottone = page[inizio:inizio + 1400]
    assert "circle cx=\"12\" cy=\"12\" r=\"3.2\"" in bottone
    # Il solicello delle azioni ha i raggi dritti: qui non devono esserci.
    assert "M12 2v3M12 19v3M2 12h3M19 12h3" not in bottone


def test_the_settings_page_fills_the_pane():
    """«Schiacciata con la scrollbar»: la colonna destra non era una colonna
    flessibile, quindi la pagina non poteva ne' crescere ne' far scorrere solo
    il proprio corpo."""
    page = _page()
    assert ".pane.right{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0}" in page
    assert ".setbody{flex:1;min-height:0;overflow-y:auto" in page
    # E su schermo stretto la destra deve comparire anche fuori dalla chat.
    assert "body.insettings .right,body.inpanel .right{display:flex}" in page
    assert 'classList.toggle("inpanel"' in _script(page)


def test_the_page_does_not_wait_for_the_backend_probes():
    """La prima pagina ci metteva 2,9 secondi, e non era il database.

    Misurato: aprire il database 63 ms, leggere la rubrica 14 ms — mentre
    `provider_catalog()`, che sonda Ollama e gli accessi ai CLI, alla prima
    chiamata costa 3,2 secondi. Serve alle impostazioni e al badge del motore,
    non al primo disegno: sta fuori dalla pagina, e viene scaldato a parte.
    """
    import inspect
    import dashboard as D

    sorgente = inspect.getsource(D.DashboardHandler._boot_script)
    assert "roster" in sorgente and "team_room" in sorgente
    assert "provider_catalog()" not in sorgente, (
        "la sonda dei backend e' tornata dentro la pagina")

    scaldata = inspect.getsource(D.DashboardServer._scalda)
    assert "provider_catalog()" in scaldata, "nessuno la scalda: la pagherebbe /api/providers"
    assert "agent_roster()" in scaldata


def test_a_cold_database_does_not_hold_the_page_hostage():
    """Se la lettura e' ancora in corso, la pagina parte lo stesso: gli agenti
    arrivano un attimo dopo, invece di far guardare il bianco."""
    import inspect
    import dashboard as D
    sorgente = inspect.getsource(D.DashboardHandler._boot_script)
    assert "boot_ready" in sorgente
    assert "store = None" in sorgente


def test_no_hidden_container_keeps_its_share_of_the_pane():
    """Le impostazioni restavano schiacciate con la loro barra.

    Avvolgendo la chat in un contenitore per fare posto all'anteprima, quel
    contenitore e' rimasto `flex:1` sempre acceso: `showView` nascondeva il
    thread dentro, non il contenitore. Da vuoto continuava a prendersi meta'
    riquadro, e la pagina che doveva occuparlo si stringeva.
    """
    js = _script(_page())
    corpo = js[js.index("function showView("):]
    corpo = corpo[:corpo.index("\nfunction ")]
    # Ogni riquadro di primo livello dentro la colonna destra dev'essere
    # governato qui, contenitori compresi.
    for riquadro in ("#setpage", "#panelpage", "#blank", "#withprev", "#composer"):
        assert riquadro in corpo, f"showView non spegne {riquadro}"


def test_you_can_talk_to_the_agents_from_the_web_too():
    """Parlare invece di scrivere. Il file va all'agente come un allegato
    qualsiasi: e' lui che lo trascrive, con lo strumento che ha gia'."""
    page = _page()
    assert 'id="mic"' in page
    js = _script(page)
    assert "MediaRecorder" in js
    assert "getUserMedia" in js
    # Il formato lo sceglie il browser: se non lo accettiamo, l'invio fallisce.
    import dashboard as D
    for est in (".webm", ".ogg", ".m4a"):
        assert est in D.DashboardHandler.UPLOAD_SUFFIXES, est
    # Un microfono negato deve dirlo, non restare muto.
    assert "microphone denied by the browser" in js
    # E una registrazione lunghissima si chiude da sola.
    assert "s>=180" in js


def test_a_web_voice_note_is_transcribed_by_the_browser_not_whisper():
    """Ogni vocale dal web tornava con «il servizio e' andato in timeout».

    Su questa macchina perfino IMPORTARE Whisper muore oltre i 90 secondi: la
    strada giusta e' quella che gia' funzionava a voce — trascrive il browser,
    mentre si registra. Il testo nasce insieme all'audio, l'audio resta per
    riascoltarlo, e all'agente viene detto di NON ritrascrivere.
    """
    import inspect
    import dashboard as D
    js = _script(_page())
    # Il riconoscimento parte INSIEME alla registrazione.
    assert "micRec=new R()" in js and "micRec.continuous=true" in js
    # Il file porta scritto nel nome che il testo c'e' gia'.
    assert 'vocale"+(micTesto?"-trascritta":"")' in js
    # E la coda del riconoscimento (che arriva dopo lo stop) non si perde.
    assert "setTimeout(async()=>" in js

    sorgente = inspect.getsource(D.make_chat_fn)
    assert "-trascritta" in sorgente
    assert "NON usare audio_transcribe" in sorgente
    # Il caso non trascritto (Firefox, o un file caricato a mano) resta.
    assert "audio_transcribe per le note vocali" in sorgente


def test_the_row_preview_shows_words_not_markdown():
    """Nella rubrica si leggeva «**Crucial P3 Plus**» con gli asterischi.

    Renderizzare il markdown li' non e' la risposta: darebbe grassetti e
    titoli dentro un rigo alto quindici pixel. L'anteprima e' una frase, non
    un messaggio in miniatura — si toglie la punteggiatura e resta il testo.
    """
    js = _script(_page())
    assert "function anteprimaTesto(" in js
    # Deve valere sia per gli agenti sia per la stanza.
    assert "anteprimaTesto(a.preview)" in js
    assert "anteprimaTesto(room.preview)" in js
    assert "esc(a.preview" not in js, "una riga mostra ancora il markdown grezzo"


@pytest.mark.skipif(shutil.which("node") is None, reason="node non disponibile")
def test_the_preview_cleaner_actually_cleans(tmp_path):
    """Verificato eseguendolo, non leggendolo."""
    js = _script(_page())
    fn = js[js.index("function anteprimaTesto("):]
    fn = fn[:fn.index("\nfunction roomRow")]
    casi = [
        ("**Crucial P3 Plus a 74 €**", "Crucial P3 Plus a 74 €"),
        ("## Titolo\n- primo\n- secondo", "Titolo · primo · secondo"),
        ("vedi `smartctl -A`", "vedi smartctl -A"),
        ("[Amazon](https://amazon.it) ha l'offerta", "Amazon ha l'offerta"),
        ("```\nx = 1\n```", "‹codice›"),
        ("![foto](x.png) guarda", "foto guarda"),
    ]
    prova = fn + "\n" + "".join(
        f"console.log(anteprimaTesto({dentro!r}));\n" for dentro, _ in casi)
    script = tmp_path / "p.js"
    script.write_text(prova, encoding="utf-8")
    esito = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert esito.returncode == 0, esito.stderr
    uscite = esito.stdout.strip().split("\n")
    for (dentro, atteso), uscita in zip(casi, uscite):
        assert uscita == atteso, f"{dentro!r} -> {uscita!r}, atteso {atteso!r}"


def test_an_attachment_is_seen_and_heard_not_read_as_a_path():
    """In chat si leggeva l'intero blocco di istruzioni con il percorso del
    file, e l'audio era il rettangolo grigio del browser."""
    js = _script(_page())
    assert "function spezzaAllegati(" in js
    assert "function allegatiHTML(" in js
    # Il lettore e' il nostro: play col colore del marchio, barra che si tocca.
    assert 'class="aplay"' in js and 'class="ap-btn"' in js
    assert "function apClic(" in js
    assert "<audio" not in js, "e' tornato il rettangolo grigio del browser"
    assert '<img class="att-img"' in js
    # In rubrica un vocale si presenta per quello che e'.
    assert "messaggio vocale" in js
    # E nessuno inventa un testo al posto tuo.
    import inspect
    import dashboard as D
    assert "Ho allegato dei file" not in inspect.getsource(D)


@pytest.mark.skipif(shutil.which("node") is None, reason="node non disponibile")
def test_the_attachment_splitter_actually_splits(tmp_path):
    js = _script(_page())
    fn = js[js.index("function spezzaAllegati("):]
    fn = fn[:fn.index("\nfunction allegatiHTML")]
    prova = fn + """
const dentro = "Ho allegato dei file: guardali.\\n\\n[ALLEGATI dall'utente — aprili con il tool adatto (image_analyze per le immagini)]\\n- /tmp/uploads/vocale.webm\\n- /tmp/uploads/foto.jpg";
const fuori = spezzaAllegati(dentro);
console.log(JSON.stringify(fuori));
console.log(JSON.stringify(spezzaAllegati("solo testo, nessun blocco")));
"""
    script = tmp_path / "s.js"
    script.write_text(prova, encoding="utf-8")
    esito = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert esito.returncode == 0, esito.stderr
    righe = esito.stdout.strip().split("\n")
    primo = json.loads(righe[0])
    assert primo["testo"] == "Ho allegato dei file: guardali."
    assert primo["files"] == ["/tmp/uploads/vocale.webm", "/tmp/uploads/foto.jpg"]
    secondo = json.loads(righe[1])
    assert secondo["files"] == [] and secondo["testo"] == "solo testo, nessun blocco"


def test_the_voice_conversation_is_immediate_by_construction():
    """Registra→carica→Whisper→modello→sintesi→scarica sono secondi: non e'
    una conversazione. Qui ascolta il browser, parla il browser, e comincia
    a parlare alla prima frase mentre il resto arriva in streaming."""
    page = _page()
    assert 'id="voicemode"' in page and 'id="vm-scene"' in page
    js = _script(page)
    assert "webkitSpeechRecognition" in js, "senza riconoscimento locale si torna ai secondi"
    assert "SpeechSynthesisUtterance" in js, "senza sintesi locale si torna ai file"
    # La frase parte appena e' completa, non a risposta finita.
    assert "voceToken" in js and "vm.buf.match" in js
    # Il markdown non si legge ad alta voce.
    assert "anteprimaTesto(senzaEmoji(testo))" in js
    # Ogni agente ha la sua voce, derivata dal suo id.
    assert "u.pitch=0.82" in js and "semino(chi)" in js
    # E il browser sbagliato riceve una spiegazione, non un pulsante muto.
    assert "Chrome ed Edge" in js


def test_the_voice_turns_pass_naturally():
    js = _script(_page())
    # Finito di parlare, torna ad ascoltare da solo: nessun pulsante in mezzo.
    fine = js[js.index("u.onend=u.onerror="):]
    assert "ascolta()" in fine[:250]
    # Toccare mentre parla e' interromperlo, come tra persone.
    scena = js[js.index('$("#vm-scene").onclick'):]
    assert "speechSynthesis.cancel" in scena[:260]
    # Uscire dalla chat chiude la voce: niente sintesi fantasma in sottofondo.
    vista = js[js.index("function showView("):]
    assert "chiudiVoce()" in vista[:220]
    # Nella stanza, chi parla si fa avanti e ha la sua voce.
    stanza = js[js.index("function roomSaid("):]
    assert "parla(e.text" in stanza[:400]


@pytest.mark.skipif(shutil.which("node") is None, reason="node non disponibile")
def test_no_python_escapes_leak_into_the_javascript(tmp_path):
    """In rubrica compariva «U0001f399 messaggio vocale».

    La pagina viene scritta da script Python, e un escape di Python
    (`\\U0001F399`, otto cifre) e' finito dentro una stringa JavaScript, che
    non lo conosce: `\\U` diventa una «U» qualsiasi e le cifre restano li'.
    Il parser non se ne accorge — e' una stringa valida — quindi il controllo
    giusto e' ESEGUIRE la funzione e guardare cosa esce.
    """
    page = _page()
    assert not re.search(r"\\\\U000[0-9a-fA-F]{5}", page), \
        "un escape di Python e' dentro il JavaScript"

    js = _script(page)
    fn = js[js.index("function anteprimaTesto("):]
    fn = fn[:fn.index("\nfunction roomRow")]
    prova = fn + """
console.log(anteprimaTesto("ciao\\n\\n[ALLEGATI dall'utente]\\n- /tmp/vocale.webm"));
"""
    script = tmp_path / "u.js"
    script.write_text(prova, encoding="utf-8")
    esito = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert esito.returncode == 0, esito.stderr
    fuori = esito.stdout.strip()
    assert "U0001" not in fuori, f"escape rotto visibile all'utente: {fuori!r}"
    assert "messaggio vocale" in fuori


def test_a_consultation_is_staged_in_the_voice_scene():
    """«Fammi vedere che chiama l'agente, ci parla, e poi va via».

    Gli eventi c'erano gia' (peer/peer_done, gli stessi dei due polpi in chat):
    a voce l'ospite entra nella scena, risponde con la SUA voce — il tono
    cambia, e' il modo in cui capisci che ha parlato qualcun altro — e quando
    ha detto la sua se ne va.
    """
    page = _page()
    js = _script(page)
    for fn in ("vmPeerArriva", "vmPeerRisponde", "vmPeerVia"):
        assert f"function {fn}(" in js, f"manca {fn}"
    # Aggancio agli eventi veri, non un giro parallelo: dentro showPeer e
    # answerPeer, prima di tutto il resto.
    corpo = js[js.index("function showPeer("):]
    corpo = corpo[:corpo.index("\nfunction ")]
    assert "vmPeerArriva(e);" in corpo
    corpo = js[js.index("function answerPeer("):]
    corpo = corpo[:corpo.index("\nfunction ")]
    assert "vmPeerRisponde(e);" in corpo
    # L'ospite risponde con la sua voce e POI se ne va: l'ordine e' un callback,
    # non un timer a occhio.
    assert 'parla(e.answer||"",e.to_id,()=>vmPeerVia(e.to_id))' in js
    # Entrata e uscita sono transizioni, e l'ospite e' marcato come tale.
    assert ".vm-who.ospite.qui{opacity:1" in page
    assert ".vm-who.ospite.via{opacity:0" in page
    assert "dropping by" in page


def test_folding_the_sidebar_cannot_hide_buttons_elsewhere():
    """Chiudevi la colonna e sparivano «parla» e il menu della chat.

    La regola `body.folded .rmore{display:none}` era pensata per i puntini
    delle righe della rubrica, ma prendeva OGNI .rmore della pagina — anche
    l'intestazione della chat, dall'altra parte dello schermo. Ogni regola di
    `folded` deve nominare `.left`.
    """
    page = _page()
    css = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)   # i commenti non sono regole
    for riga in css.splitlines():
        if "body.folded" not in riga:
            continue
        for pezzo in riga.split(","):
            if "body.folded" in pezzo:
                assert (".left" in pezzo or "#rrows" in pezzo
                        or ".rtip" in pezzo or ".rfoot" in pezzo
                        or ".rrow" in pezzo or ".rgear" in pezzo
                        or ".rfold" in pezzo or ".rtop" in pezzo
                        or ".rstack" in pezzo or ".rmini" in pezzo
                        or ".runread" in pezzo), \
                    f"regola folded fuori dalla colonna: {pezzo.strip()!r}"


def test_the_header_buttons_are_visible_not_ghosts():
    """Un comando al 55% di opacita' e' un comando che non esiste."""
    page = _page()
    # Non i keyframes (l'anello del microfono pulsa apposta): i BOTTONI.
    assert ".chathead .rmore{opacity:.55}" not in page
    assert ".chathead .rmore{opacity:1" in page
    assert "border:1px solid var(--border)" in page.split(".chathead .rmore{",1)[1][:200]


def test_refreshing_the_page_keeps_the_open_chat():
    """Aggiornare non e' uscire: la chat aperta si riapre da sola.

    Si dimentica solo quando esci TU (il tasto indietro), e un ricordo
    stantio — l'agente cancellato da un'altra scheda — si butta invece di
    riprovarlo a ogni avvio.
    """
    js = _script(_page())
    # Si salva quando apri...
    apre = js[js.index("function openChat("):]
    apre = apre[:apre.index("\nfunction ")]
    assert 'localStorage.setItem("ov.chat"' in apre
    # ...si dimentica quando esci tu...
    esce = js[js.index("function showBlank("):]
    esce = esce[:esce.index("\nfunction ")]
    assert 'localStorage.removeItem("ov.chat")' in esce
    # ...e all'avvio si riapre, con la guardia sul ricordo stantio.
    avvio = js[js.index("async function boot("):]
    assert 'localStorage.getItem("ov.chat")' in avvio
    assert "roster.some(a=>a.id===ricordata)" in avvio
    # localStorage puo' non esserci (finestra privata): mai senza try.
    assert js.count('localStorage.setItem("ov.chat"') == 1
    for pezzo in (apre, esce):
        assert "try{" in pezzo[:pezzo.index("localStorage")+40] or "try{localStorage" in pezzo


def test_no_string_escapes_outside_the_script():
    """Seconda puntata dello stesso errore: dopo «U0001f399» nel JS, «u00b7»
    nel CSS. La pagina la scrive Python, e gli escape `\\uXXXX` hanno senso
    SOLO dentro le stringhe JavaScript: il CSS li legge come una «u» seguita
    da cifre, e l'HTML nemmeno quello. Fuori dallo script, caratteri veri.
    """
    page = _page()
    fuori = re.sub(r"<script>.*?</script>", "", page, flags=re.S)
    colpe = re.findall(r".{20}\\u[0-9a-fA-F]{4}.{10}", fuori)
    assert not colpe, f"escape fuori dal JavaScript: {colpe[:3]}"
    # E i caratteri veri devono esserci al loro posto.
    assert "· dropping by" in page
    assert "···" in page


def test_a_local_ai_is_found_not_configured_by_hand():
    """Chi ha LM Studio non sa che porta usa il suo programma.

    Qualsiasi cosa esponga un server in stile OpenAI funziona gia' come
    backend (`openai_compatible`), ma nessuna pagina lo diceva e l'indirizzo
    andava indovinato. Ora openvurp bussa alle porte tipiche (LM Studio,
    llama.cpp, vLLM, Jan, koboldcpp, GPT4All) e chi risponde diventa una
    scelta da spuntare, coi suoi modelli gia' nel menu.
    """
    import json as _json
    import http.server
    import socketserver
    import threading
    import dashboard as D

    class Finto(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            corpo = _json.dumps({"data": [{"id": "qwen2.5-7b"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", 4891), Finto) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        trovati = D.DashboardHandler._server_locali()
        srv.shutdown()
    nomi = {s["name"]: s for s in trovati}
    assert "GPT4All" in nomi
    assert nomi["GPT4All"]["models"] == ["qwen2.5-7b"]
    assert nomi["GPT4All"]["url"].endswith("/v1")

    js = _script(_page())
    assert "Your local AI" in js
    # Un server trovato si sceglie: il clic compila indirizzo e modelli.
    assert "data-lsrv" in js
    assert 'OPENAI_COMPATIBLE_BASE_URL").value=srv.url' in js
    # E se non c'e' niente acceso, si spiega COME farlo comparire.
    assert "Start Server" in js
    # La scansione costa secondi (misurati: 3,1): mai dentro l'apertura della
    # pagina. Endpoint suo, la sezione si riempie quando arriva.
    assert "/api/local-servers" in js
    import inspect
    import dashboard as D
    assert "_server_locali()" not in inspect.getsource(D.DashboardHandler._settings_payload)


def test_a_file_the_agent_names_opens_beside_the_chat():
    """«Te lo mostro qui» deve mostrare.

    Dal vivo: gram prepara il giornale, dice che lo fa vedere, e non si apre
    niente da nessuna parte — il percorso nel testo dell'agente era testo
    morto. Ora i percorsi nominati diventano schede sotto il messaggio, e il
    risultato finito (PDF, pagina, immagine) si apre DA SOLO nell'anteprima
    quando il turno si chiude.
    """
    js = _script(_page())
    assert "function trovaPercorsi(" in js
    assert "prodotti.length?allegatiHTML(prodotti)" in js
    fine = js[js.index('e.kind==="assistant_end"'):]
    fine = fine[:fine.index("}else")]   # tutto il ramo, non una fetta a occhio
    assert "apriFile(belli[belli.length-1])" in fine, "il prodotto non si apre da solo"
    # E una pagina HTML si guarda impaginata, in sandbox senza permessi.
    assert '<iframe sandbox=""' in js


def test_the_agents_are_told_to_do_not_to_propose():
    """Il transcript del giornale: gram chiede a dev SE si puo' fare un PDF,
    dev risponde che l'ambiente e' read-only (falso), e all'utente arrivano
    proposte di flusso invece del giornale. La capacita' c'era; mancava che
    l'agente lo sapesse, e che gli fosse chiesto di FARE.
    """
    import tempfile
    from core.chat_store import ChatStore
    from core.swarm import Swarm

    store = ChatStore(tempfile.mkdtemp())
    store.create_agent("gram", "telegram sender", "", "", "")
    store.create_agent("dev", "programmer", "", "", "")
    sw = Swarm(parent_agent=None, store=store)
    prompt = sw._system_prompt(sw.resolve("gram"), sw.list_members())
    # Principi generali, non la cronaca dell'incidente: e il prompt deve
    # restare CORTO — un regolamento lungo fa girare il modello attorno alle
    # istruzioni invece che attorno al lavoro.
    assert "Do it and show the finished result" in prompt
    assert "irreversible" in prompt
    assert "sees it in a preview" in prompt
    assert len(prompt) < 900, f"prompt di {len(prompt)} battute: sta ringonfiando"
    assert "read-only" not in prompt and "newspaper" not in prompt, \
        "la cronaca di un incidente e' finita nel prompt"
    tools = sw._peer_tools(sw.resolve("gram"))
    ask = next(t for t in tools if t["function"]["name"] == "ask_peer")
    assert "not about what can be done" in ask["function"]["description"]


def test_the_preview_is_a_card_with_two_faces():
    """«Imita Claude, che e' bellissima»: scheda con testata e azioni a icona,
    e per una pagina l'interruttore Anteprima/Codice — le due facce dello
    stesso file, gia' pronte cosi' il passaggio e' istantaneo."""
    page = _page()
    # La scheda: bordo tondo, ombra, testata sua.
    assert 'id="pswitch"' in page and 'id="pv-render"' in page and 'id="pv-code"' in page
    css = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    assert ".prev{display:none;flex:0 0 55%" in css
    assert "border-radius:16px" in css
    js = _script(page)
    # Le due viste si preparano insieme: il toggle non aspetta la rete.
    assert "prevViste={render:" in js
    assert 'function prevVista(' in js
    # Le azioni sono icone, non parole; il copia conferma col colore.
    assert 'id="prevcopy"' in page and "<svg" in page.split('id="prevcopy"')[1][:200]
    # E ogni tipo ha la sua icona in testata.
    assert "function iconaDi(" in js


def test_the_html_divs_are_balanced_and_the_composer_stays_in_its_column():
    """Il campo di scrittura era finito A DESTRA della chat.

    Un </div> di troppo, avanzo di un innesto, chiudeva la colonna destra
    prima del tempo: il composer cadeva nella riga esterna e il browser lo
    metteva di fianco. Come per le graffe del CSS, il pareggio va contato —
    l'HTML non urla, si limita a impaginare quello che gli e' rimasto.
    """
    page = _page()
    corpo = page[page.index("<body"):] if "<body" in page else page
    corpo = re.sub(r"<script>.*?</script>", "", corpo, flags=re.S)
    aperti = len(re.findall(r"<div\b", corpo))
    chiusi = corpo.count("</div>")
    assert aperti == chiusi, f"div sbilanciati: {aperti} aperti, {chiusi} chiusi"
    # E il composer deve stare DENTRO la colonna destra, dopo la chat.
    dentro = page[page.index('id="withprev"'):page.index('<form class="composer"')]
    assert dentro.count("<div") - dentro.count("</div>") == -1, (
        "fra la chat e il composer si chiude piu' della sola withprev")


def test_the_engine_model_is_chosen_not_typed():
    """«Metti select con i modelli, non che devo scrivere io».

    Il menu del motore chiedeva il nome del modello in un campo di testo: un
    nome interno da ricordare lettera per lettera. Ora si sceglie da una
    tendina — Ollama e server locali interrogati dal vivo, gli abbonamenti da
    catalogo, piu' i nomi gia' in uso (che esistono per definizione). Scrivere
    resta possibile, ma dietro «altro…».
    """
    js = _script(_page())
    assert "async function modelliDisponibili(" in js
    assert '"/api/models"' in js
    assert 'id="popmodel-sel"' in js
    assert "predefinito del motore" in js
    assert "__altro__" in js, "manca la via d'uscita per un nome che il menu non sa"
    # Cambiando backend il popup si riapre coi modelli giusti, non si chiude.
    assert "enginePop(id,riga)" in js
    # E l'elenco non si richiede a ogni click, ma nemmeno per sempre.
    assert "MODELLI_T<120000" in js


def test_the_models_endpoint_includes_what_agents_already_use(tmp_path):
    import dashboard as D
    from core.chat_store import ChatStore
    store = ChatStore(str(tmp_path))
    store.create_agent("x", "r", "", "codex", "un-modello-mio")
    H = type("H", (D.DashboardHandler,), {"workspace_dir": str(tmp_path),
                                          "chat_store": store})
    modelli = H._modelli_per_backend()
    assert "un-modello-mio" in modelli["codex"]
    assert "gpt-5.6-luna" in modelli["codex"]
    for backend in ("ollama", "openai_compatible", "claude_cli"):
        assert backend in modelli


def test_a_dead_turn_cannot_stay_on_screen_forever():
    """«Rimane in stallo qualcosa, tool o altro, finche' non aggiorno».

    Due cause: il giro a voce non cancellava mai il suo stato dal vivo, e se
    l'evento di chiusura si perde (stream caduto, processo interrotto) nessuno
    puliva. Ora ogni evento data lo stato, e una scopa ogni 15 secondi butta i
    turni senza segni di vita da 5 minuti — un lavoro lento manda comunque la
    chiusura, un turno morto no.
    """
    js = _script(_page())
    # La scopa: intervallo, soglia, e ridisegno se guardavi proprio quella chat.
    assert "ora-(live[id].ts||0)>300000" in js
    assert "},15000);" in js
    # Ogni evento rinfresca il battito.
    assert "st.ts=Date.now();" in js
    # E il giro a voce pulisce come fa il composer.
    voce = js[js.index("async function vmInvia("):]
    voce = voce[:voce.index("\nfunction ")]
    assert "delete live[inflight]" in voce, "il giro a voce lascia lo stallo"


def test_the_folded_sidebar_scrolls_without_a_visible_scrollbar():
    """La barra di sistema dentro 76px schiacciava i polpi."""
    page = _page()
    assert "body.folded #rrows{scrollbar-width:none" in page
    assert "body.folded #rrows::-webkit-scrollbar{display:none}" in page
    # Da aperta invece la barra c'e', ma sottile e nei nostri colori.
    assert "scrollbar-width:thin" in page
    assert "#rrows::-webkit-scrollbar{width:7px}" in page


def test_the_model_menus_wear_the_site_style_and_stay_per_backend():
    """La select del popup era quella di sistema (bianca, fuori tema), e il
    menu del modello nelle impostazioni mescolava Ollama con nomi GPT e Claude
    fissi, qualunque fosse il backend. Un modello ha senso solo nel SUO motore.
    """
    page = _page()
    assert ".popfield select{width:100%;background:var(--bg)" in page
    js = _script(page)
    # Niente piu' lista fissa cucita nel client.
    assert '"gpt-5.6-luna","gpt-5.6-terra","claude-opus-5"' not in js
    # Il menu segue il backend, e cambia quando lo cambi.
    assert "function riempiModelli(" in js
    assert '$("#s-backend").onchange=riempiModelli' in js
    # Il valore gia' impostato resta in lista: toglierlo lo cancellerebbe.
    assert "lista.unshift(attuale)" in js
