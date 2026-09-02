"""Test per il modulo context — pruning, truncation, budget."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.context import (
    ContextManager, truncate_tool_result, estimate_tokens,
    SOFT_TRIM_MAX_CHARS, HARD_CLEAR_PLACEHOLDER
)


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("ciao") == 1  # 4 chars / 4
    assert estimate_tokens("a" * 100) == 25


def test_truncate_tool_result_small():
    """Output piccoli non vengono troncati."""
    text = "output breve"
    result = truncate_tool_result(text, 128000)
    assert result == text


def test_truncate_tool_result_large():
    """Output grandi vengono troncati con head + tail."""
    big = "x" * 200000
    result = truncate_tool_result(big, 10000)  # 10K token = ~40K chars max (30%)
    assert len(result) < len(big)
    assert "omessi" in result or "caratteri" in result


def test_soft_trim():
    """Soft trim riduce tool output grandi."""
    cm = ContextManager(".", "memory", "skills", max_tokens=2000)

    msgs = [
        {"role": "system", "content": "sys" * 500},
        {"role": "user", "content": "Output dei comandi:\n\n" + "x" * 10000},
        {"role": "assistant", "content": "ok" * 500},
    ]

    pruned = cm.prune_messages(msgs)
    tool_msg = [m for m in pruned if "Output" in m.get("content", "") or "omessi" in m.get("content", "")]
    # Il soft trim deve aver ridotto il tool output (o il hard clear l'ha sostituito)
    total_before = sum(len(m["content"]) for m in msgs)
    total_after = sum(len(m["content"]) for m in pruned)
    assert total_after < total_before


def test_hard_clear():
    """Hard clear sostituisce tool output vecchi con placeholder."""
    cm = ContextManager(".", "memory", "skills", max_tokens=5000)

    msgs = [{"role": "system", "content": "sys"}]
    # Aggiungi molti tool output grandi (totale prunable > 50K)
    for i in range(10):
        msgs.append({"role": "user", "content": f"domanda {i}"})
        msgs.append({"role": "user", "content": "Output dei comandi:\n\n" + "x" * 8000})
        msgs.append({"role": "assistant", "content": f"risposta {i}"})

    pruned = cm.prune_messages(msgs)
    # Il pruning deve aver ridotto significativamente
    total_before = sum(len(m["content"]) for m in msgs)
    total_after = sum(len(m["content"]) for m in pruned)
    assert total_after < total_before * 0.5, f"Riduzione insufficiente: {total_after}/{total_before}"


def test_budget_check():
    """Budget check restituisce info corrette."""
    cm = ContextManager(".", "memory", "skills", max_tokens=10000)

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "ciao"},
    ]

    budget = cm.check_budget(msgs)
    assert budget["ratio"] < 0.1
    assert not budget["over_budget"]
    assert not budget["needs_compaction"]


def test_budget_includes_tool_schema():
    cm = ContextManager(".", "memory", "skills", max_tokens=1000)
    schema = [{"type": "function", "function": {"name": "large", "description": "x" * 8000}}]
    budget = cm.check_budget([{"role": "user", "content": "ciao"}], schema)
    assert budget["tool_schema_tokens"] >= 1900
    assert budget["over_budget"]


def test_prune_to_economic_target_keeps_recent_turns():
    cm = ContextManager(".", "memory", "skills", max_tokens=64000)
    messages = [{"role": "system", "content": "s" * 4000}]
    for index in range(20):
        messages.append({"role": "user", "content": f"domanda {index} " + "x" * 1200})
        messages.append({"role": "assistant", "content": f"risposta {index} " + "y" * 1200})
    pruned = cm.prune_to_target(messages, target_tokens=3000)
    assert len(pruned) < len(messages)
    assert any("risposta 19" in m.get("content", "") for m in pruned)
    assert sum(len(m.get("content", "")) for m in pruned) <= 12000


def test_overflow_detection():
    """Rileva errori di context overflow."""
    assert ContextManager.is_context_overflow_error(
        Exception("context length exceeded"))
    assert ContextManager.is_context_overflow_error(
        Exception("prompt is too long: 200000 tokens"))
    assert not ContextManager.is_context_overflow_error(
        Exception("connection timeout"))
    assert not ContextManager.is_context_overflow_error(
        Exception("rate limit exceeded"))


def test_compaction_prompt():
    """build_compaction_prompt genera messaggi validi."""
    cm = ContextManager(".", "memory", "skills")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "ciao"},
        {"role": "assistant", "content": "ehi"},
    ]
    compact_msgs = cm.build_compaction_prompt(msgs)
    assert len(compact_msgs) == 2
    assert compact_msgs[0]["role"] == "system"
    assert "riassumi" in compact_msgs[0]["content"].lower() or \
           "riassunto" in compact_msgs[0]["content"].lower()


def test_system_prompt_can_include_environment_section():
    cm = ContextManager(".", "memory", "skills")
    prompt = cm.build_system_prompt(
        bootstrap_context="## BOOTSTRAP\ncontesto",
        memory_text="",
        tools_section="## TOOLS\n...",
        environment_text="## DOVE VIVO\n- shell attiva: `bash`",
        method_text="## METODO OPERATIVO\n- usa `find_files` prima di `read_file`",
    )
    assert "## DOVE VIVO" in prompt
    assert "`bash`" in prompt
    assert "## METODO OPERATIVO" in prompt
    assert "`find_files`" in prompt
    assert "## SELF-KNOWLEDGE" in prompt
    assert "## IL MIO CODICE" not in prompt


def test_format_instructions_delegate_to_generated_tool_schema():
    cm = ContextManager(".", "memory", "skills")
    text = cm._format_instructions()
    assert "## TOOL DISPONIBILI" in text
    assert "```TOOL:glob" not in text
    assert "`offset`" not in text


if __name__ == "__main__":
    test_estimate_tokens()
    test_truncate_tool_result_small()
    test_truncate_tool_result_large()
    test_soft_trim()
    test_hard_clear()
    test_budget_check()
    test_overflow_detection()
    test_compaction_prompt()
    test_system_prompt_can_include_environment_section()
    test_format_instructions_delegate_to_generated_tool_schema()
    print("Tutti i test context passati!")
