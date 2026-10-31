# البند ٣ — عمل «External Discord Role Delivery»: ما هو موجود فعلاً مقابل ما هو مجرد تقرير سابق

**تاريخ الفحص:** 2026-10-31
**الخلاصة في سطر:** **لا يوجد أي أثر مادي لعمل يُسمّى «External Discord Role Delivery»، ولا يوجد commit اسمه `f39ec247`.** ما يوجد فعلاً هو عمل **تقوية تسليم الأدوار** (role-delivery hardening) على فرع `arena/c014328f-nilive-bot`، وهو مُستخرج بالكامل في هذا المجلد.

---

## ١. السؤال المطروح

الجلسة السابقة أبلغت عن عمل «External Discord Role Delivery» عند commit `f39ec247`.
المطلوب: بيان ما هو **موجود فعلاً** مقابل ما هو **مجرد تقرير سابق**.

---

## ٢. ما ثبت أنه **غير موجود** (بثلاث طرق مستقلة)

### ٢.١ الـcommit `f39ec247` نفسه

| الطريقة | الأمر | النتيجة |
|---|---|---|
| الأرشيف المحلي | `git cat-file -e f39ec247…` | **غائب** |
| استنساخ كامل (791 commit) | `git cat-file -e f39ec247…` في `/tmp/fx` | **غائب** |
| GitHub REST API | `gh api repos/Akuroii/Nilive-Bot/commits/f39ec2476ff1b8b70ed53bd9ba96b112a41c6317` | **`HTTP 422 — No commit found for SHA`** |

الشيء نفسه ينطبق على `0a2645977f09bfa8beb7722a3efe7f8a0fe439f8`، وعلى `3768d96…` (مصدر patch الخاص بـDesign C) وأبيه `09d3778…`.

> هذه ليست أخطاء نسخ. الأرشيف **shallow بعمق 1**، لكن الاستنساخ الكامل يحتوي **791 commit** و`fsck` نظيف — والـSHAs غائبة فيه أيضاً. لا يمكن استرجاعها من أي مكان متاح لي.

### ٢.٢ الاسم نفسه لا يظهر في أي مكان

| الفحص | النتيجة |
|---|---|
| `git grep -il "external discord role delivery"` على `origin/arena/c014328f-nilive-bot` | **0 ملفات** |
| نفس الفحص على `origin/main` | **0 ملفات** |
| نفس الفحص على `origin/review/g-purchase-grant-ledger` | **0 ملفات** |
| `git log --all` (791 commit) بحثاً عن رسالة commit تحتوي role-delivery/external-role | **لا شيء بهذا الاسم** |
| فرع `arena/pr3-delivery-fixes` (المذكور كمصدر Design C) | **غير موجود** ضمن 212 ref |

### ٢.٣ الكلمتان «external» الظاهرتان في الوثائق تقولان العكس

الظهوران الوحيدان لكلمة «external» في `docs/execution-20261030/README.md` هما **تحفّظان صريحان بأن الحدود الخارجية غير مُختبَرة**:

> «The demonstrated guarantee is SQLite all-or-nothing across inventory, reward provenance and effect, **not distributed/external exactly-once delivery**.»

> «**Notification/Discord external boundaries are faked**; entitlement and SQLite paths are real.»

أي أن الفرع نفسه ينفي وجود ضمان تسليم خارجي مُثبَت. **لا يمكن نسبة «External Discord Role Delivery» كمُنجَز إلى أي أثر هنا.**

---

## ٣. ما هو **موجود فعلاً** — ومُستخرج في هذا المجلد

العمل الحقيقي المُنجز في مجال تسليم أدوار Discord موجود على `arena/c014328f-nilive-bot` تحت `docs/execution-20261030/`. **61 ملفاً**، كلها مُستخرجة هنا:

### ٣.١ الوثائق (٤ ملفات)

| الملف | الحجم | المحتوى |
|---|---|---|
| `ROLE_SYNCHRONIZATION.md` | 9,306 B | **جرد 78 موضع كتابة أدوار إنتاجي** (add_roles/remove_roles) مع القفل المُستخدم في كل موضع |
| `README.md` | 10,963 B | **نقطة تنفيذ صريحة** — «NOT ready for a delivery PR» + جدول ما هو مُختبَر فعلاً مقابل ما هو stub |
| `ARCHIVE_COMPARISON.md` | 5,371 B | مقارنة ثلاثية للأرشيف |
| `archive-three-way.json` | 15,187 B | بيانات المقارنة |

### ٣.٢ البيانات (٣ ملفات)

| الملف | الحجم |
|---|---|
| `role-writers.csv` | 7,656 B |
| `tested-code-sha256.json` | 1,239 B |
| `test-dependencies.txt` | 646 B |

### ٣.٣ سجلات ما قبل الـcommit — `precommit-logs/` (٥٢ ملفاً)

أزواج `.json` + `.log` تغطي 27 موضوعاً:

