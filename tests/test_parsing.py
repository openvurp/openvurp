"""Test per il parsing dei tool calls (regex fallback per Ollama).

Copre TUTTE le varianti di formato che i modelli Ollama producono.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class MockUI:
    """UI mock per test."""
    def start_spinner(self, *a): pass
    def stop_spinner(self): pass
    def start_response(self): pass
    def end_response(self): pass
    def stream_text(self, t): pass
    def status(self, t): pass
    def show_cmd(self, c): pass
    def show_output(self, o, **kw): pass
    def error(self, t): pass
    def openvurp_say(self, t): pass
    def confirm(self, t): return False
    def prompt(self): return ""
    def welcome(self, **kw): pass
    def goodbye(self): pass
    def show_memory_table(self): pass
    def show_skills_table(self): pass
    def show_self_panel(self): pass
    def show_trace(self, t): pass
    def show_evolve(self): pass


def get_agent():
    """Crea agent mock per test di parsing."""
    from core.agent import Agent
    return Agent(MockUI())


# ══════════════════════════════════════════════════════════
#  FORMATO STANDARD: ```TOOL:name\n{json}\n```
# ══════════════════════════════════════════════════════════

def test_standard_tool():
    """```TOOL:read_file\n{"path": "..."}\n```"""
    agent = get_agent()
    calls, text = agent._parse_response('```TOOL:read_file\n{"path": "/tmp/test.txt"}\n```')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"
    assert calls[0][1]["path"] == "/tmp/test.txt"


def test_standard_shell():
    """```SHELL\nls -la\n```"""
    agent = get_agent()
    calls, text = agent._parse_response('Eseguo:\n```SHELL\nls -la\n```\nFatto.')
    assert len(calls) == 1
    assert calls[0][0] == "shell"
    assert calls[0][1]["command"] == "ls -la"
    assert "Fatto." in text


def test_standard_multiple():
    """Più tool nella stessa risposta."""
    agent = get_agent()
    response = (
        'Leggo:\n'
        '```TOOL:read_file\n{"path": "/a.txt"}\n```\n'
        'Scrivo:\n'
        '```TOOL:write_file\n{"path": "/b.txt", "content": "hello"}\n```'
    )
    calls, text = agent._parse_response(response)
    assert len(calls) == 2
    assert calls[0][0] == "read_file"
    assert calls[1][0] == "write_file"
    assert calls[1][1]["content"] == "hello"


# ══════════════════════════════════════════════════════════
#  VARIANTE: lowercase tool:/Tool: e spazi
# ══════════════════════════════════════════════════════════

def test_lowercase_tool_prefix():
    """```tool:read_file\n{...}\n```"""
    agent = get_agent()
    calls, _ = agent._parse_response('```tool:read_file\n{"path": "/tmp/x"}\n```')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"
    assert calls[0][1]["path"] == "/tmp/x"


def test_mixed_case_tool_prefix():
    """```Tool:read_file\n{...}\n```"""
    agent = get_agent()
    calls, _ = agent._parse_response('```Tool:read_file\n{"path": "/tmp/x"}\n```')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


def test_tool_prefix_with_space():
    """```TOOL: read_file\n{...}\n```"""
    agent = get_agent()
    calls, _ = agent._parse_response('```TOOL: read_file\n{"path": "/tmp/x"}\n```')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


def test_tool_prefix_space_lowercase():
    """```tool: read_file\n{...}\n```"""
    agent = get_agent()
    calls, _ = agent._parse_response('```tool: read_file\n{"path": "/a"}\n```')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


# ══════════════════════════════════════════════════════════
#  VARIANTE: senza prefisso TOOL: — solo nome dentro backtick
# ══════════════════════════════════════════════════════════

def test_backtick_name_only():
    """```read_file\n{"path": "..."}\n``` — senza TOOL:"""
    agent = get_agent()
    calls, _ = agent._parse_response('```read_file\n{"path": "/tmp/x"}\n```')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"
    assert calls[0][1]["path"] == "/tmp/x"


def test_backtick_write_file_no_prefix():
    """```write_file\n{...}\n```"""
    agent = get_agent()
    calls, _ = agent._parse_response('```write_file\n{"path": "/a", "content": "test"}\n```')
    assert len(calls) == 1
    assert calls[0][0] == "write_file"
    assert calls[0][1]["content"] == "test"


def test_backtick_web_search_no_prefix():
    """```web_search\n{...}\n```"""
    agent = get_agent()
    calls, _ = agent._parse_response('Cerco:\n```web_search\n{"query": "python"}\n```')
    assert len(calls) == 1
    assert calls[0][0] == "web_search"


def test_backtick_shell_lowercase():
    """```shell\nls\n```"""
    agent = get_agent()
    calls, _ = agent._parse_response('```shell\nls -la /home\n```')
    assert len(calls) == 1
    assert calls[0][0] == "shell"
    assert calls[0][1]["command"] == "ls -la /home"


def test_backtick_bash():
    """```bash\nls\n```"""
    agent = get_agent()
    calls, _ = agent._parse_response('```bash\npwd\n```')
    assert len(calls) == 1
    assert calls[0][0] == "shell"
    assert calls[0][1]["command"] == "pwd"


def test_backtick_sh():
    """```sh\necho hello\n```"""
    agent = get_agent()
    calls, _ = agent._parse_response('```sh\necho hello\n```')
    assert len(calls) == 1
    assert calls[0][0] == "shell"


# ══════════════════════════════════════════════════════════
#  VARIANTE: senza backtick — TOOL:name\n{json}
# ══════════════════════════════════════════════════════════

def test_plain_tool_no_backtick():
    """TOOL:read_file\n{"path": "..."}"""
    agent = get_agent()
    calls, _ = agent._parse_response('TOOL:read_file\n{"path": "/tmp/x"}')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


def test_plain_tool_lowercase_no_backtick():
    """tool:read_file\n{"path": "..."}"""
    agent = get_agent()
    calls, _ = agent._parse_response('tool:read_file\n{"path": "/tmp/x"}')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


def test_plain_tool_with_text_before():
    """Testo prima, poi TOOL:name\n{json}"""
    agent = get_agent()
    calls, text = agent._parse_response('Leggo il file:\nTOOL:read_file\n{"path": "/a.txt"}')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"
    assert "Leggo" in text


def test_plain_name_only_no_backtick():
    """read_file\n{"path": "..."} — solo nome tool senza TOOL: e senza backtick"""
    agent = get_agent()
    calls, _ = agent._parse_response('read_file\n{"path": "/tmp/x"}')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


# ══════════════════════════════════════════════════════════
#  VARIANTE: case insensitive tool name
# ══════════════════════════════════════════════════════════

def test_tool_name_uppercase():
    """```READ_FILE\n{...}\n```"""
    agent = get_agent()
    calls, _ = agent._parse_response('```READ_FILE\n{"path": "/tmp/x"}\n```')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


def test_media_tools_are_registered():
    agent = get_agent()
    names = set(agent.tools.names())
    assert "image_analyze" in names
    assert "audio_transcribe" in names
    assert "pdf_read" in names


def test_tool_name_mixed_case():
    """```Read_File\n{...}\n```"""
    agent = get_agent()
    calls, _ = agent._parse_response('```Read_File\n{"path": "/tmp/x"}\n```')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


# ══════════════════════════════════════════════════════════
#  BLOCCHI CODICE NORMALI (non tool) — NON devono matchare
# ══════════════════════════════════════════════════════════

def test_code_block_python_not_tool():
    """```python\nprint('hello')\n``` NON è un tool"""
    agent = get_agent()
    calls, text = agent._parse_response('Ecco il codice:\n```python\nprint("hello")\n```')
    assert len(calls) == 0
    assert "print" in text


def test_code_block_json_not_tool():
    """```json\n{"key": "value"}\n``` NON è un tool"""
    agent = get_agent()
    calls, text = agent._parse_response('Il JSON è:\n```json\n{"key": "value"}\n```')
    assert len(calls) == 0
    assert "key" in text


def test_code_block_javascript_not_tool():
    """```javascript\nconst x = 1;\n``` NON è un tool"""
    agent = get_agent()
    calls, text = agent._parse_response('```javascript\nconst x = 1;\n```')
    assert len(calls) == 0


# ══════════════════════════════════════════════════════════
#  NESSUNA TOOL CALL
# ══════════════════════════════════════════════════════════

def test_no_tools_plain_text():
    """Risposta normale senza tool."""
    agent = get_agent()
    calls, text = agent._parse_response("Ciao! Come posso aiutarti?")
    assert len(calls) == 0
    assert "Ciao" in text


def test_no_tools_with_colon():
    """Testo con due punti non è una tool call."""
    agent = get_agent()
    calls, text = agent._parse_response("Ecco cosa fare: leggi il file e modificalo.")
    assert len(calls) == 0


def test_empty_response():
    """Risposta vuota."""
    agent = get_agent()
    calls, text = agent._parse_response("")
    assert len(calls) == 0


# ══════════════════════════════════════════════════════════
#  JSON ROBUSTO — malformato, incompleto, rotto
# ══════════════════════════════════════════════════════════

def test_json_trailing_comma():
    """{"path": "/tmp/x", } — virgola finale"""
    agent = get_agent()
    args = agent._parse_tool_json('{"path": "/tmp/test.txt", }', "read_file")
    assert args.get("path") == "/tmp/test.txt"


def test_json_unclosed():
    """{"path": "/tmp/x" — senza }"""
    agent = get_agent()
    args = agent._parse_tool_json('{"path": "/tmp/test.txt"', "read_file")
    assert args.get("path") == "/tmp/test.txt"


def test_json_valid():
    """JSON perfettamente valido."""
    agent = get_agent()
    args = agent._parse_tool_json('{"query": "python", "max_results": 5}', "web_search")
    assert args["query"] == "python"
    assert args["max_results"] == 5


def test_json_broken():
    """Testo completamente rotto — fallback."""
    agent = get_agent()
    args = agent._parse_tool_json('not json at all', "read_file")
    assert "input" in args


def test_json_multiline():
    """JSON su più righe."""
    agent = get_agent()
    json_str = '{\n  "path": "/tmp/test.txt",\n  "content": "hello world"\n}'
    args = agent._parse_tool_json(json_str, "write_file")
    assert args["path"] == "/tmp/test.txt"
    assert args["content"] == "hello world"


# ══════════════════════════════════════════════════════════
#  SCENARI REALI — output copiati da Ollama veri
# ══════════════════════════════════════════════════════════

def test_real_ollama_read_file():
    """Scenario reale: modello che mette solo il nome senza TOOL:"""
    agent = get_agent()
    response = (
        "Leggo il file di configurazione:\n\n"
        "```read_file\n"
        '{"path": "C:\\\\Users\\\\alice\\\\Desktop\\\\openvurp\\\\config.py"}\n'
        "```\n"
    )
    calls, text = agent._parse_response(response)
    assert len(calls) == 1
    assert calls[0][0] == "read_file"
    assert "config.py" in calls[0][1]["path"]


def test_real_ollama_multiple_reads():
    """Scenario reale: modello che fa 3 read_file di fila."""
    agent = get_agent()
    response = (
        "```read_file\n"
        '{"path": "C:\\\\Users\\\\alice\\\\Desktop\\\\openvurp\\\\config.py"}\n'
        "```\n\n"
        "```read_file\n"
        '{"path": "C:\\\\Users\\\\alice\\\\Desktop\\\\openvurp\\\\IDENTITY.md"}\n'
        "```\n\n"
        "```read_file\n"
        '{"path": "C:\\\\Users\\\\alice\\\\Desktop\\\\openvurp\\\\USER.md"}\n'
        "```\n"
    )
    calls, text = agent._parse_response(response)
    assert len(calls) == 3
    assert all(c[0] == "read_file" for c in calls)
    assert "config.py" in calls[0][1]["path"]
    assert "IDENTITY.md" in calls[1][1]["path"]
    assert "USER.md" in calls[2][1]["path"]


def test_real_ollama_shell_bash():
    """Scenario reale: modello che usa ```bash per comandi."""
    agent = get_agent()
    response = (
        "Controllo la versione di Python:\n\n"
        "```bash\n"
        "python --version\n"
        "```\n"
    )
    calls, text = agent._parse_response(response)
    assert len(calls) == 1
    assert calls[0][0] == "shell"
    assert "python --version" in calls[0][1]["command"]


