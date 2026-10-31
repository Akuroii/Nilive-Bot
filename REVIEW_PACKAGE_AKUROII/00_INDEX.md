# حزمة مراجعة Akuroi — الفهرس

**تاريخ الإعداد:** 2026-10-31
**المُعد:** جلسة الاسترجاع المستقلة (Arena)
**المستودع:** `https://github.com/Akuroii/Nilive-Bot`
**الحجم:** 90 ملفاً، ~2.0 MB

> **مصدر كل ملف:** مستخرج حرفياً من استنساخ حي للمستودع (`git archive` / `git show`)، **بدون أي تعديل**.
> لم يُعَد بناء أي نسخة مفقودة، ولم يُنشَأ بديل لأي مادة غير متاحة.

---

## البنود الأربعة المطلوبة

| # | المطلوب | الحالة | الموضع |
|---|---|---|---|
| ١ | `RECOVERY_INVENTORY_2026-10-31.md` كاملاً | ✅ متاح | `01_inventory/` |
| ٢ | مواد Design C الأصلية (patch + test + R1 + R2 + manifest + checksums + report) | ✅ **كلها متاحة** | `02_design_c/` |
| ٣ | عمل «External Discord Role Delivery» عند `f39ec247` | ⚠️ **غير متاح — انظر البيان** | `03_role_delivery/` |
| ٤ | روابط GitHub الدقيقة | ✅ **كل رابط مُتحقَّق منه** | `04_github_links.md` |

---

## البنية

```
REVIEW_PACKAGE_AKUROII/
├── 00_INDEX.md                                    ← هذا الملف
├── 04_github_links.md                             ← البند ٤
│
├── 01_inventory/                                  ← البند ١
│   ├── RECOVERY_INVENTORY_2026-10-31.md            (398 سطراً — التصنيف + خطة 7 دفعات)
│   └── NILIVE_SESSION_HANDOFF.md                   (658 سطراً — المرجع المعتمَد)
│
├── 02_design_c/                                   ← البند ٢
│   ├── from-review-g-purchase-grant-ledger/        (22 ملفاً، من فرع c872aa2)
│   │   ├── review-materials/
│   │   │   ├── 0001-G-purchase-grant-ledger.patch  (118,342 B — الـpatch الكامل)
│   │   │   ├── G-report.md                         (7,823 B — تقرير التصميم)
│   │   │   ├── MANIFEST.txt                        (5,965 B)
│   │   │   ├── SHA256SUMS-repo-files.txt           (1,056 B)
│   │   │   ├── SHA256SUMS-review-materials.txt     (1,001 B)
│   │   │   ├── blob-verification.txt               (847 B)
│   │   │   ├── diff-09d3778..3768d96.patch         (114,817 B)
│   │   │   ├── diff-stat.txt / name-status.txt
│   │   │   ├── clean-3768d96-tests.log
│   │   │   ├── reproduce_inventory_ambiguity.output.txt      (3,293 B — مخرجات R1)
│   │   │   └── reproduce_booster_obligation_gap.output.txt   (1,359 B — مخرجات R2)
│   │   └── repo-files/
│   │       ├── test_purchase_grant_ledger.py       (32,960 B — 29 اختباراً)
│   │       ├── cogs/shop.py · utils/{level_ops,level_test_reset,reward_engine,timed_ownership}.py
│   │       ├── test_timed_ownership.py
│   │       └── docs/execution-20261030/{G-purchase-grant-ledger.md, README.md, reproduce_inventory_ambiguity.py}
│   │
│   └── from-arena-c014328f/                        ← **مُعيدات الإنتاج الأصلية (سكريبتات)**
│       ├── reproduce_inventory_ambiguity.py        (3,099 B — **R1**)
│       └── reproduce_booster_obligation_gap.py     (1,809 B — **R2**)
│
└── 03_role_delivery/                              ← البند ٣
    ├── STATEMENT_WHAT_EXISTS_VS_PRIOR_REPORT.md    ← **اقرأ هذا أولاً**
    └── from-arena-c014328f/docs/execution-20261030/  (61 ملفاً)
        ├── ROLE_SYNCHRONIZATION.md                 (9,306 B — 78 موضع كتابة أدوار)
        ├── README.md                               (10,963 B — نقطة التنفيذ)
        ├── ARCHIVE_COMPARISON.md · archive-three-way.json
        ├── role-writers.csv · tested-code-sha256.json · test-dependencies.txt
        ├── reproduce_inventory_ambiguity.py · reproduce_booster_obligation_gap.py
        └── precommit-logs/                         (52 ملفاً، 27 موضوعاً)
```