```
api-and-shop              inventory-ui                  safety-first
baseline-ownership        level-race-red                safety-first.raw
booster-callback-fixed    level-race-red-valid          safety-fixed
booster-obligation-red    level-reset                   safety-provenance
booster-obligation-red-valid  ownership-after           safety-with-level-fix
booster-stub-fixed        progression                   wallet-harness
compile                   provenance
diff-check                rules-identical
frontend                  safety-final-precommit
inflight
inventory-ambiguity-confirmed
inventory-ambiguity-red
```

**هذه أقوى أثر مادي متاح**: سجلات تنفيذ فعلية بتواريخ ومخرجات، لا ادعاءات.

### ٣.٤ مُعيدات الإنتاج (٢ ملف) — مُستخرجة أيضاً في `02_design_c/from-arena-c014328f/`

| الملف | الحجم | الحالة |
|---|---|---|
| `reproduce_inventory_ambiguity.py` | 3,099 B | **R1 — أثبتُّ بنفسي أنه exit 1 قبل Design C وexit 0 بعده** |
| `reproduce_booster_obligation_gap.py` | 1,809 B | **R2 — أثبتُّ بنفسي أنه exit 1 في الحالتين، أي أن Design C لا يُصلحه** |

### ٣.٥ commits تقوية تسليم الأدوار (٢٢ commit، كلها على `c014328f`)

قائمة الـSHAs الكاملة في `04_github_links.md` §٣. أمثلة:

- `7137d71` — Keep `unequip_role` from erasing a slot equipped during its await
- `6467bbd` — Serialise `equip_role`/`unequip_role` per member
- `867c067` — Extend the shared-role policy to Twitch and MVP, in both directions
- `ea75a90` — Check every source, not just the Shop, before a system removes a role
- `48ddb0f` — Gate late-delivery role compensation on the member's equip slot
- `e72d75a` — Guard the third role-removal loop in `_handle_unboost`

---

## ٤. ما الذي يعترف الفرع نفسه بأنه **غير منجز**

من `ROLE_SYNCHRONIZATION.md` و`README.md` حرفياً:

> «This is a call-site inventory, **not a claim that every reachable path is synchronized**.»

> «Level delivery/Claim All, Booster, Prestige, Reset/rollback, Twitch, MVP, legacy level-award helpers and explicit reaction/custom/moderation role writers **still need a lock-order and caller audit** before universal serialization can be claimed.»

> «Existing Level operation leases protect a different boundary; they are not automatically the same lock as equip. **Do not wrap the whole call graph blindly: `slot_lock` is not reentrant.**»

والعائق G في `README.md`:

> «The reproduced race is fixed. **General role-delivery serialization is not proven.**»

---

## ٥. الجدول الفاصل — للمراجع

| الادعاء السابق | الحالة | الدليل |
|---|---|---|
| يوجد عمل «External Discord Role Delivery» عند `f39ec247` | ❌ **تقرير سابق فقط، لا أثر** | SHA غائب محلياً وفي 791 commit وفي GitHub (HTTP 422) |
| الاسم «External Discord Role Delivery» مُوثّق | ❌ **لا يظهر في أي ملف على أي فرع** | `git grep -il` = 0 على ثلاثة فروع |
| التسليم الخارجي مُثبَت | ❌ **الفرع ينفيه صراحة** | «Notification/Discord external boundaries are **faked**» |
| يوجد جرد مواضع كتابة الأدوار | ✅ **موجود ومُستخرج** | `ROLE_SYNCHRONIZATION.md`، 78 موضعاً |
| يوجد عمل تقوية تسليم فعلي | ✅ **موجود: 22 commit** | قائمة SHAs كاملة في `04_github_links.md` §٣ |
| توجد سجلات تنفيذ ما قبل الـcommit | ✅ **موجودة: 52 ملفاً** | `precommit-logs/`، 27 موضوعاً |
| serialization عام للأدوار مُثبَت | ❌ **الفرع ينفيه** | «General role-delivery serialization is **not proven**» |

---

## ٦. آخر مصدر معروف للمادة المفقودة

للمادة التي **لا** يمكن تقديمها (أي شيء منسوب إلى `f39ec247` أو `0a26459`):

- **لا يوجد مصدر معروف متاح.** ليست في الأرشيف المحلي، ولا في الاستنساخ الكامل، ولا على GitHub.
- إن كانت قد وُجدت، فآخر مكان معروف لها هو **بيئة الجلسة السابقة نفسها** (المسار المُشار إليه في سجلاتها: `/home/user/nilive-test-env/…`)، وهي غير متاحة لي.
- **لم أُعِد إنشاءها ولم أخترع بديلاً**، التزاماً بتعليماتك.

أما المادة التي **يمكن** تقديمها فكلها في هذا المجلد، مستخرجة حرفياً من `origin/arena/c014328f-nilive-bot` دون تعديل.