def test_real_ollama_mixed_text_and_tools():
    """Scenario reale: testo, tool, altro testo, altro tool."""
    agent = get_agent()
    response = (
        "Perfetto, prima leggo il file:\n\n"
        "```TOOL:read_file\n"
        '{"path": "/home/test.py"}\n'
        "```\n\n"
        "Ora eseguo il test:\n\n"
        "```bash\n"
        "python -m pytest tests/\n"
        "```\n"
    )
    calls, text = agent._parse_response(response)
    assert len(calls) == 2
    assert calls[0][0] == "read_file"
    assert calls[1][0] == "shell"
    assert "pytest" in calls[1][1]["command"]


def test_real_ollama_write_file_multiline_content():
    """Scenario reale: write_file con content multilinea."""
    agent = get_agent()
    response = (
        '```TOOL:write_file\n'
        '{"path": "/tmp/hello.py", "content": "print(\'hello\')\\nprint(\'world\')"}\n'
        '```\n'
    )
    calls, text = agent._parse_response(response)
    assert len(calls) == 1
    assert calls[0][0] == "write_file"
    assert calls[0][1]["path"] == "/tmp/hello.py"


def test_real_ollama_speak_tool():
    """Scenario reale: tool speak con testo italiano."""
    import config

    original_voice = getattr(config, "VOICE_ENABLED", False)
    original_voice_tools = getattr(config, "VOICE_TOOLS_ENABLED", False)
    config.VOICE_ENABLED = True
    config.VOICE_TOOLS_ENABLED = True
    try:
        agent = get_agent()
    finally:
        config.VOICE_ENABLED = original_voice
        config.VOICE_TOOLS_ENABLED = original_voice_tools
    response = (
        "```TOOL:speak\n"
        '{"text": "Ciao, sono openvurp!"}\n'
        "```\n"
    )
    calls, _ = agent._parse_response(response)
    assert len(calls) == 1
    assert calls[0][0] == "speak"
    assert "openvurp" in calls[0][1]["text"]


