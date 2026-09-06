# `operator-state-2026-07-31-p533.md`

The doc `S-almdudleer-operator-p640` actually booted from on 2026-09-06 — the
five-week-stale specimen T-0942 exists because of. Captured by p640's own tool
log immediately before its first write, and byte-matched against the live
file's size at that moment (55832 bytes, sha256 `72e9636144855570ce9683e4…`).
The full specimen lives in the ticket evidence dir on the install
(`data/bot-squad/artifacts/evidence/T-0942/`); what is checked in here is its
verbatim frontmatter + header + the seven `## ` headings, with the section
bodies elided to keep the fixture readable.

**Do not "fix" the seven sections down to five.** The schema
(`assignment.OPERATOR_STATE_SECTIONS`) is five and this file has seven, and
that mismatch is part of the specimen: the doc's own header asserts the closed
list of five while the file it heads carries two off-schema sections. A
hand-typed fixture would have had five, and would have been a test of the model
that wrote the bug rather than of the surface.
