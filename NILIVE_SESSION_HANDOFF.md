# NILIVE_SESSION_HANDOFF — تسليم واستعادة عمل جلسة التطوير السابقة

**تاريخ الإعداد:** 2026-10-31
**أعده:** وكيل مستقل (جلسة جديدة، بلا اعتماد على ادعاءات الجلسة السابقة)
**الحالة:** تقرير للقراءة والمراجعة فقط. **لم يُنفَّذ أي نقل، ولم يُعدَّل أي ملف مصدر، ولم يُفتَح أي PR.**

---

## ٠. بطاقة التعريف بالحالة المقاسة

| البند | القيمة |
|---|---|
| المستودع | `Akuroii/Nilive-Bot` |
| أرشيف العمل (هذه الجلسة) | `/home/user/Nilive-Bot` |
| فرع الجلسة | `arena/3792b762-nilive-bot` |
| SHA الأرشيف | `a6b812be26d35f3048db39d0425374d23cd804ea` |
| بصمة الشجرة | `d0edba20c252ab780b7b43013e216ce36cf8db56` — **مطابقة لـ`origin/main`** |
| حالة الأرشيف | نظيف: 614 ملفاً متتبَّعاً، 0 تعديل، 0 غير متتبَّع |
| طرف `main` على GitHub | `a6b812b` (لم يتحرك منذ 2026-10-23) |
| مصدر العمل المرشَّح للاستعادة | فرع `arena/c014328f-nilive-bot` @ `f9e413f72a026ac09e33104f809ea2345753eee7` |
| بيئة القياس | Python 3.11.2، venv من `requirements.txt` + pytest، Node v22.22.3 |
| متغيرات التشغيل | `OWNER_ID=1`، `PYTHONPATH=.`، `PYTHONFAULTHANDLER=1` |
| العزل | قياس في ثلاثة `git worktree` منفصلة (`/tmp/wt-main`, `/tmp/wt-670`, `/tmp/wt-c01`) — **الأرشيف لم يُمسّ** |
| مجموع التشغيلات | **513 تشغيلاً** (167 + 172 + 174) |

---

## ١. تاريخ العمل

### ١.١ ما دخل `main` فعلاً (موثّق من GitHub)

تسلسل PRs المدمجة ذات الصلة بالاقتصاد والمستوى والمتجر:

| PR | التاريخ | الموضوع |
|---|---|---|
| #73 | 2026-10-04 | إصلاح Shop Publisher channel-ID + عناصر المتجر المجانية (price = 0) |
| #74 | 2026-10-04 | تخفيض حارس zero-economy-row من blocker إلى warning |
| #75 | 2026-10-06 | Leveling: أدوار مكافآت قائمة على الادّعاء، حصرية مُبوَّبة، anti-spam حقيقي |
| #76 | 2026-10-07 | تقوية تدفقات XP وادّعاء المكافآت |
| #80 | 2026-10-09 | Shop Designer Delete: 409 للتصاميم ذات المنشورات |
| #81 / #82 | 2026-10-09 | إصلاح ثم **تراجع** عن زر Delete في Shop Publisher |
| #85–#92 | 2026-10-10 → 10-15 | سلسلة «Add files via upload» (حزم Shop Publication / Designer) |
| #91 | 2026-10-15 | إضافة `GET /api/leveling/rewards` المفقود |
| #93 / #95 | 2026-10-16 | مهام النشاط الصوتي قبل تهيئة العميل |
| #96 | 2026-10-16 | `/level`: مسار التقدّم، هدف المعلم، المكافآت القابلة للادّعاء |
| #98 | 2026-10-20 | أهلية المكافأة تتبع المستوى المُصحَّح |
| #99 | 2026-10-20 | تشغيل مسح XP من الـcog المُحمَّل |
| #100 | 2026-10-21 | Claim All دون تعديل البطاقة الأصلية |
| **#103** | **2026-10-23** | **تحضير إيموجي منتجات المتجر — آخر مدمج، وهو طرف `main` الحالي** |

**PRs مفتوحة (لم تُدمج):** #104, #102, #97, #94, #84, #70, #69, #66, #63, #41.

### ١.٢ العمل الذي لم يدخل `main`

بدأت الجلسة السابقة دورة تطوير واسعة **بعد** #103 مباشرة، على أساس `a6b812b`. نتيجتها 53 commit على `arena/c014328f-nilive-bot` بين 2026-10-24 و2026-10-30 12:35.

مراحل الدورة كما تسلسلت فعلياً (من سجل الـcommits):

| المرحلة | commits | المحتوى |
|---|---|---|
| **البناء** | `dbf772f` → `baa6883` (7) | لوحة المخزون، نموذج الملكية المؤقتة، أنواع المنتجات الخمسة، الـsaga مع التعويض، تبويب Colors، `test_timed_ownership.py` |
| **التقوية** | `7283809` → `670ffeb` (18) | rollback، provenance، monotonic descriptors، ربط الـholding/marker بعملية الشراء، CI، `TEST_BASELINE.md` |
| **التسلسل** | `87687c5` → `8277bdc` (16) | slot-lock لكل عضو، تسلسل equip/unequip/expiry/sweep، تقاعد الـdescriptors بقرار مشغّل |
| **سياسة الأدوار** | `e72d75a` → `3307231` (8) | حراسة `_handle_unboost`، توسيع السياسة إلى Twitch/MVP، فحص كل المصادر قبل إزالة دور، Prestige، فجوة لون Booster |
| **نقطة التنفيذ** | `8eb8e3d`, `6c04021`, `cb54484`, `f9e413f` (4) | CI على PR فقط، أرشيف اللقطة، إصلاحات أمان التسليم A–F + `test_delivery_safety.py` + `AGENTS.md`/`CLAUDE.md` + أدلة `docs/execution-20261030/` |

### ١.٣ حالة الشجرة المتّسخة في الجلسة السابقة (دليل من الـmanifest)

`recovery/nilive-snapshot-16706-unreviewed.manifest.json` يوثّق بدقة حالة عمل غير مدمجة كانت موجودة على آلة الجلسة السابقة:

| الحقل | القيمة |
|---|---|
| `purpose` | «Unreviewed reference snapshot; **not Phase 2.2 and not approved for merge**» |
| `created_at_utc` | 2026-10-30T01:45:46 |
| `base_sha` | `a6b812be26d35f3048db39d0425374d23cd804ea` |
| `tracked_modified_count` | 23 |
| `untracked_count` | 5 |
| `total_additions` / `total_deletions` | 16,706 / 523 |
| `source_root` | `/home/user/Nilive-Bot` (على آلة الجلسة السابقة) |

**هذه الـ28 ملفاً هي «التعديلات غير المحفوظة» التي ذكرتها في طلبك.** هي ليست في أرشيفنا كملفات عمل — بل محفوظة داخل blob ثنائي في فرع `c014328f` (commit `6c04021`)، مع بصمات SHA-256 لكل ملف.

كذلك تذكر وثائق الفرع مسارين للحفظ: `/home/user/nilive-preservation-20261030/local-work.tar.gz` و`/home/user/Nilive-execution`. **تحققت: كلاهما غير موجود في هذه البيئة** — كانا على آلة الجلسة السابقة فقط.

---

## ٢. حالة شجرة التكامل السابقة

### ٢.١ المعرّفات التي ذكرتها — مفقودة نهائياً