def test_real_ollama_plain_tool_in_paragraph():
    """Scenario reale: TOOL:name inline senza backtick."""
    agent = get_agent()
    response = (
        "Controllo il contenuto:\n"
        "TOOL:read_file\n"
        '{"path": "/etc/hostname"}\n'
        "Dopo ti dico cosa c'è."
    )
    calls, text = agent._parse_response(response)
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


def test_real_ollama_edit_file():
    """Scenario reale: edit_file tool."""
    agent = get_agent()
    response = (
        '```edit_file\n'
        '{"path": "/tmp/test.py", "old_text": "foo", "new_text": "bar"}\n'
        '```\n'
    )
    calls, _ = agent._parse_response(response)
    assert len(calls) == 1
    assert calls[0][0] == "edit_file"
    assert calls[0][1]["old_text"] == "foo"
    assert calls[0][1]["new_text"] == "bar"


# ══════════════════════════════════════════════════════════
#  EDGE CASES
# ══════════════════════════════════════════════════════════

def test_tool_with_extra_whitespace():
    """Spazi extra intorno al nome."""
    agent = get_agent()
    calls, _ = agent._parse_response('```  TOOL:read_file  \n{"path": "/tmp/x"}\n```')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


def test_tool_with_newline_before_json():
    """Newline extra tra nome e JSON."""
    agent = get_agent()
    calls, _ = agent._parse_response('```TOOL:read_file\n\n{"path": "/tmp/x"}\n```')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


