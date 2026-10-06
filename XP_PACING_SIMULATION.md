# XP pacing simulation — live formula and gates, applied defaults

**Applied after this simulation (owner decision):** `xp_cooldown_seconds` **30 → 20 s**, and
nothing else — `xp_per_word`, the clamps, `spam_threshold`/`spam_xp_penalty`, the level curve and
every other economy value are unchanged. Sections 1–6 give the matrix with 20 s (the new
default) next to 25/30/15 s for comparison; section 7 records what the change itself buys.

Simulated with the **live gate order** (`cogs/leveling.py` + `utils/xp_calculator.py`):

1. **anti-spam gate first** — the 3rd message inside a 10 s window is flagged:
   `xp = max(0, xp - 10)`, it earns nothing **and the cooldown is not consumed**;
2. **cooldown gate** — a message that earns XP starts the cooldown (`< cooldown` → no XP);
3. **XP** — `int(max(5, min(50, words × xp_per_word)))`, role/boost multipliers 1.0.

Arrivals are jittered (seeded, 5 runs averaged), 6 active hours/day in two 3 h
sessions; voice XP = 3/min for the same 6 h when ON (the ≥2-real-members gate is not modelled
as a reduction — it is an upper bound). No production formula was changed to run this.

## Level curve (unchanged)

| level | XP for that level | cumulative |
|---|---:|---:|
| L10 | 3,162 | 14,264 |
| L25 | 12,500 | 131,300 |
| L50 | 35,355 | 724,849 |
| L75 | 64,951 | 1,981,105 |
| L90 | 85,381 | 3,116,502 |
| L100 | 100,000 | 4,050,079 |

**Target**: L100 = 4,050,079 XP → **13,500 XP/day** over 300 days.

## 1. Capacity: what the cooldown ceiling allows (arrival faster than the cooldown)

| cooldown | XP messages/h | XP messages/day (6 h) | chat XP/day at 10 words | at 20 words | at the 50 clamp |
|---:|---:|---:|---:|---:|---:|
| 20s **(default)** | 180 | 1080 | 11,880* | 22,680* | 55,080* |
| 25s | 144 | 864 | 9,720* | 18,360* | 44,280* |
| 30s | 120 | 720 | 8,280* | 15,480* | 37,080* |
| 15s | 240 | 1440 | 15,480* | 29,880* | 73,080* |

\* including the 1,080 voice XP/day. The cooldown ceiling — not the level curve — is the first
hard wall: even while spamming, a member cannot earn more XP-messages than this per day.

## 2. Matrix — XP/day and days to L50 / L75 / L100

Voice ON; cells are `XP/day — L50 / L75 / L100`. `↧` marks a scenario pinned to the cooldown
ceiling (arrival rate above the cap); otherwise the result depends on how much the member types.

### casual (~200 msg/day) — voice ON

| words | cd=20s | cd=25s | cd=30s | cd=15s |
|---:|---|---|---|---|
| 5 | 1,903 — 381d / 1042d / 2129d | 1,870 — 388d / 1060d / 2166d | 1,829 — 397d / 1084d / 2215d | 1,928 — 376d / 1028d / 2101d |
| 8 | 2,412 — 301d / 822d / 1679d | 2,360 — 308d / 840d / 1717d | 2,294 — 316d / 864d / 1766d | 2,452 — 296d / 808d / 1652d |
| 10 | 2,752 — 264d / 720d / 1472d | 2,686 — 270d / 738d / 1508d | 2,604 — 279d / 761d / 1556d | 2,802 — 259d / 708d / 1446d |
| 12 | 3,092 — 235d / 641d / 1311d | 3,012 — 241d / 658d / 1345d | 2,914 — 249d / 680d / 1390d | 3,152 — 230d / 629d / 1286d |
| 15 | 3,601 — 202d / 551d / 1125d | 3,502 — 207d / 566d / 1157d | 3,379 — 215d / 587d / 1199d | 3,676 — 198d / 539d / 1102d |
| 20 | 4,450 — 163d / 446d / 911d | 4,318 — 168d / 459d / 938d | 4,154 — 175d / 477d / 975d | 4,550 — 160d / 436d / 891d |

Voice at 10 words, cd=20s: **OFF 1,672 XP/day** → L100 in 2423d; **ON 2,752 XP/day** → 1472d. Voice XP is ~13 % of a day; never the lever.

