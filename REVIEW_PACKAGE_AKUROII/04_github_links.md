# روابط GitHub الدقيقة — كل رابط مُتحقَّق منه في هذه الجلسة

**تاريخ التحقق:** 2026-10-31
**طريقة التحقق:** `gh api repos/Akuroii/Nilive-Bot/commits/<sha>` و`.../compare/<a>...<b>` لكل عنصر أدناه.
**المستودع:** `https://github.com/Akuroii/Nilive-Bot`

> كل SHA أدناه أُعيد حلّه إلى طوله الكامل من استنساخ حي (791 commit، `fsck` نظيف)، ثم فُحص وجوده على GitHub عبر REST API.
> ما فشل فحصه مُعلَّم صراحة. **لا يوجد هنا رابط مُختلَق.**

---

## ١. الفروع

| الفرع | الطرف (SHA كامل) | التاريخ | الرابط |
|---|---|---|---|
| `main` | `a6b812be26d35f3048db39d0425374d23cd804ea` | 2026-10-23 22:15 +0300 | https://github.com/Akuroii/Nilive-Bot/tree/main |
| **`arena/c014328f-nilive-bot`** | `f9e413f72a026ac09e33104f809ea2345753eee7` | 2026-10-30 12:35 +0000 | https://github.com/Akuroii/Nilive-Bot/tree/arena/c014328f-nilive-bot |
| `review/g-purchase-grant-ledger` | `c872aa299396ed58fb19ab3c0fb5c6997b86a6ee` | 2026-10-30 18:34 +0200 | https://github.com/Akuroii/Nilive-Bot/tree/review/g-purchase-grant-ledger |
| `review/phase22-final` | `8eb8e3d4fc18c1e96cf9ec3e6d6b55aa63fc0436` | 2026-10-29 16:12 +0000 | https://github.com/Akuroii/Nilive-Bot/tree/review/phase22-final |
| `arena/884b26a8-nilive-bot` (PR #104) | `baa6883a286551dfbd5b1837c8bcc038cf7897e2` | 2026-10-24 10:09 +0000 | https://github.com/Akuroii/Nilive-Bot/tree/arena/884b26a8-nilive-bot |

---

## ٢. نطاقات المقارنة (compare) — كلها مُتحقَّقة عبر API

| النطاق | ahead | behind | ملفات | الرابط |
|---|---|---|---|---|
| `main...c014328f` | **53** | **0** | **113** | https://github.com/Akuroii/Nilive-Bot/compare/a6b812be26d35f3048db39d0425374d23cd804ea...f9e413f72a026ac09e33104f809ea2345753eee7 |
| `main...g-purchase-grant-ledger` | 1 | 0 | 22 | https://github.com/Akuroii/Nilive-Bot/compare/a6b812be26d35f3048db39d0425374d23cd804ea...c872aa299396ed58fb19ab3c0fb5c6997b86a6ee |
| `main...884b26a8` (PR #104) | 7 | 0 | 20 | https://github.com/Akuroii/Nilive-Bot/compare/a6b812be26d35f3048db39d0425374d23cd804ea...baa6883a286551dfbd5b1837c8bcc038cf7897e2 |

**`behind = 0` في الثلاثة** — أي أن merge-base هو طرف `main` الحالي بالضبط. هذا هو أساس قابلية الدمج.

---

## ٣. الـcommits المحورية في سلسلة `c014328f`

| SHA كامل | الموضوع | الرابط |
|---|---|---|
| `dbf772f77038a0b0fc58b3f47d73388de4cad6ed` | feat(inventory): لوحة Items/Potions/Titles | https://github.com/Akuroii/Nilive-Bot/commit/dbf772f77038a0b0fc58b3f47d73388de4cad6ed |
| `1f1c62dc65c0387c8fb00c9baf0c232164548d6c` | feat(shop): نموذج الملكية المؤقتة (Phase 1) — يُدخل `utils/timed_ownership.py` | https://github.com/Akuroii/Nilive-Bot/commit/1f1c62dc65c0387c8fb00c9baf0c232164548d6c |
| `baa6883a286551dfbd5b1837c8bcc038cf7897e2` | test(shop): تغطية الملكية المؤقتة + Colors + purchase-saga — طرف PR #104 | https://github.com/Akuroii/Nilive-Bot/commit/baa6883a286551dfbd5b1837c8bcc038cf7897e2 |
| `670ffebf1571954537ef4549b2bc850357b4b280` | Reconcile rollback descriptors — **يُدخل `tests.yml` و`TEST_BASELINE.md`** (أساس خط الأساس المقاس) | https://github.com/Akuroii/Nilive-Bot/commit/670ffebf1571954537ef4549b2bc850357b4b280 |
| `3307231ae4ad838f79addcdd0a15b2fad2aa4a9e` | Pin the Booster colour-selection gap — **آخر commit شُغّل عليه CI فعلاً** | https://github.com/Akuroii/Nilive-Bot/commit/3307231ae4ad838f79addcdd0a15b2fad2aa4a9e |
| `8eb8e3d4fc18c1e96cf9ec3e6d6b55aa63fc0436` | CI على pull_request فقط — طرف `review/phase22-final` | https://github.com/Akuroii/Nilive-Bot/commit/8eb8e3d4fc18c1e96cf9ec3e6d6b55aa63fc0436 |
| `6c04021ef5b422998a80bddcf5d4635a8b529039` | Archive unreviewed snapshot — يُدخل `recovery/…tar.gz` | https://github.com/Akuroii/Nilive-Bot/commit/6c04021ef5b422998a80bddcf5d4635a8b529039 |
| `cb54484cebeda195169acffd5f232e78d98c13c5` | إصلاحات أمان التسليم A–F + `test_delivery_safety.py` + `AGENTS.md`/`CLAUDE.md` | https://github.com/Akuroii/Nilive-Bot/commit/cb54484cebeda195169acffd5f232e78d98c13c5 |
| `f9e413f72a026ac09e33104f809ea2345753eee7` | **طرف الفرع** — تطبيع تنسيق artifacts | https://github.com/Akuroii/Nilive-Bot/commit/f9e413f72a026ac09e33104f809ea2345753eee7 |

### commits تسليم أدوار Discord (لمراجعة Akuroi في البند ٣)

| SHA كامل | الموضوع |
|---|---|
| `7283809b468095fa001adf3fdc81a72a6a9479f1` | Harden timed ownership, equip swap, entitlements, purchase saga recovery |
| `6adc1382d09b5f48be313055674159ee32b75c2b` | Harden purchase transaction rollback, legacy equip slot handling |
| `374f1c8c743fded37973edf26f0a2348bb63ad23` | Fix refund-vs-delivery race + fail-safe role entitlement checks |
| `62ca5585e27d26c2707a81d659b95c70e6a8d92b` | Guard delivered purchase transitions, classify ambiguous Discord role grants |
| `86913f6adff610e1a87b14a0b8d733686477e82d` | fail-safe `temp_role_cleanup` entitlement, late-delivery rollback |
| `5100a5fd5e33de57ba587bcb427680bedf277d05` | harden purchase compensation for permanent Colors, xp_boost, pre-held roles |
| `2684cbbe331e2d6034b1a9eb85f73176be78d8ef` | F1 cached-member role removal, F2 durable inventory rollback, F3 rebuy equip |
| `4f0e0371abdf9713690951236b2b523b539e6c3b` | Harden late-delivery rollback concurrency, role deletion proof |
| `17dfcc0520d5a4f9332701cbf3c8e112327db476` | Fix permanent-role and boost compensation races |
| `7137d71d04cb3084d73670885284644911e5d4ac` | Keep `unequip_role` from erasing a slot equipped during its await |
| `6467bbdae7bd43941ab7b4e61f4bef43ea0386db` | Serialise `equip_role`/`unequip_role` per member |
| `e82456a60aaa46556381b992d6e8b3f9b8d29464` | Serialise role-bearing timed grants on the member's equip slot |
| `7cb64603d5dd12a9c774f6a5a8688be356f06901` | Serialise expiry on the member's equip slot |
| `aa496cdd2f27238fa0d95f660e6e5dd788c99d28` | Serialise the legacy temp-role sweep |
| `48ddb0f1a4da8534a178db43dcf7ad6c155c4787` | Gate late-delivery role compensation on the member's equip slot |
| `a88f1da12892d5f01141f6339018d19a25d0d27b` | Keep a Shop-owned role when a system retires its own entitlement |
| `e72d75ab33b363d1891627eee654306c4df7b15f` | Guard the third role-removal loop in `_handle_unboost` |
| `9e10c032cbb34de815823a45f7caba262dd8451c` | Stop a follow-up unequip from orphaning a concurrent equip task |
| `867c067baf8bacc27b520af5b6ee5ddedc54fc50` | Extend the shared-role policy to Twitch and MVP, in both directions |
| `ea75a90d55a944524329746b7851dd1129d525ad` | Check every source, not just the Shop, before a system removes a role |
| `b8e76d1857b54fb16379a8245e777965c6759fb1` | Keep the Prestige resolution path total when the member cannot re-resolve |
| `3307231ae4ad838f79addcdd0a15b2fad2aa4a9e` | Pin the Booster colour-selection gap and its selection semantics |

كل رابط منها على الصورة: `https://github.com/Akuroii/Nilive-Bot/commit/<الـSHA الكامل>`

---

## ٤. الوثائق على الفرع (روابط مباشرة)

| الوثيقة | الرابط |
|---|---|
| `docs/execution-20261030/README.md` — **نقطة التنفيذ، «NOT ready for a delivery PR»** | https://github.com/Akuroii/Nilive-Bot/blob/f9e413f72a026ac09e33104f809ea2345753eee7/docs/execution-20261030/README.md |
| `docs/execution-20261030/ROLE_SYNCHRONIZATION.md` — جرد 78 موضع كتابة أدوار | https://github.com/Akuroii/Nilive-Bot/blob/f9e413f72a026ac09e33104f809ea2345753eee7/docs/execution-20261030/ROLE_SYNCHRONIZATION.md |
| `docs/execution-20261030/role-writers.csv` | https://github.com/Akuroii/Nilive-Bot/blob/f9e413f72a026ac09e33104f809ea2345753eee7/docs/execution-20261030/role-writers.csv |
| `docs/execution-20261030/ARCHIVE_COMPARISON.md` | https://github.com/Akuroii/Nilive-Bot/blob/f9e413f72a026ac09e33104f809ea2345753eee7/docs/execution-20261030/ARCHIVE_COMPARISON.md |
| `docs/execution-20261030/reproduce_inventory_ambiguity.py` — **مُعيد R1** | https://github.com/Akuroii/Nilive-Bot/blob/f9e413f72a026ac09e33104f809ea2345753eee7/docs/execution-20261030/reproduce_inventory_ambiguity.py |
| `docs/execution-20261030/reproduce_booster_obligation_gap.py` — **مُعيد R2** | https://github.com/Akuroii/Nilive-Bot/blob/f9e413f72a026ac09e33104f809ea2345753eee7/docs/execution-20261030/reproduce_booster_obligation_gap.py |
| `TEST_BASELINE.md` | https://github.com/Akuroii/Nilive-Bot/blob/f9e413f72a026ac09e33104f809ea2345753eee7/TEST_BASELINE.md |
| `.github/workflows/tests.yml` | https://github.com/Akuroii/Nilive-Bot/blob/f9e413f72a026ac09e33104f809ea2345753eee7/.github/workflows/tests.yml |
| `utils/timed_ownership.py` | https://github.com/Akuroii/Nilive-Bot/blob/f9e413f72a026ac09e33104f809ea2345753eee7/utils/timed_ownership.py |

### وثائق Design C (على فرع `review/g-purchase-grant-ledger`)

| الوثيقة | الرابط |
|---|---|
| `review-materials/G-report.md` — **تقرير التصميم** | https://github.com/Akuroii/Nilive-Bot/blob/c872aa299396ed58fb19ab3c0fb5c6997b86a6ee/review-materials/G-report.md |
| `review-materials/0001-G-purchase-grant-ledger.patch` — **الـpatch الكامل** | https://github.com/Akuroii/Nilive-Bot/blob/c872aa299396ed58fb19ab3c0fb5c6997b86a6ee/review-materials/0001-G-purchase-grant-ledger.patch |
| `review-materials/MANIFEST.txt` | https://github.com/Akuroii/Nilive-Bot/blob/c872aa299396ed58fb19ab3c0fb5c6997b86a6ee/review-materials/MANIFEST.txt |
| `review-materials/SHA256SUMS-repo-files.txt` | https://github.com/Akuroii/Nilive-Bot/blob/c872aa299396ed58fb19ab3c0fb5c6997b86a6ee/review-materials/SHA256SUMS-repo-files.txt |
| `review-materials/SHA256SUMS-review-materials.txt` | https://github.com/Akuroii/Nilive-Bot/blob/c872aa299396ed58fb19ab3c0fb5c6997b86a6ee/review-materials/SHA256SUMS-review-materials.txt |
| `review-materials/blob-verification.txt` | https://github.com/Akuroii/Nilive-Bot/blob/c872aa299396ed58fb19ab3c0fb5c6997b86a6ee/review-materials/blob-verification.txt |
| `repo-files/test_purchase_grant_ledger.py` | https://github.com/Akuroii/Nilive-Bot/blob/c872aa299396ed58fb19ab3c0fb5c6997b86a6ee/repo-files/test_purchase_grant_ledger.py |

---

## ٥. pull requests

| PR | الحالة | الفرع | الرابط |
|---|---|---|---|
| **#104** | **مفتوح** | `arena/884b26a8-nilive-bot` | https://github.com/Akuroii/Nilive-Bot/pull/104 |
| #103 | مدموج 2026-10-23 — **طرف `main` الحالي** | `arena/903e7318-nilive-bot` | https://github.com/Akuroii/Nilive-Bot/pull/103 |
| **#90** | مدموج 2026-10-15 — **`fd9c452`، القناة التي دخلت منها D1–D6 وP1 إلى `main`** | `Akuroii-patch-27` | https://github.com/Akuroii/Nilive-Bot/pull/90 |
| #97 | مفتوح | `arena/ae120c69-nilive-bot` | https://github.com/Akuroii/Nilive-Bot/pull/97 |

`fd9c452444dae4da52b5711bffe73bd3032b8759` = https://github.com/Akuroii/Nilive-Bot/commit/fd9c452444dae4da52b5711bffe73bd3032b8759

---

## ٦. CI

| البند | الرابط / القيمة |
|---|---|
| سجل التشغيلات | https://github.com/Akuroii/Nilive-Bot/actions |
| عدد التشغيلات | 16، كلها على `arena/c014328f-nilive-bot` |
| آخر تشغيل ناجح | `head_sha = 3307231ae4ad838f79addcdd0a15b2fad2aa4a9e` بتاريخ 2026-10-29 16:09 |
| **الطرف `f9e413f`** | **لم يُشغَّل عليه CI قط** — 4 commits بعد آخر تشغيل (`8eb8e3d`, `6c04021`, `cb54484`, `f9e413f`) |

> **تنبيه مهم للمراجع:** `main` **لا يحوي** `.github/workflows/tests.yml`. الـAPI يعرضه لأنه موجود على فروع أخرى. تحققتُ مباشرة: `git show origin/main:.github/workflows/tests.yml` يفشل.

---

## ٧. ⚠️ المعرّفات التي **لا** توجد — لا تبحث عنها

| SHA | الحالة على GitHub |
|---|---|
| `f39ec2476ff1b8b70ed53bd9ba96b112a41c6317` | **`HTTP 422 — No commit found for SHA`** |
| `0a2645977f09bfa8beb7722a3efe7f8a0fe439f8` | **`HTTP 422 — No commit found for SHA`** |
| `3768d96…` (مصدر patch الخاص بـDesign C) | غائب — مُشار إليه داخل الحزمة فقط |
| `09d3778…` (أب `3768d96`) | غائب |
| فرع `arena/pr3-delivery-fixes` | **غير موجود** في أي ref من أصل 212 |

هذه ليست أخطاء نسخ منّي — تحققتُ منها بثلاث طرق مستقلة: `git cat-file -e` محلياً، وفي استنساخ كامل (791 commit)، وعبر REST API.