def test_multiline_shell_command():
    """Comando shell su più righe."""
    agent = get_agent()
    calls, _ = agent._parse_response('```SHELL\ncd /tmp\nls -la\npwd\n```')
    assert len(calls) == 1
    assert "cd /tmp" in calls[0][1]["command"]
    assert "ls -la" in calls[0][1]["command"]


def test_tool_not_invented():
    """Tool inventato dal modello — NON deve essere eseguito."""
    agent = get_agent()
    calls, text = agent._parse_response('```TOOL:invent_something\n{"x": 1}\n```')
    # Il tool non esiste, dovrebbe essere trattato come testo o ignorato
    for name, _ in calls:
        assert name != "invent_something"


def test_backtick_unknown_language_not_tool():
    """```yaml\nkey: value\n``` — linguaggio sconosciuto, non un tool."""
    agent = get_agent()
    calls, _ = agent._parse_response('```yaml\nkey: value\n```')
    assert len(calls) == 0


# ══════════════════════════════════════════════════════════
#  BACKTICK NON CHIUSI (Ollama spesso non chiude ```)
# ══════════════════════════════════════════════════════════

def test_unclosed_backtick_tool():
    """```TOOL:read_file\n{...}\n  — senza ``` di chiusura"""
    agent = get_agent()
    calls, _ = agent._parse_response('```TOOL:read_file\n{"path": "/tmp/x"}\n')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