Anti-spam interaction, cd=20s, 10 words: 3 flagged messages/day (penalty 10 XP each) against 170 XP-granting messages.

### active (~400 msg/day) — voice ON

| words | cd=20s | cd=25s | cd=30s | cd=15s |
|---:|---|---|---|---|
| 5 | 2,465 — 295d / 804d / 1644d | 2,384 — 305d / 832d / 1699d | 2,301 — 316d / 861d / 1761d | 2,568 — 283d / 772d / 1578d |
| 8 | 3,339 — 218d / 594d / 1213d | 3,210 — 226d / 618d / 1262d | 3,077 — 236d / 644d / 1317d | 3,504 — 207d / 566d / 1156d |
| 10 | 3,922 — 185d / 506d / 1033d | 3,760 — 193d / 527d / 1078d | 3,594 — 202d / 552d / 1127d | 4,128 — 176d / 480d / 982d |
| 12 | 4,505 — 161d / 440d / 900d | 4,310 — 169d / 460d / 940d | 4,111 — 177d / 482d / 986d | 4,752 — 153d / 417d / 853d |
| 15 | 5,379 — 135d / 369d / 753d | 5,136 — 142d / 386d / 789d | 4,887 — 149d / 406d / 829d | 5,688 — 128d / 349d / 713d |
| 20 | 6,836 — 107d / 290d / 593d | 6,512 — 112d / 305d / 622d | 6,180 — 118d / 321d / 656d | 7,248 — 101d / 274d / 559d |

Voice at 10 words, cd=20s: **OFF 2,842 XP/day** → L100 in 1426d; **ON 3,922 XP/day** → 1033d. Voice XP is ~13 % of a day; never the lever.

Anti-spam interaction, cd=20s, 10 words: 7 flagged messages/day (penalty 10 XP each) against 291 XP-granting messages.

### heavy (~800 msg/day) — voice ON

| words | cd=20s | cd=25s | cd=30s | cd=15s |
|---:|---|---|---|---|
| 5 | 2,860 — 254d / 693d / 1417d | 2,667 — 272d / 743d / 1519d | 2,475 — 293d / 801d / 1637d | 3,120 — 233d / 635d / 1299d |
| 8 | 4,230 — 172d / 469d / 958d | 3,920 — 185d / 506d / 1034d | 3,613 — 201d / 549d / 1121d | 4,646 — 157d / 427d / 872d |
| 10 | 5,144 — 141d / 386d / 788d | 4,756 — 153d / 417d / 852d | 4,372 — 166d / 454d / 927d | 5,664 — 128d / 350d / 716d |
| 12 | 6,058 — 120d / 328d / 669d | 5,592 — 130d / 355d / 725d | 5,131 — 142d / 387d / 790d | 6,682 — 109d / 297d / 607d |
| 15 | 7,428 — 98d / 267d / 546d | 6,846 — 106d / 290d / 592d | 6,270 — 116d / 316d / 646d | 8,208 — 89d / 242d / 494d |
| 20 | 9,712 — 75d / 204d / 418d | 8,936 — 82d / 222d / 454d | 8,168 — 89d / 243d / 496d | 10,752 — 68d / 185d / 377d |

Voice at 10 words, cd=20s: **OFF 4,064 XP/day** → L100 in 997d; **ON 5,144 XP/day** → 788d. Voice XP is ~13 % of a day; never the lever.

Anti-spam interaction, cd=20s, 10 words: 50 flagged messages/day (penalty 10 XP each) against 457 XP-granting messages.

### rapid exchange (~1440/day) — voice ON

| words | cd=20s | cd=25s | cd=30s | cd=15s |
|---:|---|---|---|---|
| 5 | 1,994 — 364d / 994d / 2032d | 1,641 — 442d / 1208d / 2469d | 1,399 — 519d / 1417d / 2895d | 2,526 — 287d / 785d / 1604d |
| 8 | 3,792 — 192d / 523d / 1069d | 3,192 — 228d / 621d / 1269d | 2,766 — 263d / 717d / 1465d | 4,661 — 156d / 426d / 869d |
| 10 | 5,000 — 145d / 397d / 811d | 4,248 — 171d / 467d / 954d | 3,716 — 196d / 534d / 1090d | 6,086 — 120d / 326d / 666d |
| 12 | 6,208 — 117d / 320d / 653d | 5,304 — 137d / 374d / 764d | 4,666 — 156d / 425d / 868d | 7,511 — 97d / 264d / 540d |
| 15 | 8,020 — 91d / 248d / 505d | 6,889 — 106d / 288d / 588d | 6,091 — 120d / 326d / 665d | 9,649 — 76d / 206d / 420d |
| 20 | 11,040 — 66d / 180d / 367d | 9,532 — 77d / 208d / 425d | 8,468 — 86d / 234d / 479d | 13,212 — 55d / 150d / 307d |