| SHA | الأرشيف (shallow) | استنساخ كامل (791 commit) | GitHub REST API |
|---|---|---|---|
| `f39ec2476ff1b8b70ed53bd9ba96b112a41c6317` | غائب | غائب | `HTTP 422 — No commit found for SHA` |
| `0a2645977f09bfa8beb7722a3efe7f8a0fe439f8` | غائب | غائب | `HTTP 422 — No commit found for SHA` |
| `09d3778` (مرجع داخلي) | — | غائب | — |
| `3768d96` (مرجع داخلي) | — | غائب | — |

لا يوجد أي ref من أصل 212 طرفه أحد هذين الـSHA. **شجرة التكامل بهذين المعرّفين غير قابلة للاسترجاع.**

### ٢.٢ لماذا انقطعت الاستمرارية

الأرشيف **مستنسخ ضحل بعمق ١**:
- `.git/shallow` يحوي `a6b812b`
- `git rev-list --all --count` = **1**
- `git fsck --full --strict` نظيف، 461 كائناً في pack واحد، 0 dangling

أي أن الأرشيف سليم لكنه **بلا تاريخ**. كل مقارنة أو بحث عن commit قديم من داخله يفشل — وهذا هو التفسير التقني لانقطاع الوصول، لا تلف في البيانات.

### ٢.٣ تصحيح لوصف الأرشيف في طلبك

| ما ذُكر | ما هو مثبت |
|---|---|
| «Git bundle» | **لا يوجد أي ملف `*.bundle` على النظام** (بحث كامل بـ`find / -xdev`) |
| «تعديلات غير محفوظة في commits» | **0** في أرشيفنا؛ النسخة الوحيدة منها محفوظة داخل `recovery/…tar.gz` |
| «ملفات غير متتبعة» | **0** |
| «الفروق الثنائية وبيانات البصمات والجرد» | **صحيح** — `REVIEW/*.diff`، `_install/*.patch` + `FILES.sha256`، `economy-currency-livereload.patch`، `P0-fixes.zip` |

### ٢.٤ ما نجا فعلياً

المستودع الحي: **212 ref، 99 فرعاً، 0 tag، 791 commit، `fsck` نظيف.**

العلاقة بين الفروع الثلاثة الحاملة للعمل **خطّية تماماً** (مثبتة بـ`git merge-base --is-ancestor`):

```
main (a6b812b)  ← merge-base للجميع، وbehind = 0
   └─ 7 commits ─→ arena/884b26a8-nilive-bot   (PR #104، مفتوح)
                     └─ 43 commits ─→ review/phase22-final
                                        └─ 3 commits ─→ arena/c014328f-nilive-bot  ← الأحدث والأكمل
```

`git log c014328f..phase22-final` = **فارغ**، أي لا يوجد commit فريد في `phase22-final`. و`git log --merges` على السلسلة = **فارغ**، أي خطّية بلا merge commits.

---

## ٣. جرد التغييرات

### ٣.١ الحزمة الرئيسية — `arena/c014328f-nilive-bot`

**53 commit، 113 ملفاً، +27,655 / −885.** منها 24,109 سطراً كود واختبارات، و3,546 سطراً توثيق وأدلة.

| الملف | الحجم | أُدخل في |
|---|---|---|
| `utils/timed_ownership.py` | 6,770 سطر (جديد) | `1f1c62d` |
| `test_timed_ownership.py` | 11,244 سطر (جديد) | `baa6883` |
| `test_delivery_safety.py` | جديد | `cb54484` |
| `test_inventory_ui.py` + `scripts/test_inventory_ui.py` | 1,094 سطر لكل منهما | `dbf772f` |
| `scripts/recover_shop_purchases.py` | جديد (أداة مشغّل) | `7283809` |
| `.github/workflows/tests.yml` | جديد | `670ffeb` |
| `TEST_BASELINE.md` | جديد | `670ffeb` |
| `cogs/shop.py` | +909 / −195 | متعدد |
| `cogs/wallet.py` | +551 / −214 | متعدد |
| `utils/reward_engine.py` | +387 / −17 | متعدد |
| `utils/inventory.py`, `utils/potion_engine.py`, `utils/level_claims.py`, `utils/equip_engine.py`, `utils/economy_safe.py`, `utils/prestige.py`, `utils/shop_validation.py`, `utils/title_engine.py`, `utils/xp_calculator.py`, `utils/rank_card_data.py`, `utils/level_test_reset.py`, `database.py`, `wallet.py` | معدَّلة | متعدد |
| `cogs/boost.py`, `cogs/mvp.py`, `cogs/trade.py`, `cogs/twitch.py` | معدَّلة | `867c067`, `ea75a90` … |
| `dashboard/templates/systems/shop.html` | معدَّل | `dbf772f` |
| `AGENTS.md`, `CLAUDE.md` | جديدان (متطابقان بايت-ببايت) | `cb54484` |
| `docs/execution-20261030/` | 40+ ملف أدلة | `cb54484` |

**حذوفات في السلسلة** (كلها في `670ffeb`، وكلها تنظيف لمخرجات مُولَّدة):
- `__pycache__/database.cpython-312.pyc`، `__pycache__/main.cpython-312.pyc`، `dashboard/__pycache__/app.cpython-312.pyc`
- `preview_p4_custom_currency.png`، `preview_p4_integration_booster.png`، `preview_p4_integration_booster_noact.png`، `preview_p4_integration_normal.png`، `preview_p4_integration_notitle.png`
- تعديل `.gitignore` لإضافة `preview_p4_*.png`

**blob ثنائي:** `recovery/nilive-snapshot-16706-unreviewed.tar.gz` (0.29 MB) في `6c04021`.

### ٣.٢ حِزم موجودة أصلاً في أرشيف `main`

