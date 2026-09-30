---
name: proposal-review-gate
description: No bulk firmware/host code until the user approves docs/PROPOSAL.md (layout, protocol, timing); open decisions listed there
metadata:
  type: project
---

On 2026-09-30 the user asked for the repo layout, USB protocol byte format and default timing values to be proposed
for review **before** any code is written. The proposal is in `docs/PROPOSAL.md`, and §6 lists the open decisions
(seq field, end frame, READ_STATUS dummy 00h, continue-on-timeout, BUS_TEST readback, VID:PID).

**Why:** SPEC "Working style: plan first". The user reviews the design choices themselves.
**How to apply:** Check whether the user has approved or changed the proposal before starting M0 code. Once it is
approved, move the protocol into `docs/PROTOCOL.md` and update this memory. See [[datasheet-findings]].