Voice at 10 words, cd=20s: **OFF 3,920 XP/day** → L100 in 1034d; **ON 5,000 XP/day** → 811d. Voice XP is ~13 % of a day; never the lever.

Anti-spam interaction, cd=20s, 10 words: 213 flagged messages/day (penalty 10 XP each) against 604 XP-granting messages.

### bursty (800/day, 3-msg bursts) — voice ON

| words | cd=20s | cd=25s | cd=30s | cd=15s |
|---:|---|---|---|---|
| 5 | 1,080 — 672d / 1835d / >10y | 1,080 — 672d / 1835d / >10y | 1,080 — 672d / 1835d / >10y | 1,080 — 672d / 1835d / >10y |
| 8 | 1,080 — 672d / 1835d / >10y | 1,080 — 672d / 1835d / >10y | 1,080 — 672d / 1835d / >10y | 1,080 — 672d / 1835d / >10y |
| 10 | 1,080 — 672d / 1835d / >10y | 1,080 — 672d / 1835d / >10y | 1,080 — 672d / 1835d / >10y | 1,080 — 672d / 1835d / >10y |
| 12 | 1,090 — 665d / 1818d / >10y | 1,088 — 667d / 1821d / >10y | 1,085 — 668d / 1826d / >10y | 1,090 — 665d / 1818d / >10y |
| 15 | 1,129 — 643d / 1755d / 3588d | 1,116 — 650d / 1776d / 3630d | 1,107 — 655d / 1790d / >10y | 1,165 — 623d / 1701d / 3477d |
| 20 | 1,994 — 364d / 994d / 2032d | 1,754 — 414d / 1130d / 2310d | 1,608 — 451d / 1233d / 2519d | 2,158 — 336d / 919d / 1877d |

Voice at 10 words, cd=20s: **OFF 0 XP/day** → L100 in —; **ON 1,080 XP/day** → >10y. Voice XP is ~13 % of a day; never the lever.

Anti-spam interaction, cd=20s, 10 words: 346 flagged messages/day (penalty 10 XP each) against 218 XP-granting messages.

## 3. Cumulative XP and level at 30 / 100 / 200 / 300 days (voice ON)

| scenario | XP/day | 30 d | 100 d | 200 d | 300 d |
|---|---:|---|---|---|---|
| heavy, 10 words, cd=20s (default) | 5,144 | 154,320 (L26) | 514,400 (L43) | 1,028,800 (L57) | 1,543,200 (L67) |
| heavy, 10 words, cd=30s (old default) | 4,372 | 131,160 (L24) | 437,200 (L40) | 874,400 (L53) | 1,311,600 (L63) |
| rapid exchange, 10 words, cd=20s | 5,000 | 150,000 (L26) | 500,000 (L43) | 1,000,000 (L56) | 1,500,000 (L67) |
| rapid exchange, 12 words, cd=20s | 6,208 | 186,240 (L28) | 620,800 (L46) | 1,241,600 (L62) | 1,862,400 (L73) |
| rapid exchange, 12 words, cd=15s | 7,511 | 225,336 (L31) | 751,120 (L50) | 1,502,240 (L67) | 2,253,360 (L78) |
| bursty, 20 words, cd=20s | 1,994 | 59,820 (L18) | 199,400 (L29) | 398,800 (L39) | 598,200 (L46) |
| active, 20 words, cd=20s | 6,836 | 205,080 (L29) | 683,600 (L48) | 1,367,200 (L64) | 2,050,800 (L76) |

## 4. Does L100-in-300-days depend on an unrealistic message ceiling? (unchanged requirement)

**Yes.** 13,500 XP/day is required. Converting that back into typing:

