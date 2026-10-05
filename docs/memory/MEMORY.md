# Project memory index

- [Milestone status](milestone-status.md) — read side M0, M2-M6 approved 2026-10-04 (M1 dropped); full dump in dumps/ on WSL; write mode W1-W6 done on hardware 2026-10-05 (full rewrite, dump SHA unchanged); W7 waits for a second chip; host→device USB ~290 KB/s limits write speed
- [Datasheet findings](datasheet-findings.md) — SR=60h after reset, dummy 00h before 70h, 5 ms power-on busy, golden param page CRC 3B C5