| الحزمة | الحجم | الأساس | تُطبَّق على `main`؟ | الحكم |
|---|---|---|---|---|
| `_install/designer-publication-fixes.patch` | 1,717 سطر | `9c63a48` (PR #85) | **لا** | **موجودة مسبقاً في `main`** |
| `REVIEW/D1-D6.diff` | 198 سطر | `99851bf` (على `b5a56831`) | **لا** (`leveling.html:921`, `level_claims.py:428`) | **⚠️ موجودة مسبقاً في `main`** (تصحيح — انظر أدناه) |
| `REVIEW/P1_expired_processing.diff` | 48 سطر | `99851bf` | **لا** (`level_claims.py:1064`) | **⚠️ موجودة مسبقاً في `main`** (تصحيح — انظر أدناه) |
| `economy-currency-livereload.patch` | 17 سطر | — | **نعم ✅** | **الوحيدة التي تحوي عملاً غير موجود في `main`** |
| `P0-fixes.zip` | قالبان | مجهول | — | **غير قابلة للحكم** |

**دليل وجود محتوى `_install/` في `main` فعلاً:** `git apply --check` يفشل بـ`cogs/shop_publication_sync.py: already exists in working directory`، والفحص النصي على `main` يجد: `shop_publication_sync` (5 ملفات)، `channel_deleted` (7)، «remove record only» (6)، `recheck` (11).

#### ⚠️ تصحيح جوهري: `REVIEW/D1-D6` و`P1` **ليستا متعارضتين — بل مدموجتان في `main` أصلاً**

صنّفتهما في المسودة الأولى «متعارضتين» استناداً إلى فشل `git apply --check` وحده. **هذا استنتاج خاطئ.** السبب الحقيقي لفشل التطبيق هو أن المحتوى **موجود فعلاً في `main`** — تماماً كحالة `_install/`. تحققت من كل عنصر في شيفرة `main` الفعلية (لا في `REVIEW/*.diff` الذي يقتبسها):

| العنصر | الموقع في `main` |
|---|---|
| **D1** `_confirm_role_added()` | `utils/level_claims.py:584` (التعريف)، `:925` (الاستدعاء) |
| **D2** `attempted_ids` | `utils/level_claims.py:854`, `:916`, `:918` |
| **D3** `applyRewardFilter()` في الفرع الفارغ للحِمّالات الثلاثة | `leveling.html:1023` (`loadCurrencyRewards`)، `:1301` (`loadShopRewards`)، `:1361` (`loadBoostRewards`) — **بنفس نص التعليق الوارد في الـdiff** |
| **D4** `rewardApiCall()` | `leveling.html:958`, `:993`, `:1009` |
| **D5** تحذير «could not be refreshed» | `leveling.html:985` |
| **D6** `__rewardsLoadSeq` | `leveling.html:929`, `:931`, `:937` |
| **P1** `expired_before` + `processing_started_at` + `stale` في `list_claims` | `utils/level_claims.py` — الدالة كاملة مطابقة، **بنفس نص التعليق حرفياً** (`processing_started_at` ×7، `expired_before` ×4، `stale` ×14) |

**كيف دخلت `main`؟** ليس عبر دمج `b5a56831` — `git cherry` يُظهر أن **كل الـ17 commit فيه غير موجودة في `main`** بحسب patch-id. دخلت عبر **`fd9c452` («Add files via upload (#90)»)**، أي رفعاً يدوياً للملفات لا دمجاً للفرع.

> **الأثر على القرار:** لا حاجة لأي «إعادة تنفيذ يدوي» لمفاهيم D1–D6 أو P1 — وهو ما أوصيتُ به في المسودة الأولى. **العمل موجود ومنشور.** يبقى `REVIEW/` ذا قيمة توثيقية فقط (بصمات الأساس ونتائج قياس المراجع).
>
> **الاستثناء الوحيد:** `economy-currency-livereload.patch` هو الحزمة الوحيدة التي تحوي تغييراً **غير موجود** في `main` ويُطبَّق نظيفاً.

### ٣.٣ الحزمة اللاحقة غير المدمجة — «Design C»

فرع `review/g-purchase-grant-ledger` (commit واحد، 22 ملفاً، **+29,626 / −0**، بلا أي حذف).

يحتوي `repo-files/` (لقطة الملفات الناتجة) و`review-materials/` (التقرير، البصمات، الـpatch، أدلة الاستنساخ).

**حالته:** التقرير يقول صراحة *«implemented LOCALLY on `arena/pr3-delivery-fixes`, not pushed, not merged»* — وهذا الفرع **غير موجود على GitHub** (تحققت: لا `pr3` ولا `delivery` في أي ref). الـpatch مصدره `3768d96` الغائب.

الملفات الخمسة المشتركة **تختلف كلها** عن نسخ `c014328f`:

| الملف | blob في `c014328f` | blob في الحزمة |
|---|---|---|
| `utils/timed_ownership.py` | `110d5244b09b` | `a11b121514a0` |
| `utils/reward_engine.py` | `f0531303d2a7` | `c72e8475c82c` |
| `cogs/shop.py` | `8bf82fd337d3` | `4283608427e9` |
| `utils/level_test_reset.py` | `1c69241d3058` | `705aa640d956` |
| `test_timed_ownership.py` | `27f227cf4441` | `8ecad8cda4b8` |

**ملاحظة إيجابية:** `git apply --check --3way` للـpatch على طرف `c014328f` **ينجح** (مع fallback للتطبيق المباشر بسبب نقص blobs للـ3-way).

**⚠️ تحديث جوهري — الحزمة انتقلت من «تحتاج تحققاً» إلى «مُثبتة»:** لم أكتفِ بفحص التطبيق، بل **نفّذتها في worktree معزول** (`/tmp/wt-dc`): الـpatch طُبّق فعلياً (11 ملفاً متغيّراً)، واختباراتها شُغّلت، **ومُعيد R1 أُبطل**. الأرقام في ٤.٢ و٦/R1. ما زال صحيحاً أن **فرعها الأصلي مفقود** وأن الـpatch مصدره commit غائب — لكن المحتوى نفسه **قابل للتطبيق ومُختبَر وفعّال**.

### ٣.٤ لقطة `recovery/…tar.gz` — مُتحقَّق أنها لا تحوي عملاً فريداً

استخرجتُ اللقطة (28 ملفاً) وقارنتُ بصمة SHA-256 لكل ملف بكل نسخة مُلتزمة من نفس المسار عبر تاريخ الفرع كاملاً (`git rev-list --reverse`، أي الـ53 commit كلها):

**النتيجة: 28 / 28 ملفاً محتواها مطابق لنسخة مُلتزمة في تاريخ الفرع.**

| المقارنة | النتيجة |
|---|---|
| مطابقة لطرف الفرع `f9e413f` | 12 ملفاً |
| مطابقة لنسخة مُلتزمة **أسبق** | 16 ملفاً |
| **محتوى غير موجود في أي commit** | **0 ملفات** |

أمثلة على الملفات الـ16 ونسخها المطابقة:

| الملف | اللقطة تطابق الفرع عند |
|---|---|
| `utils/timed_ownership.py` | `8bc0a59` (2026-10-26 18:01) |
| `test_timed_ownership.py` | `8bc0a59` (2026-10-26 18:01) |
| `cogs/shop.py` | `8bc0a59` (2026-10-26 18:01) |
| `utils/xp_calculator.py` | `48d7e51` (2026-10-26 15:18) |
| `scripts/recover_shop_purchases.py` | `6adc138` (2026-10-24 18:39) |
| `utils/equip_engine.py` | `86913f6` (2026-10-24 22:44) |
| `utils/level_test_reset.py` | `86913f6` (2026-10-24 22:44) |

**التفسير:** اللقطة أُخذت 2026-10-30T01:45:46Z من شجرة عمل قذرة، أي **قبل** `cb54484` (12:35) الذي أعاد صياغة 9 من تلك الملفات. فاللقطة حالة **سبقت** آخر commits، لا عملاً تجاوزها.

> **الأثر:** قرار تخطّي الـblob الثنائي **لا يُفقد أي عمل**. وهذا يحوّله من «تفضيل تنظيمي» إلى نتيجة مُثبتة.

### ٣.٥ فرع `arena/b5a56831-nilive-bot` — مات الفرع وعاش معظم عمله

`git cherry origin/main origin/arena/b5a56831-nilive-bot` يُظهر الـ**17 commit كلها بعلامة `+`**، أي غير موجودة في `main` بحسب patch-id. **لكن هذا مضلِّل:** العمل دخل `main` عبر **رفع يدوي للملفات** (`fd9c452`، PR #90) لا عبر دمج الفرع، فلا يتطابق patch-id.

**الطريقة:** لكل commit، أخذتُ أسطره المُضافة (كود، >25 محرفاً) وفحصتُ وجودها الحرفي في `main` بـ`git grep -F`.

**تحقق ذاتي من الطريقة:** commit التنسيق `381fd7c` (تحويل em-dash إلى hyphen) يسجّل **0/12**، بينما `Revert` الخاص به `c877cda` يسجّل **12/12**. زوجٌ متعاكس يعطي نتيجتين متعاكستين — فالطريقة تميّز فعلاً ولا تُنتج أرقاماً عشوائية.

| commit | الموضوع | أسطره موجودة في `main` |
|---|---|---|
| `a393bec` | Level Rewards tab scannable | **12/12** ✅ |
| `5be4619` | rename reward audience labels | **10/10** ✅ |
| `99851bf` | B5 follow-up (shared Member/Booster) | **12/12** ✅ |
| `554b8f7` | B3: role-name cache refresh | **7/7** ✅ |
| `9da24ed` | `/level` card reference layout | 11/12 ✅ |
| `a8d91a3` | `/level` card next milestone | 11/12 ✅ |
| `964f014` | B1/B2: exact role IDs | 10/12 ✅ |
| `3fab337` | B4: JSON not HTML 500 | 2/5 ⚠️ |
| `6177cf6` | B5: restore Booster-only roles | 3/12 ⚠️ |
| `3b08ed9` | drop Booster-only roles on last boost | 3/12 ⚠️ |
| `04e16ed` | nero-select: don't cache failed fetch | 1/2 ⚠️ |
| `c682a31` | **U1** refresh in place | **0/12** ❌ |
| `8623355` | **U2** show role names | **0/12** ❌ |
| `8c716e0` | **U3** "no matches" on empty filter | **0/12** ❌ |
| `d00a687` | **scope Level-role progression to audience** | 0/12 — **لكنه موجود فعلياً** (إنذار كاذب، انظر ٣) |
| `381fd7c` / `c877cda` | تنسيق + تراجعه | 0/12 و12/12 — **يلغي أحدهما الآخر، لا أثر** |

**لكن «0/12» لا يعني تلقائياً «العمل ضائع»** — وهذا بالضبط درس D1–D6. فحصتُ الميزات نفسها لا الأسطر:

| الميزة | في `main`؟ |
|---|---|
| U2: أسماء الأدوار (`roleName`) | **نعم — 14 موضعاً**، و`deleted role` موضع واحد |
| U3: تحديث الفلتر عند إفراغ الجدول | **نعم — عبر D3**: `applyRewardFilter()` في الفرع الفارغ للحِمّالات الثلاثة (`leveling.html:1023/1301/1361`) |
| U3: نص «no matches» تحديداً | **لا — 0 موضع** |
| ملفات اختبار U1/U2/U3 | **لا — غائبة عن `main` وعن `c014328f` معاً** |

**الخلاصة العملية:**

1. **معظم عمل `b5a56831` موجود في `main`** — لا حاجة لإنقاذه.
2. **ملفات اختبار U1/U2/U3 الثلاثة موجودة فقط على فرع `b5a56831`** — إن أردتَ الحفاظ عليها كمجموعة اختبارات، فهذا هو مصدرها الوحيد. لم تُنقل إلى `main` ولا إلى `c014328f`.
3. **`d00a687` («scope Level-role progression to the reward audience») — محسوم: موجود في `main`.** كان يسجّل 0/12، لكن ذلك **إنذار كاذب من طريقتي**. فحصتُ المعرّفات المميّزة لا الأسطر، فوجدتُ التوقيع الذي أدخله الـcommit موجوداً **حرفياً** في `main`:

   ```
   origin/main:utils/level_claims.py:628
     async def superseded_role_ids(db, guild_id: int, user_id: int,
                                   level: int, role_id: int, *,
                                   booster: bool = False) -> list[int]:
   ```

   وكذلك في `c014328f` (السطر 631). و`is_booster_ref` موجود 5 مرات في كليهما، و`member_is_booster` 5 مرات، و`booster: bool = False` 3 مرات في `main`. **فالعمل المنطقي منقول بالكامل.**

> **⚠️ تحفّظ منهجي يجب أن يبقى ظاهراً:** مطابقة الأسطر المُضافة **غربال لا دليل**. نتجت عنه إنذارات كاذبة (`d00a687`) لأن المفهوم نزل بأسطر محيطة مختلفة. أرقام العمود «موجودة في `main`» تصلح **لترتيب أولوية الفحص**، لا لإثبات الغياب. كل بند حسمتُه فيها تحققتُ منه ثانياً بالمعرّفات أو بالنص.

> **الأثر على الخطة:** لا شيء يتغيّر. `b5a56831` يبقى خارج نطاق الإنقاذ (متأخر 24 commit عن `main`)، **ولم يعد فيه أي عمل منطقي غير منقول**. القيمة الوحيدة المتبقية هي ملفات اختبار U1–U3 الثلاثة.

---

## ٤. القرارات الفنية

### ٤.١ قرارات مُنفَّذة وموثّقة في الفرع

من `docs/execution-20261030/README.md` — عنوانه الصريح: **«Execution checkpoint — NOT ready for a delivery PR»**.

| القرار | التنفيذ | الاختبار |
|---|---|---|
| **A: لا Drop في المحفظة** | إزالة الزر وأصناف التأكيد والـbuilder والـopener من `cogs/wallet.py` و`wallet.py` | `WalletDeletionSurfaceTests` |
| **B: هوية الـdescriptor الغريب** | الإبقاء على الحد الإنتاجي القائم، لم يُعَد بناؤه | `DescriptorIsolationTests` |
| **C: معاملة ذرّية للجرعة** | `use_potion` تقرأ الـmetadata وتستهلك الكمية/المنشأ وتُدخل الـboost وتُثبت على اتصال واحد | `PotionTransactionTests` (بلا تغيير مخطط) |
| **D: سباق تقدّم المستوى/equip** | `enforce_role_progression` يمسك `slot_lock` القائم | `LevelRemovalSynchronizationTests` — **لا يغطي كل كتّاب الأدوار** |
| **E: هجرة `entry_key` إلزامية** | `init_db` يرفع `RuntimeError` بسبب مُسلسل بدل التسجيل والمتابعة | `EntryKeyMigrationTests` |
| **F: إصلاح harness للـBooster فقط** | `scripts/test_booster_rewards.py` يوفّر `followup` | سلوك المنتج **لم يتغير** |
| **قواعد دائمة** | `AGENTS.md` و`CLAUDE.md` متطابقان بايت-ببايت | `cmp -s` |
| **CI** | الـworkflow يبقى PR-only؛ **لا يُدّعى نجاح CI من دفع فرع** | — |

**حدود صريحة أعلنها الفرع نفسه:**
- نطاق الجرعات: **بلا مفتاح idempotency جديد**. التنشيطات المشروعة المنفصلة قد تستهلك نسخاً مملوكة منفصلة كما سابقاً. الضمان المُثبت هو all-or-nothing في SQLite، **وليس exactly-once خارجياً**.
- القرار D **لا يغطي كل كتّاب الأدوار**.

### ٤.٢ قرار التصميم اللاحق (Design C) — غير مُنفَّذ في الفرع الرئيسي

القاعدة: الوحدة القابلة للتكديس (جرعة/لقب/مخصص) **لا تُسترجع إلا عبر صف `reward_grants` الذي كتبته عملية الشراء نفسها، وبنفس المعاملة**. لا شيء آخر دليل: لا اسم العنصر، لا كمية الكومة، ولا لقطة `delivered_inventory` (تُحفظ للتشخيص فقط — إعادة منح في نفس الثانية تُنتجها بايت-ببايت).

| العنصر | التفصيل |
|---|---|
| `reward_grants.grant_source` | `level_reward` \| `shop_purchase`؛ يُضاف بـ`ALTER` مع backfill `NULL → level_reward` في نفس الاستدعاء |
| `reversed_at` / `reversed_by` | يُضبط مرة واحدة عند الاسترجاع — حارس idempotency |
| `idx_rg_purchase_operation` | `UNIQUE (operation_id) WHERE grant_source='shop_purchase'` — عملية واحدة ⇒ منح واحد |
| ترتيب الاستهلاك | `ORDER BY (grant_source='shop_purchase'), id` — منح المستوى أولاً، ثم منح الشراء. لا FIFO زمني عبر المجموعتين |
| سياسة القديم | **fail-closed**: شراء بلا منح يبقى `legacy_no_provenance`؛ لا يُزيل الوحدة ولا يُفبرك منشأ |
| أداة المشغّل | `--retire-rollback --yes --accept-unprovable --note "≥20 chars"` — لا يُزيل شيئاً |
| محذوف | `utils/reward_engine.rollback_item_inventory_grant` (مقارن اللقطات غير الآمن) |

### ٤.٣ قرارات اتخذتها أنت في هذه الجلسة

| القرار | اختيارك |
|---|---|
| `recovery/…tar.gz` | **تخطٍّ** |
| PR #104 | **إبقاء مفتوحاً** |
| قياس خط الأساس | **قبل B1**، كامل (41 root + 72 scripts) |
| التصنيف والدفعات السبع | **غير معتمدة بعد** — تُناقش بعد مراجعة هذا التقرير |

---

## ٥. الاختبارات — القياس الفعلي الكامل

### ٥.١ الظروف

- **عزل:** `git worktree add --detach /tmp/wt-main origin/main` و`/tmp/wt-c01 origin/arena/c014328f-nilive-bot`. الأرشيف `/home/user/Nilive-Bot` لم يُلمس (بقي عند `a6b812b`، وdirty=1 وهو ملف هذا التقرير فقط).
- **الأمر لكل ملف:** `OWNER_ID=1 PYTHONPATH=. PYTHONFAULTHANDLER=1 timeout -s ABRT <cap> <cmd>`
- **الحد الزمني:** 120s للمصفوفات القصيرة، 180s لـ`scripts/`، 900s لـ`test_timed_ownership.py`.
- **مجموع التشغيلات:** 167 (main) + 174 (c014328f) = **341 تشغيلاً**.

### ٥.٢ ⚠️ خطأ في مشغّل القياس — صُحّح ولا يجوز تجاهله

التشغيل الأول أعطى نتائج **باطلة** لمصفوفتين بسبب خطأ في سكربت القيادة لا في الكود:

| المصفوفة | النتيجة الأولى | السبب |
|---|---|---|
| `scripts_py` | 71/71 و72/72 بـ`exit=2` | مرّر السكربت `scripts/$f` بينما `$f` يبدأ أصلاً بـ`scripts/` ← مسار مضاعف |
| `scripts_node` | 18/18 بـ`exit=1` | نفس الخطأ |
| `root_pytest/script` على `test_timed_ownership.py` | `exit=124` | حدّي 120s **أقصر** من زمنه الفعلي (~153s) |

**الدليل:** `can't open file '/tmp/wt-main/scripts/scripts/test_wallet.py'` و`Cannot find module '/tmp/wt-main/scripts/scripts/test_vi_shop_form.js'`.

**أُعيد تشغيل المصفوفتين بالكامل** بحد 180s، وأُعيد تشغيل `test_timed_ownership.py` بحد 900s. **الجداول أدناه هي نتائج التشغيل المصحَّح فقط.**

### ٥.٣ النتائج المقاسة — `main` @ `a6b812be26d35f3048db39d0425374d23cd804ea`

| المصفوفة | الأمر | ناجح / المجموع |
|---|---|---|
| root / pytest | `pytest -q --tb=no <file>` | **10 / 39** |
| root / script mode | `python <file>` | **25 / 39** |
| `scripts/` Python | `python scripts/<file>` | **67 / 71** |
| `scripts/` Node | `node scripts/<file>` | **18 / 18** |

**الفشل في `scripts/` Python (4):**
`scripts/test_afk.py`، `scripts/test_booster_rewards.py`، `scripts/test_leveling_voice_hard_blocks.py`، `scripts/test_phase2_backend.py`

**الفشل في root/script mode (14):**
`test_afk.py`، `test_afk_dashboard.py`، `test_booster_rewards.py`، `test_check_emoji.py`، `test_currency_icon.py`، `test_heartbeat.py`، `test_phase1_runtime.py`، `test_phase2_backend.py`، `test_prestige.py`، `test_shop_options.py`، `test_shop_publisher.py`، `test_shop_publisher_api.py`، `test_slice3_shop_claims.py`، `test_slice4_boost_claims.py`

**root/pytest — 29 غير ناجح:** 22 بـ`exit=5` (**pytest لم يجمع أي اختبار** — ملفات harness بأسلوب سكربت، متوقع) و7 بـ`exit=1` (`test_minigame_engines.py`، `test_minigame_spawn.py`، `test_minigame_store.py`، `test_phase1_runtime.py`، `test_phase2_backend.py`، `test_shop_options.py`، `test_shop_publisher_api.py`).

### ٥.٤ النتائج المقاسة — `arena/c014328f-nilive-bot` @ `f9e413f72a026ac09e33104f809ea2345753eee7`

| المصفوفة | ناجح / المجموع |
|---|---|
| root / pytest | **12 / 42** |
| root / script mode | **28 / 42** |
| `scripts/` Python | **69 / 72** |
| `scripts/` Node | **18 / 18** |

**الفشل في `scripts/` Python (3):** `scripts/test_afk.py`، `scripts/test_leveling_voice_hard_blocks.py`، `scripts/test_phase2_backend.py`
**الفشل في root/script mode (14):** نفس قائمة `main` تماماً.

**الاختبارات الجديدة في الفرع — كلها ناجحة:**
| الملف | root/pytest | root/script | الزمن |
|---|---|---|---|
| `test_timed_ownership.py` | **241 passed** (exit 0) | exit 0 | 153s / 154s |
| `test_delivery_safety.py` | **15 passed** (+14 subtests) | — | 5.3s |
| `scripts/test_inventory_ui.py` | — | **161 passed** | 2s |

### ٥.٥ مقارنة مع ادّعاء `TEST_BASELINE.md` — محسومة بالقياس لا بالاستنتاج

قستُ عند الـSHA الذي يسمّيه الملف نفسه (`670ffebf1571954537ef4549b2bc850357b4b280`) في worktree معزول ثالث (`/tmp/wt-670`)، بمجموع **172 تشغيلاً**:

| المصفوفة | ادّعاء `TEST_BASELINE.md` | قياسي عند `670ffeb` | النتيجة |
|---|---|---|---|
| root / pytest | 11 / 41 | **11 / 41** | ✅ مطابق تماماً |
| root / script | 27 / 41 | **27 / 41** | ✅ مطابق تماماً |
| `scripts/` Python | 68 / 72 | **68 / 72** | ✅ مطابق تماماً |
| `scripts/` Node | 18 / 18 | **18 / 18** | ✅ مطابق تماماً |

**الادّعاء صحيح بالكامل ومستقل التحقق — في المصفوفات الأربع وفي العددَين (41، 72).** و`test_delivery_safety.py` غير موجود عند ذلك الـSHA، مؤكداً أنه أُضيف لاحقاً في `cb54484`.

الفشل الأربعة في `scripts/` Python عند `670ffeb` هي **نفسها** في `main`:
`test_afk.py`، `test_booster_rewards.py`، `test_leveling_voice_hard_blocks.py`، `test_phase2_backend.py`
وفي root/pytest: 23 ملفاً بـ`exit=5` (لا اختبارات قابلة للجمع — harnesses بأسلوب سكربت) و7 بـ`exit=1`.

#### وبذلك يصبح الفرق عند الطرف مُفسَّراً بالقياس، لا بالاستنتاج

| المصفوفة | `main` | `670ffeb` (أساس الادّعاء) | `c014328f` (الطرف) | سبب الفرق |
|---|---|---|---|---|
| root / pytest | 10 / 39 | **11 / 41** | **12 / 42** | +`test_inventory_ui.py` ثم +`test_delivery_safety.py` |
| root / script | 25 / 39 | **27 / 41** | **28 / 42** | نفسه |
| `scripts/` Python | 67 / 71 | **68 / 72** | **69 / 72** | +`scripts/test_inventory_ui.py`، ثم القرار F يُصلح `test_booster_rewards.py` |
| `scripts/` Node | 18 / 18 | **18 / 18** | **18 / 18** | بلا تغيير |

> **تصحيح لقياسي الأول عند `670ffeb`:** ظهر `test_timed_ownership.py` فاشلاً بـ`exit=124` في المصفوفتين، ثم **نجح** بحد 900s:
> `root_pytest` exit=0 في 78s (**122 passed**)، و`root_script` exit=0 في 81s.
> حدّي الأول (120s) هو السبب، لا الكود. الأرقام أعلاه من التشغيل المصحَّح.
>
> **ملاحظة دالة:** 122 اختباراً عند `670ffeb` مقابل **241** عند الطرف — السلسلة ضاعفت تغطية الملكية المؤقتة تقريباً في الـ28 commit التالية.

### ٥.٦ CI على GitHub — ما هو مثبت وما ليس كذلك

- **`main` لا يحوي `.github/workflows/tests.yml`** (تحققت: `git show origin/main:.github/workflows/tests.yml` يفشل). الـAPI يعرضه لأنه موجود على فروع أخرى.
- **16 تشغيلاً**، كلها على `arena/c014328f-nilive-bot`، غالبيتها `success`.
- **آخر تشغيل ناجح: `head_sha = 3307231` بتاريخ 2026-10-29 16:09.**
- **طرف الفرع `f9e413f` (2026-10-30 12:35) لم يُشغَّل عليه CI قط** — أي 4 commits (`8eb8e3d`, `6c04021`, `cb54484`, `f9e413f`).

**الفجوة مقفلة بتشغيلي المحلي**، لا بادّعاء: القياس أعلاه تم على `f9e413f` نفسه، والمصفوفة الثلاث التي يعتمدها `tests.yml` كبوابة كلها خضراء:

| البوابة | النتيجة |
|---|---|
| `pytest test_timed_ownership.py` | 241 passed |
| `pytest test_delivery_safety.py` | 15 passed |
| `npm test` | 18 harness(es) passed (148/148 checks في `test_shop_publisher_form.js` …) |

### ٥.٧ ما تعذّر تشغيله وأسبابه

| البند | السبب |
|---|---|
| 22 ملف root تحت pytest بـ`exit=5` | **ليست أعطالاً**: pytest لا يجد اختبارات قابلة للجمع في harnesses بأسلوب سكربت. تعمل في وضع السكربت. |
| `docs/execution-20261030/precommit-logs/*` | أدلة تاريخية من تشغيلات سابقة؛ **لم أتحقق من تطابقها مع الحالة الحالية** |
| اختبارات المراجع في `REVIEW/README.md` (14/14، 20/20 jsdom) | أدوات المراجع **غير مضمّنة** في الحزمة عن قصد؛ لا يمكن إعادة تشغيلها |
| ~~حزمة Design C (`test_purchase_grant_ledger.py`)~~ | **أُغلق — شُغّلت ونجحت**: طبّقتُ الـpatch على طرف `c014328f` في worktree معزول (`/tmp/wt-dc`)، 11 ملفاً، ثم `pytest` → **29 passed، exit 0** في 26.15s (٤.٢) |

---

## ٦. المخاطر

### 🔴 مخاطر بيانات (موثّقة من الفرع نفسه، غير مُعالجة)

**R1 — الاسترجاع القديم قد يحذف وحدات شراء أحدث (فقدان بيانات)**
`docs/execution-20261030/reproduce_inventory_ambiguity.py` يخرج بـ**1** مؤكداً **أربع حالات غير آمنة**: الـhelper والـ`compensate_late_delivery` الدائم، كلٌّ مع كومة أحدث من وحدتين وعودة ABA إلى نفس اللقطة. الوحدة الأقدم استُهلكت قبل المنح الجديد. في المسار الدائم يُبلَّغ `complete=True` **بعد حذف وحدات أحدث**.

الفرع يعلن صراحة: *«This diagnostic is a required negative delivery gate… Its location under docs does NOT exclude this failure from release acceptance.»*

**الحالة في `c014328f`:** غير مُعالَج — **وأعدتُ تأكيد ذلك بنفسي** (تشغيل تحكّمي): `reproduce_inventory_ambiguity.py` على `/tmp/wt-c01` يخرج **1** برسالة `BLOCKER: old rollback removed newer units: [('inventory_helper', 2), ('inventory_helper', 1), ('durable_compensation', 2), ('durable_compensation', 1)]`.

**⚠️ لكن المعالجة موجودة ومُثبتة — وهذا تصحيح لتشاؤم المسودة الأولى.** أجريتُ تجربة A/B مضبوطة (نفس الـvenv، نفس المُعيد، شجرتان تختلفان بالـpatch فقط):

| الشجرة | exit | النتيجة |
|---|---|---|
| `c014328f` بلا patch (تحكّم) | **1** | `BLOCKER` — حُذفت وحدات أحدث في 4 حالات |
| `c014328f` + patch حزمة Design C | **0** | `OK: no unprovable deduction on any path` |

في الشجرة المُعالَجة **يُرفَض** الاسترجاع بدل أن يحذف: `Stackable rollback refused [consumed_before_rollback] … taking one now would remove a unit from another source`، مع `inventory_rolled_back: False` و`still_pending: True` و**`safe: True`**. أي أن السلوك تحوّل من «حذف صامت لوحدة مملوكة لغيره» إلى «رفض مُسبَّب + التزام معلّق» — وهو بالضبط ما تصفه سياسة fail-closed في ٤.٢.

> **الأثر على القرار:** R1 لم يعد «خطر بيانات بلا حل معروف». الحل موجود، مُختبَر (29 اختباراً)، ويُثبت إبطال المُعيد. ما يتبقى قرار **ترتيب** الدمج لا قرار **تصميم**.

**R2 — التزام إعادة محاولة Booster/انتهاء المصدر مفقود**
`reproduce_booster_obligation_gap.py` يخرج بـ**1**: مسار Twitch لانتهاء المصدر يحافظ على دور مشترك ملبوس كمكافأة Booster نشطة عندما يكون عدد الـboosts مجهولاً، لكن **الالتزامات الدائمة فارغة**. وبما أن الالتزام الأولي غائب، فدورة الإنشاء/إعادة التشغيل/فشل المحاولة **غير مُثبتة**. و`temp_role_cleanup` **غير مُصادَق على تكافئه** مع `system_cleanup`.

**أعدتُ تأكيد ذلك بنفسي، والنتيجة غير متماثلة مع R1 — وهذا مهم للترتيب:**

| الشجرة | R1 (وحدات أحدث) | R2 (التزام Booster) |
|---|---|---|
| `c014328f` بلا patch (تحكّم) | exit **1** — `BLOCKER: old rollback removed newer units` | exit **1** — `BLOCKER: source ended under unknown Booster eligibility without a durable retry obligation` |
| `c014328f` + Design C | exit **0** — `OK: no unprovable deduction on any path` | exit **1** — **نفس الرسالة حرفياً** |

**أي أن Design C يُصلح R1 ولا يمسّ R2 إطلاقاً.** وهذا يؤكد مستقلاً ادّعاء الفرع نفسه أن R2 «مُصرَّح به في النطاق لكن **غير مُنفَّذ** عند نقطة التنفيذ هذه». لم أكن قد شغّلت R2 بنفسي من قبل — كنت أنقله عن وثائق الفرع، وهذا بالضبط ما طلبتَ ألا أفعله.

> **الأثر على الخطة:** R2 **عمل جديد حقيقي**، لا إنقاذ. لا يوجد في أي فرع ولا في أي حزمة. إن أردت معالجته فهو تطوير من الصفر: تمييز «مجهول» عن «مؤكد»، والتزامات دائمة قبل فقدان سجل المصدر، وإعادة محاولة تحتفظ بالالتزام عند الخطأ/الجهل.

الفرع يعلن: *«This work is authorized in scope but NOT implemented at this checkpoint.»*

### 🟠 مخاطر عملية

**R3 — السلسلة غير مُختبَرة بـCI عند طرفها.** آخر تشغيل ناجح قبل 4 commits. (مغطى محلياً، لكن أي نقل يجب أن يُعاد قياسه بعد كل دفعة.)

**R4 — 3 ملفات `scripts/` فاشلة في الفرع.** `test_afk.py`، `test_leveling_voice_hard_blocks.py`، `test_phase2_backend.py`. **موجودة في `main` أيضاً** — أي أنها فشل سابق لا تسبّب به الفرع. لكنها تعني أن «أخضر بالكامل» غير متاح، وأن البوابة يجب أن تبقى المصفوفة الثلاث.

**R5 — Design C يغيّر مخطط قاعدة البيانات.** أعمدة + فهرس فريد جزئي. **لا يُدمج مع الدفعات الوظيفية** — يبقى دفعة مستقلة.
*تحديث:* الخطر أخفّ مما قدّرته أولاً. الـmigration مُنفَّذة داخل `ensure_tables` بـ`ALTER` + backfill لمرة واحدة، وإعادة التشغيل read-only، **و29 اختباراً تمر عليها فعلياً** (شغّلتها). المتبقي حقاً هو قرار **الترتيب** — بعد استقرار الدفعات الوظيفية، لا معها.

**R6 — blob ثنائي غير مُراجَع.** `recovery/…tar.gz`. قرارك: تخطٍّ — **وقد تحققتُ الآن أنه القرار الصحيح تقنياً، لا مجرد تفضيل**: كل ملفات اللقطة الـ28 مطابقة لنسخة مُلتزمة في تاريخ الفرع (القسم ٣.٤)، فاللقطة لا تحوي عملاً فريداً. يبقى متاحاً في فرعه.

**R7 — `P0-fixes.zip` بلا أساس ولا تاريخ ولا بصمات.** لا يمكن الحكم عليه. يحتاج سياقاً منك.

**R8 — ⚠️ مُصحَّح: حِزم `REVIEW/` ليست بحاجة إلى نقل أصلاً.** كنتُ قد صنّفتها «متعارضة» وطلبتُ إعادة تنفيذ يدوية. **هذا كان خاطئاً.** فشل `git apply --check` سببه أن المحتوى **مدموج في `main` بالفعل** عبر `fd9c452` (رفع يدوي، PR #90) — لا عبر دمج `b5a56831` (تحققت: كل الـ17 commit فيه غير موجودة في `main` بحسب patch-id). التفاصيل والمواضع الدقيقة في ٣.٢. **الخطر الحقيقي هنا كان أن نُعيد تنفيذ عمل موجود، فنُحدث تكراراً أو تراجعاً.**

### 🟢 مخاطر مُخفَّضة

- **لا تعارض مع `main`:** merge-base = طرف `main` بالضبط وbehind = 0، فلا تصادم محتمل.
- **خطّية السلسلة** تجعل `cherry-pick` بنطاقات متصلة آمناً بحكم البناء.
- **الأرشيف سليم ولم يُمسّ.**

---

## ٧. ما يمكن استعادته وما لا يمكن

### ✅ قابل للاستعادة كاملاً

| البند | المقدار | الحالة |
|---|---|---|
| **عمل الاقتصاد/المتجر/المخزون/الملكية المؤقتة/تسليم الأدوار** | 53 commit، 113 ملفاً، +27,655 سطر | على `arena/c014328f-nilive-bot`، بوابة CI الثلاث خضراء بقياسي |
| **`utils/timed_ownership.py`** | 6,770 سطر + 11,244 سطر اختبارات | 241 اختباراً ناجحاً |
| **أداة استعادة المشتريات** | `scripts/recover_shop_purchases.py` | موجودة |
| **CI + خط الأساس** | `.github/workflows/tests.yml` + `TEST_BASELINE.md` | موجودة |
| **حالة الشجرة المتّسخة للجلسة السابقة** | 28 ملفاً، 16,706 سطر | داخل `recovery/…tar.gz` — **⚠️ مُتحقَّق: كل الـ28 ملفاً مطابق لنسخة مُلتزمة في الفرع، فلا عمل فريد فيها** (٣.٤) |
| **Design C (purchase grant ledger)** | 22 ملفاً، +29,626 سطر | على `review/g-purchase-grant-ledger`، والـpatch يُطبَّق على طرف `c014328f` |
| **إصلاح reload العملة** | 17 سطر | يُطبَّق نظيفاً على `main` |
| **كل الأدلة والتقارير** | `docs/execution-20261030/`، `REVIEW/`، `_install/` | سليمة |

### ❌ غير قابل للاستعادة

| البند | السبب |
|---|---|
| **شجرة التكامل `f39ec24…`** | غائبة محلياً وفي 791 commit وعلى GitHub (`HTTP 422`) |
| **الأب `0a26459…`** | نفس النتيجة |
| **فرع `arena/pr3-delivery-fixes`** | غير موجود على GitHub؛ لم يُدفَع قط |
| **commit `3768d96` و`09d3778`** | غائبان |
| **تاريخ ما قبل `a6b812b` في الأرشيف** | الأرشيف ضحل بعمق ١ — لكن التاريخ كامل على GitHub (791 commit) |
| **مسارات الحفظ القديمة** | `/home/user/nilive-preservation-20261030`، `/home/user/Nilive-execution`، `/home/user/nilive-test-env` — غير موجودة هنا |
| **أدوات قياس المراجع في `REVIEW/`** | غير مضمّنة عن قصد |

### ⚠️ قابل للاستعادة بشروط

| البند | الشرط |
|---|---|
| **R1 (حذف وحدات أحدث)** | **⚠️ مُصحَّح: الحل مُثبت لا مُقترَح.** Design C يُبطل المُعيد (A/B: exit 1 → exit 0) و29 اختباراً ناجحاً. المتبقي قرار **ترتيب** الدمج، لا قرار تصميم. انظر ٤.٢ و٦/R1 |
| **R2 (التزام Booster)** | مُصرَّح به في النطاق لكن **غير مُنفَّذ** — عمل جديد |
| **مفاهيم `REVIEW/D1-D6` و`P1`** | **⚠️ مُصحَّح: لا حاجة لأي نقل — مدموجة في `main` بالفعل** عبر `fd9c452` (PR #90). انظر ٣.٢ |
| **فروع `b5a56831` و`ae120c69`** | متأخرتان 24 و17 commit — تحتاج إعادة أساس، أي عمل جديد لا إنقاذ |

---

## ٨. إعادة الإنتاج

```bash
# ١. السلامة
cd /home/user/Nilive-Bot
git fsck --full --strict                    # نظيف
git status --porcelain | wc -l              # 0 (عدا ملف هذا التقرير)
git rev-parse HEAD^{tree}                   # d0edba20c252ab780b7b43013e216ce36cf8db56
cat .git/shallow                            # a6b812b…  ← ضحل بعمق ١
git rev-list --all --count                  # 1

# ٢. المعرّفات المفقودة
git cat-file -e f39ec2476ff1b8b70ed53bd9ba96b112a41c6317   # يفشل
gh api repos/Akuroii/Nilive-Bot/commits/f39ec2476ff1b8b70ed53bd9ba96b112a41c6317   # HTTP 422
gh api repos/Akuroii/Nilive-Bot/commits/0a2645977f09bfa8beb7722a3efe7f8a0fe439f8   # HTTP 422

# ٣. استنساخ جنائي منفصل (الأرشيف لا يُمسّ)
git clone https://github.com/Akuroii/Nilive-Bot /tmp/fx
git -C /tmp/fx rev-list --all --count       # 791
git -C /tmp/fx merge-base --is-ancestor origin/arena/884b26a8-nilive-bot \
                                          origin/arena/c014328f-nilive-bot   # 0 = نعم
git -C /tmp/fx log --merges origin/main..origin/arena/c014328f-nilive-bot    # فارغ = خطّي

# ٤. بيئة الاختبار
python3 -m venv /tmp/fxvenv
/tmp/fxvenv/bin/pip install -r requirements.txt pytest

# ٥. قياس معزول (ثلاثة worktrees، الأرشيف لا يُمسّ)
git -C /tmp/fx worktree add --detach /tmp/wt-main origin/main
git -C /tmp/fx worktree add --detach /tmp/wt-670 670ffebf1571954537ef4549b2bc850357b4b280
git -C /tmp/fx worktree add --detach /tmp/wt-c01  origin/arena/c014328f-nilive-bot
# لكل ملف:
OWNER_ID=1 PYTHONPATH=. PYTHONFAULTHANDLER=1 timeout -s ABRT 180 \
  /tmp/fxvenv/bin/python -m pytest -q --tb=no <file>
# ⚠️ الحد الزمني مهم: test_timed_ownership.py يحتاج 78–154s حسب الـcommit.
#    حد 120s يُظهره زوراً بـexit=124. استُخدم 900s لهذا الملف تحديداً.

# ٦. بوابات CI (على الطرف f9e413f)
OWNER_ID=1 PYTHONPATH=. pytest -q test_timed_ownership.py    # 241 passed (153s)
OWNER_ID=1 PYTHONPATH=. pytest -q test_delivery_safety.py    #  15 passed
npm test                                                     # 18 harnesses passed

# ٧. حالة CI الفعلية
gh api "repos/Akuroii/Nilive-Bot/actions/runs?per_page=20" \
  --jq '.workflow_runs[] | "\(.head_sha[0:9]) \(.conclusion) \(.created_at[0:16])"'

# ٨. قابلية تطبيق الـpatches على main
git apply --check _install/designer-publication-fixes.patch  # يفشل (موجود مسبقاً)
git apply --check REVIEW/D1-D6.diff                          # يفشل
git apply --check REVIEW/P1_expired_processing.diff          # يفشل
git apply --check economy-currency-livereload.patch          # ينجح ✅
```

**ملاحظة على القياس:** تشغيلان أوليان كانا باطلين بسبب خطأ في سكربت القيادة (مسار مضاعف `scripts/scripts/…`) وحدّ زمني أقصر من اللازم (120s مقابل ~153s). أُعيد التشغيل وأُدمجت النتائج المصحَّحة. التفاصيل في ٥.٢. **هذا الإفصاح جزء من النتيجة، لا حاشية.**

---

## ٩. ما لم يُتحقَّق منه (صراحةً)

| البند | الحالة |
|---|---|
| ~~القياس عند `670ffeb` لمطابقة `TEST_BASELINE.md`~~ | **أُجري وأُغلق** — 172 تشغيلاً، المصفوفات الأربع مطابقة تماماً (٥.٥) |
| ~~محتوى `recovery/…tar.gz`~~ | **أُغلق** — استُخرجت الـ28 ملفاً وقورنت بكل نسخ الفرع الملتزمة: 28/28 مطابقة، لا عمل فريد (٣.٤) |
| تطابق `precommit-logs/` مع الحالة الحالية | **لم يُفحص** |
| ~~تشغيل `test_purchase_grant_ledger.py` من حزمة Design C~~ | **أُغلق** — طُبّق الـpatch على طرف `c014328f` وشُغّل: **29 passed، exit 0**. والمُعيد R1 تحوّل من exit 1 إلى exit 0 (٤.٢، ٦/R1) |
| هل دخل عمل `b5a56831` إلى `main`؟ | **أُغلق: دخل معظمه، وبقيت بقايا محددة.** `git cherry` يُظهر الـ17 commit كلها غير موجودة بحسب patch-id، لكن الفحص بمطابقة الأسطر المُضافة يُظهر أن **الفرع مات وعاش عمله** — دخل عبر رفع يدوي (`fd9c452`، PR #90) لا عبر دمج. التفاصيل في ٣.٥ |
| CI على GitHub عند الطرف `f9e413f` | **غير موجود أصلاً** — استُعيض بتشغيل محلي |
| أصل `P0-fixes.zip` ومقصده | **مجهول** — لا أساس ولا بصمات |

---

## ١٠. حالة الأرشيف بعد هذه الجلسة

| البند | القيمة |
|---|---|
| `HEAD` | `a6b812be26d35f3048db39d0425374d23cd804ea` (بلا تغيير) |
| بصمة الشجرة | `d0edba20c252ab780b7b43013e216ce36cf8db56` (بلا تغيير) |
| الملفات المتتبَّعة | 614 (بلا تغيير) |
| `git status --porcelain` | سطر واحد: `?? NILIVE_SESSION_HANDOFF.md` و`?? RECOVERY_INVENTORY_2026-10-31.md` |
| تعديل على مصدر | **صفر** |
| push / PR / دمج | **صفر** |
| `reset` / `clean` / `force push` | **صفر** |

كل الفحص الجنائي والقياس جرى في `/tmp/fx` و`/tmp/wt-main` و`/tmp/wt-c01` و`/tmp/fxvenv`.

---

## ١١. الخطوة التالية

**متوقف هنا بانتظار مراجعتك.** لم يُعتمد تصنيف، ولم تُنفَّذ دفعة، ولم يُنشأ فرع إنقاذ.

الملف المرافق `RECOVERY_INVENTORY_2026-10-31.md` يحوي التصنيف المقترح (ست فئات) وخطة الدفعات السبع — **كلاهما مقترح غير معتمد** وفق ردّك الصريح: «لا تفترض موافقتي المسبقة على أي خطة».