| voice | words | XP/msg | XP-msgs/day needed | sustained rate | interval | cooldown must be ≤ |
|---|---:|---:|---:|---:|---:|---:|
| off | 8 | 8 | 1,688 | 281.3/h | 1 every 12.8s | 12.8s |
| off | 10 | 10 | 1,350 | 225.0/h | 1 every 16.0s | 16.0s |
| off | 12 | 12 | 1,125 | 187.5/h | 1 every 19.2s | 19.2s |
| off | 20 | 20 | 675 | 112.5/h | 1 every 32.0s | 32.0s |
| on | 8 | 8 | 1,553 | 258.8/h | 1 every 13.9s | 13.9s |
| on | 10 | 10 | 1,242 | 207.0/h | 1 every 17.4s | 17.4s |
| on | 12 | 12 | 1,035 | 172.5/h | 1 every 20.9s | 20.9s |
| on | 20 | 20 | 621 | 103.5/h | 1 every 34.8s | 34.8s |

At realistic volumes (200–400 messages/day — the casual/active rows above) the cooldown never
binds and a member earns 1,000–2,700 XP/day → L100 in 4–10 years. The ~10-month target is only
reachable by a schedule that saturates the cooldown every day (section 7).

## 5. Side finding: the anti-spam defaults zero out normal bursts (still open)

A member typing in 3-message bursts (3 s apart) every ~40 s — ordinary conversation, not spam —
trips the 10 s/3-message gate once per burst. Each flag costs 10 XP, exactly one
10-word message's XP:

* bursty, 10 words, cd=20s: **1,080 XP/day** — 346 flags against
  218 grants, so chat XP is cancelled by penalties;
* bursty, 20 words, cd=20s: **1,994 XP/day** (the penalty no longer cancels the gain).

Below ~10 words per message a bursting member earns nothing at all. That is a pacing decision,
not a moderation one, and it hits the most human pattern hardest. The user chose **not** to
change the spam defaults in this pass; the candidate pair (`spam_threshold` 5 / `spam_xp_penalty`
3) stays on the table as an independent follow-up.

## 6. Levers considered (for the record)

| lever | effect | spam risk | decision |
|---|---|---|---|
| `xp_cooldown_seconds` 30 → 20 s | ceiling 720 → 1,080 XP-msgs/day (+50 %); no change for a
  member below 1 msg/20 s | raises the ceiling for fast senders too; the anti-spam gate still
  runs first | **APPLIED** |
| `xp_cooldown_seconds` 20 → 15 s | ceiling 1,350/+87 % | larger effect, same caveat | left at 20 s
| `xp_per_word` 1 → 2 | doubles every message | doubles spam earnings in proportion | not applied |
| `spam_threshold` 3 → 5, penalty 10 → 3 | stops ordinary bursts being zeroed | a real spammer
  keeps a little XP (cooldown + window still cap them) | **declined for now** |

## 7. Effect of the applied change (cooldown 30 s → 20 s, nothing else)

| metric | before (30 s) | after (20 s) |
|---|---:|---:|
| XP-message ceiling per hour | 120 | 180 |
| XP-message ceiling per 6 h day | 720 | 1,080 |
| max chat XP/day at 10 words | 7,200 | 10,800 |
| max chat XP/day at 12 words | 8,640 | 12,960 |
| max chat XP/day at the 50 XP clamp | 36,000 | 54,000 |

The 300-day target needs 13,500 XP/day. Adding the 1,080 voice XP:

| scenario at the new 20 s ceiling (6 h, every day) | XP/day | days to L100 |
|---|---:|---:|
| 1,080 XP-messages/day at 8 words | 9,720 | 417d |
| 1,080 XP-messages/day at 10 words | 11,880 | 341d |
| 1,080 XP-messages/day at 12 words | 14,040 | 289d |
| 1,080 XP-messages/day at 20 words | 22,680 | 179d |

So the change moves L100-in-300-days from *unreachable* (old ceiling: 720 grants × 12 words =
9,720 XP/day → 417d) to *reachable only by a member who sends
~1,080 XP-eligible messages/day (one every 20 s for the full 6 h, every day) with ≥12-word
messages* — ~289 days. Nobody below 1 message/20 s notices the change at all, so ordinary
members keep their old pacing; the anti-spam gate still runs first and the cooldown still caps
payout at 1,080 grants/day, so there is no new spam incentive.
