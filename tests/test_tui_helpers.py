import unittest
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from TUI import (
    ModalItem,
    choose_waiting_phrase,
    compute_slash_suggestions,
    decode_paste_codes,
    format_tokens_compact,
    format_runtime_estimates,
    format_gateway_summary,
    layout_editor_text,
    markdown_to_tui_lines,
    normalize_status_event,
    pick_waiting_phrase,
    score_modal_item,
)


class TuiHelperTests(unittest.TestCase):
    def test_pick_waiting_phrase_cycles(self):
        self.assertEqual(pick_waiting_phrase(0), "flibbertigibbeting")
        self.assertEqual(pick_waiting_phrase(10), "kerfuffling")

    def test_choose_waiting_phrase_keeps_existing_value(self):
        self.assertEqual(choose_waiting_phrase("moonwalking"), "moonwalking")

    def test_decode_paste_codes_multiline(self):
        # "ab\ncd" con CRLF deve diventare un solo newline
        codes = [ord("a"), ord("b"), 13, 10, ord("c"), ord("d")]
        self.assertEqual(decode_paste_codes(codes), "ab\ncd")

    def test_decode_paste_codes_lf_only_and_tab(self):
        codes = [ord("x"), 10, 9, ord("y")]
        self.assertEqual(decode_paste_codes(codes), "x\n\ty")

    def test_decode_paste_codes_drops_control_chars(self):
        codes = [ord("a"), 1, 7, ord("b")]  # SOH e BEL ignorati
        self.assertEqual(decode_paste_codes(codes), "ab")

    def test_score_modal_item_prefers_label_match(self):
        item = ModalItem(
            value="x",
            label="glm-5.1:cloud",
            description="current model",
            search_text="ollama cloud current",
        )
        score = score_modal_item(item, "glm")
        self.assertIsNotNone(score)
        self.assertEqual(score[0], 0)

    def test_score_modal_item_returns_none_when_not_matchable(self):
        item = ModalItem(value="x", label="alpha", description="beta", search_text="gamma")
        self.assertIsNone(score_modal_item(item, "zzz"))

    def test_compute_slash_suggestions_filters_live(self):
        items = compute_slash_suggestions("/mo")
        labels = [item.label for item in items]
        self.assertIn("/model", labels)
        self.assertIn("/models", labels)
        self.assertNotIn("/doctor", labels)

    def test_compute_slash_suggestions_includes_toggle_commands(self):
        items = compute_slash_suggestions("/re")
        labels = [item.label for item in items]
        self.assertIn("/reasoning", labels)
        self.assertIn("/reset", labels)
        self.assertLess(labels.index("/reasoning"), labels.index("/model"))

    def test_compute_slash_suggestions_includes_usage(self):
        items = compute_slash_suggestions("/us")
        labels = [item.label for item in items]
        self.assertIn("/usage", labels)

    def test_layout_editor_text_wraps_and_tracks_cursor(self):
        lines, row, col = layout_editor_text("abc\ndefgh", cursor_pos=8, width=3)
        self.assertEqual(lines, ["abc", "def", "gh"])
        self.assertEqual((row, col), (2, 1))

    def test_format_runtime_estimates_marks_values_as_estimates(self):
        summary, footer = format_runtime_estimates(
            {"tokens_total": 1200, "llm_calls": 4, "turns": 3, "tool_calls": 2},
            {"total_tokens": 800, "budget_tokens": 128000},
            12,
        )
        self.assertIn("ctx stima 12%", summary)
        self.assertIn("session tok~ 1200", summary)
        self.assertIn("ctx tok~ 800/128000", footer)

    def test_normalize_status_event_classifies_waiting_statuses(self):
        text, kind = normalize_status_event("[Elaboro i risultati...]")
        self.assertEqual(text, "Elaboro i risultati...")
        self.assertEqual(kind, "thinking")

    def test_format_tokens_compact_matches_compact_style(self):
        self.assertEqual(format_tokens_compact(1530, 128000), "tokens ~1.5k/128k (1%)")

    def test_format_gateway_summary_online(self):
        line = format_gateway_summary({"ok": True, "host": "127.0.0.1", "port": 8421, "payload": {"sessions": 3, "event_count_estimate": 9}})
        self.assertIn("gateway online", line)
        self.assertIn("snapshots 3", line)

    def test_format_gateway_summary_offline(self):
        line = format_gateway_summary({"ok": False, "host": "127.0.0.1", "port": 8421, "error": "refused"})
        self.assertIn("gateway offline", line)
        self.assertIn("refused", line)

    def test_markdown_to_tui_lines_renders_common_blocks_without_markers(self):
        source = (
            "# Titolo\n\n**Mint Cucina Fresca** e `codice`\n\n"
            "- uno\n- due\n\n```python\nprint('ok')\n```"
        )
        lines = markdown_to_tui_lines(source, 80)
        text = "\n".join(line for line, _style in lines)
        self.assertIn("Mint Cucina Fresca", text)
        self.assertIn("• uno", text)
        self.assertIn("code · python", text)
        self.assertIn("print('ok')", text)
        self.assertNotIn("**", text)
        self.assertNotIn("```", text)


if __name__ == "__main__":
    unittest.main()
