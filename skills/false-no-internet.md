---
id: official/false-no-internet
type: solution
title: "Agent wrongly believes there is no internet"
description: "A one-time, single-host connectivity check froze a false 'no internet' into the agent's context, making it refuse web tools even though the network works. Fix: robust live probe."
tags: [internet, network, connectivity, capabilities, web, rete, connettivita, offline]
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

The agent kept saying it had no internet and refused web search/fetch, even
though git, package installs and other HTTPS worked fine.

## Context

The connectivity check ran only once (at first run / birth), used a single
fragile endpoint over plain HTTP, and depended on an external command. When that
one host was slow or blocked, the result was a false "no internet" — and it was
then frozen into the agent's saved environment and reused on every later turn.

## Approach

Two root issues: the probe was fragile (one host, HTTP, external command), and
it was static (checked once, never refreshed). Connectivity is dynamic, so it
must be re-checked live, not trusted from an old snapshot.

## Solution

- Replace the single-host check with a TCP/443 probe to several reliable targets
  (e.g. 1.1.1.1, 8.8.8.8, plus a couple of well-known hostnames). Online = any
  one of them connects. No external command, no DNS dependency for the IP ones.
- Make it **live per turn** but cached behind the environment snapshot's TTL, so
  it reflects reality without probing on every single message.
- Default to "online" when uncertain, so a flaky probe never blocks web tools.

## How to verify

Disconnect: the agent reports offline. Reconnect: within the snapshot TTL it
reports online again and uses web tools without complaint. A successful git/HTTPS
operation should never coexist with an "internet: no" status.