def test_unclosed_backtick_name_only():
    """```read_file\n{...}  — senza chiusura e senza TOOL:"""
    agent = get_agent()
    calls, _ = agent._parse_response('```read_file\n{"path": "/tmp/x"}')
    assert len(calls) == 1
    assert calls[0][0] == "read_file"


def test_unclosed_backtick_bash():
    """```bash\nls -la\n  — shell non chiuso"""
    agent = get_agent()
    calls, _ = agent._parse_response('```bash\nls -la\n')
    assert len(calls) == 1
    assert calls[0][0] == "shell"


def test_unclosed_backtick_with_text():
    """Testo prima, poi tool non chiuso."""
    agent = get_agent()
    calls, text = agent._parse_response('Ecco il file:\n```read_file\n{"path": "/a.txt"}\n')
    assert len(calls) == 1
    assert "Ecco" in text


def test_unclosed_backtick_python_not_tool():
    """```python\nprint(1)\n  — non è un tool anche senza chiusura."""
    agent = get_agent()
    calls, _ = agent._parse_response('```python\nprint(1)\n')
    assert len(calls) == 0


def test_unclosed_multiple_last():
    """Primo chiuso, ultimo non chiuso."""
    agent = get_agent()
    response = '```read_file\n{"path": "/a"}\n```\nPoi:\n```write_file\n{"path": "/b", "content": "x"}\n'
    calls, _ = agent._parse_response(response)
    assert len(calls) == 2


# ══════════════════════════════════════════════════════════
#  RUNNER
# ══════════════════════════════════════════════════════════

ALL_TESTS = [
    # Standard
    test_standard_tool,
    test_standard_shell,
    test_standard_multiple,
    # Lowercase/spazi
    test_lowercase_tool_prefix,
    test_mixed_case_tool_prefix,
    test_tool_prefix_with_space,
    test_tool_prefix_space_lowercase,
    # Solo nome dentro backtick
    test_backtick_name_only,
    test_backtick_write_file_no_prefix,
    test_backtick_web_search_no_prefix,
    test_backtick_shell_lowercase,
    test_backtick_bash,
    test_backtick_sh,
    # Senza backtick
    test_plain_tool_no_backtick,
    test_plain_tool_lowercase_no_backtick,
    test_plain_tool_with_text_before,
    test_plain_name_only_no_backtick,
    # Case insensitive nome tool
    test_tool_name_uppercase,
    test_tool_name_mixed_case,
    # Non-tool code blocks
    test_code_block_python_not_tool,
    test_code_block_json_not_tool,
    test_code_block_javascript_not_tool,
    # Nessuna tool call
    test_no_tools_plain_text,
    test_no_tools_with_colon,
    test_empty_response,
    # JSON robusto
    test_json_trailing_comma,
    test_json_unclosed,
    test_json_valid,
    test_json_broken,
    test_json_multiline,
    # Scenari reali Ollama
    test_real_ollama_read_file,
    test_real_ollama_multiple_reads,
    test_real_ollama_shell_bash,
    test_real_ollama_mixed_text_and_tools,
    test_real_ollama_write_file_multiline_content,
    test_real_ollama_speak_tool,
    test_real_ollama_plain_tool_in_paragraph,
    test_real_ollama_edit_file,
    # Edge cases
    test_tool_with_extra_whitespace,
    test_tool_with_newline_before_json,
    test_multiline_shell_command,
    test_tool_not_invented,
    test_backtick_unknown_language_not_tool,
    # Backtick non chiusi
    test_unclosed_backtick_tool,
    test_unclosed_backtick_name_only,
    test_unclosed_backtick_bash,
    test_unclosed_backtick_with_text,
    test_unclosed_backtick_python_not_tool,
    test_unclosed_multiple_last,
]

if __name__ == "__main__":
    passed = 0
    failed = 0
    for test in ALL_TESTS:
        try:
            test()
            passed += 1
            print(f"  OK  {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {test.__name__}: {e}")

    print(f"\n  {passed}/{passed + failed} test passati")
    if failed:
        print(f"  {failed} FALLITI")
        sys.exit(1)
    else:
        print("  Tutti i test passati!")