---

## ⚠️ ثلاث ملاحظات يجب أن يراها المراجع

### ١. حزمة Design C تُعلن 11 ملفاً في `repo-files/` لكن فيها 10

| المصدر | العدد المُعلَن |
|---|---|
| `MANIFEST.txt` | 11 |
| `SHA256SUMS-repo-files.txt` | 11 |
| `blob-verification.txt` | 11 |
| **الموجود فعلياً في الشجرة** | **10** |

الملف الناقص: **`.github/workflows/tests.yml`**
- `sha256` المُتوقَّع في الحزمة: `dd82239920d236763011caee74e171e38bb57b16cff895c0aa380997451cf3d1`
- `sha256` نسخة `c014328f`: `20d271e1875a67c9e75e805ddda70027c0bf2715cdc3ffb56a69a6e247a36b8d` — **مختلفة**
- سبب الاختلاف: الحزمة تتبّع commit `3768d96` (المفقود)، لا طرف `c014328f`
- تحققتُ من الـblob المذكور في `blob-verification.txt` (`4466b90c…`): **`could not get object info`** — غير موجود في المستودع

**الخلاصة:** لا تعتمد على `tests.yml` من الحزمة. استخدم النسخة على الفرع:
https://github.com/Akuroii/Nilive-Bot/blob/f9e413f72a026ac09e33104f809ea2345753eee7/.github/workflows/tests.yml

### ٢. مُعيد R2 ليس داخل حزمة Design C

الحزمة تحمل **مخرجات** R2 فقط (`reproduce_booster_obligation_gap.output.txt`)، **لا السكريبت**.
السكريبت موجود فقط على `arena/c014328f-nilive-bot` — وهو مستخرج هنا في `02_design_c/from-arena-c014328f/`.

كذلك: الحزمة تحمل نسختها **المُعدَّلة** من `reproduce_inventory_ambiguity.py` (5,671 B)، بينما الأصل على `c014328f` حجمه 3,099 B. **النسختان مُسلَّمتان هنا ومُسمَّيتان بمصدرهما** — لا تخلط بينهما.

### ٣. ما ثبتت استحالته

| SHA | الحالة |
|---|---|
| `f39ec2476ff1b8b70ed53bd9ba96b112a41c6317` | **`HTTP 422 — No commit found`** |
| `0a2645977f09bfa8beb7722a3efe7f8a0fe439f8` | **`HTTP 422 — No commit found`** |
| `3768d96…` (مصدر patch الخاص بـDesign C) | غائب — مُشار إليه داخل الحزمة فقط |
| `09d3778…` (أب `3768d96`) | غائب |
| فرع `arena/pr3-delivery-fixes` | غير موجود ضمن 212 ref |

فُحصت بثلاث طرق مستقلة: `git cat-file -e` محلياً، وفي استنساخ كامل (791 commit، `fsck` نظيف)، وعبر GitHub REST API.

---

## ما تحققتُ منه بنفسي في هذه الجلسة

| الفحص | النتيجة |
|---|---|
| كل SHA مُستشهَد به يحلّ على GitHub | ✅ كلها `OK` عدا ما ذُكر أعلاه |
| نطاقات `compare` الثلاثة | ✅ `53/0/113` · `1/0/22` · `7/0/20` — **`behind=0` في الثلاثة** |
| استخراج Design C مقابل checksums الحزمة نفسها | ✅ **10/10 `repo-files` OK** · **11/11 `review-materials` OK** |
| وجود البنود الأربعة الأساسية للبند ٢ | ✅ patch · test · R1 · R2 · MANIFEST · SHA256SUMS×2 · G-report |

---

## ما **لم** أفعله (التزاماً بالتعليمات)

- ❌ لم أبدأ B0/B1 ولا أي دفعة من الدفعات السبع
- ❌ لم أنقل أو أعدّل أو أحذف أو أعمل commit/push/PR/merge
- ❌ لم أُنشئ أرشيفاً جديداً
- ❌ لم أُعِد بناء أي نسخة مفقودة
- ❌ لم أُعِد تشغيل الاختبارات لمجرد التكرار
- ✅ الأرشيف الأصلي `/home/user/Nilive-Bot` **بقي سليماً**: HEAD `a6b812be…`، tree `d0edba20…`، 614 ملفاً مُتتبَّعاً، **0 تعديل على المصدر**
