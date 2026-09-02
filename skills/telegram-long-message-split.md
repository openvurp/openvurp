---
id: official/telegram-long-message-split
type: solution
title: "Telegram replies arrive truncated"
description: "Long answers were cut at Telegram's 4096-char limit, and in groups a streaming edit effect got rate-limited and left messages stuck mid-text. Fix: split into parts, no edit-spam in groups."
tags: [telegram, message, truncated, split, 4096, group, messaggio, tagliato, gruppo]
openvurp_version: ">=4.0"
trust: official
capabilities:
  shell: false
  file_read: false
  file_write: false
  network: []
provenance:
  author: openvurp
  contributed: 2026-06-30
  reviewed_by: openvurp-maintainers
  review_date: 2026-06-30
verified: true
---

## Problem

Replies on Telegram arrived cut off — sometimes mid-sentence, sometimes stuck on
a short fragment ending in " ..." — especially in group chats.

## Context

Two causes. First, the send path truncated text to Telegram's 4096-char limit
instead of splitting it, so anything longer was lost. Second, the "streaming"
effect edited one message many times; Telegram rate-limits edits much harder in
groups, the failures were swallowed, and the final full text never landed.

## Approach

Stop truncating: split long text into parts on line/word boundaries and send
them as separate messages. Stop the edit-spam where it breaks: skip the
progressive-edit effect in groups (and on multi-part messages) and just send the
full content; keep streaming only in direct chats, with a guaranteed final send.

## Solution

- A splitter that breaks text into chunks under the limit (with a margin),
  preferring newline/space boundaries so it never cuts mid-word.
- In group chats: no edit-streaming — send the parts directly and completely.
- In direct chats: keep a light streaming effect but always finalize with the
  full text; if the final edit fails, send the complete text as a new message.

## How to verify

Send a reply well over 4096 chars in a group: it arrives whole, split across
messages, with no " ..." fragment left behind and nothing lost.
