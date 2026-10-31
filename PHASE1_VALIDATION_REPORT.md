# Phase 1 — Isolated Validation Report

**التاريخ:** 2026-10-31
**النطاق المأذون:** اختبار وتحقق فقط. لا دمج، لا نطاق دمع مُعتمَد.
**الحكم:** البند ١٠.

---

## ⚠️ الخلاصة التي تقلب توصية المرحلة ٠

في المرحلة ٠ أوصيتُ بأن **PR #104 هو «أصغر نطاق آمن»**. القياس الفعلي في هذه المرحلة **يدحض ذلك**:

> **PR #104 يُدخل ٣ توافيق اختبار فاشلة تنجح في `main` وتنجح في السلسلة الكاملة.**
> **بينما السلسلة الكاملة + Design C مجموعة فشلها مطابقة لمجموعة فشل `main` حرفياً — لا انحدار إطلاقاً.**

التوصية الصحيحة انعكست: **السلسلة الكاملة + Design C هي وحدة الدمج الآمنة، لا PR #104 وحده.**

---

## ١. البيئة والعزل — مُتحقَّق

| العنصر | القيمة |
|---|---|
| نسخ العمل | `/tmp/p1-main` @ `a6b812b` · `/tmp/p1-pr104` @ `baa6883` · `/tmp/p1-chain` @ `f9e413f` · `/tmp/p1-dc` = `f9e413f` + Design C |
| البيئة | `/tmp/p1venv` — Python 3.11.2، discord 2.7.1، aiosqlite 0.19.0، pytest 9.2.0 |
| عزل القاعدة | `phase1_support.py` يفرض `assert Path(DB_PATH) == DB_FILE, "Refuse to run against a non-test database"` |
| مسار الإنتاج | `/app/data` **غير موجود** في هذه البيئة |
| ملفات `.db` في نسخ العمل | **صفر** — كل الاختبارات على scratch مؤقت |
| عمليات Discord حقيقية | **صفر** — كل الحدود الخارجية مُزيَّفة صراحةً |

**سلامة الأرشيف الأصلي بعد كل التشغيل:**

```
HEAD:              a6b812be26d35f3048db39d0425374d23cd804ea
tree:              d0edba20c252ab780b7b43013e216ce36cf8db56
tracked:           614
تعديل على المصدر:  0
commits على الفرع: 1
refs بعيدة للفرع:  0
worktrees عليه:    1
PR #104:           OPEN @ baa6883a286551dfbd5b1837c8bcc038cf7897e2
```

**كل العمل جرى في `/tmp`، خارج المستودع.** لم يُنشأ commit أو PR، ولم يُدفع شيء، ولم يُدمج، ولم يُنشر. PR #104 ما زال مفتوحاً. لم يبدأ B0 ولا B1. لم يُصلَح R2.

---

## ٢. البوابات المُسمّاة — كلها شُغّلت فعلياً الآن

| البوابة | SHA | النتيجة | exit |
|---|---|---|---|
| `test_timed_ownership.py` | `f9e413f` (السلسلة) | **241 passed** + 5 subtests · 130.09s | 0 |
| `test_timed_ownership.py` | `f9e413f` + Design C | **241 passed** + 5 subtests · 135.95s | 0 |
| `test_timed_ownership.py` | `baa6883` (PR #104) | **34 passed** · 11.95s | 0 |
| `test_delivery_safety.py` | `f9e413f` | **15 passed** + 14 subtests · 5.16s | 0 |
| `test_inventory_ui.py` (وضع سكربت) | `f9e413f` | **161 passed, 0 failed** | 0 |
| `test_inventory_ui.py` (وضع سكربت) | `baa6883` | **161 passed, 0 failed** | 0 |
| `test_purchase_grant_ledger.py` | `f9e413f` + Design C | **29 passed** · 12.61s | 0 |
| `npm test` | `f9e413f` | **18 harness(es) passed** | 0 |

**ملاحظة على فرق التغطية:** `test_timed_ownership.py` عند PR #104 يحوي **34 اختباراً** مقابل **241** عند السلسلة. الـ7 commits لا تحوي أعمال التقسية الـ46.

**ملاحظة منهجية:** `test_inventory_ui.py` ليس وحدة pytest بل سكربت ذو `__main__` في `:1093`. تشغيله بـ`pytest` يعيد `no tests ran` / exit 5 — وهو **ليس فشلاً**. شُغّل بالوضع الصحيح.

---

## ٣. المصفوفة الكاملة — النتيجة المحورية

**١١٤ بوابة لكل حالة** (جذر + `scripts/`)، ملف بملف، ~810 ثانية:

| الحالة | PASS | FAIL | SKIP | ملفات فاشلة فريدة |
|---|---|---|---|---|
| `main` @ `a6b812b` | 83 | 24 | 3 | **21** |
| `baa6883` (PR #104) | 83 | 27 | 3 | **23** |
| `f9e413f` (السلسلة) | 88 | 23 | 3 | **21** |

### ٣.١ السلسلة الكاملة: **صفر انحدار**

```
diff F-main.txt F-chain.txt  →  متطابقتان
```

مجموعة فشل السلسلة **مطابقة حرفياً** لمجموعة فشل `main`. لا تُدخل فشلاً جديداً، ولا تُصلح فشلاً قديماً في هذه البوابات.

### ٣.٢ PR #104: **انحداران حقيقيان**

`comm -23 F-pr104 F-main` →

```
test_free_shop_items.py
test_vi_shop_api.py
```

ثلاثة توافيق تفشل في PR #104 وتنجح في `main` **وفي السلسلة**:

| الملف | الوضع | `main` | `baa6883` | `f9e413f` |
|---|---|---|---|---|
| `test_vi_shop_api.py` (الجذر) | pytest | PASS | **FAIL** (4 failed, 5 passed) | PASS |
| `scripts/test_vi_shop_api.py` | script | PASS | **FAIL** (failures=4) | PASS |
| `scripts/test_free_shop_items.py` | script | PASS | **FAIL** | PASS |
| `test_free_shop_items.py` (الجذر) | pytest | PASS | PASS (24 passed) | PASS |

**أُعيد إنتاج كل فشل معزولاً** — فليس أثر ترتيب اختبارات داخل المصفوفة.

### ٣.٣ سبب الانحدار — مُثبت بالبصمات

```
test_vi_shop_api.py   على main:    8972856e08dcb204fe1af431a1e87c1148605481
test_vi_shop_api.py   على baa6883: 8972856e08dcb204fe1af431a1e87c1148605481   ← مطابق
utils/shop_validation.py على main:    1c9bf742e3017239df91ef3da3786aa2df4ca080
utils/shop_validation.py على baa6883: 331728a10b66032236a04c7f1d350d9d05f3f41d ← مختلف
```

**PR #104 شدّد `utils/shop_validation.py` دون تحديث الاختبار الذي يغطّيه.** رسائل الرفض الفعلية:

```
AssertionError: 400 != 200 : {'error': 'Titles are permanent and cannot have a duration.'}
AssertionError: 400 != 200 : {'error': 'Only potions and XP boosts have an effect multiplier.'}
AssertionError: 400 != 200 : {'error': 'A potion cannot have a role ID.'}
```

وفي `scripts/test_free_shop_items.py`:
```
AssertionError: Lists differ: [] != [('temp_role',)]
  في test_every_supported_item_type_reaches_its_normal_reward_path_for_free (kind='temp_role')
  و test_unlimited_free_temp_role_stacks_exactly_like_a_paid_one
```

**من يُصلحه:** `7283809` — **أول** commit في الـ46 (2026-10-24 18:10) — يحدّث `test_vi_shop_api.py` و`scripts/test_vi_shop_api.py` (31 سطراً لكل منهما، +40/−22).

### ٣.٤ فشل `main` القائم أصلاً — 21 ملفاً، ليس سببه الدمج

```
test_afk.py                  test_afk_dashboard.py       test_booster_rewards.py
test_check_emoji.py          test_currency_icon.py       test_heartbeat.py
test_leveling_voice_hard_blocks.py                       test_minigame_engines.py
test_minigame_spawn.py       test_minigame_store.py      test_phase1_runtime.py
test_phase2_backend.py       test_prestige.py            test_shop_emoji.py
test_shop_emoji_tls.py       test_shop_options.py        test_shop_publications.py
test_shop_publisher.py       test_shop_publisher_api.py  test_slice3_shop_claims.py
test_slice4_boost_claims.py
```

هذه **قائمة في `main` قبل أي دمج**، ويجب ألا تُحسب على المشروع المسترجَع.

---

## ٤. R1 — المصفوفة الرباعية الكاملة

| الاختبار | الشجرة | النتيجة | exit |
|---|---|---|---|
| **R1-A** المُعيد الأصلي | `f9e413f` غير مُرقّع | `BLOCKER: old rollback removed newer units: [('inventory_helper',2),('inventory_helper',1),('durable_compensation',2),('durable_compensation',1)]` | **1** |
| **R1-B** المُعيد الأصلي | + Design C | `ImportError: cannot import name 'rollback_item_inventory_grant'` | **1** |
| **R1-C** نسخة الحزمة | + Design C | `OK: no unprovable deduction on any path` | **0** |
| **R1-D** نسخة الحزمة | غير مُرقّع | `ImportError: cannot import name 'REFUSAL_NO_PROVENANCE' from 'utils.level_ops'` | **1** |

### ٤.١ تصحيح على تقريري السابق

تقرير المرحلة ٠ قال «patched exit 0» للمُعيد الأصلي. **هذا خطأ.** المُعيد الأصلي على الشجرة المُرقّعة يعطي **exit 1** بـ`ImportError`.

### ٤.٢ الفرق الدلالي الحقيقي بين النسختين

النسختان **ليستا قابلتين للتبادل** — نطاقا صلاحيتهما منفصلان:

| | المُعيد الأصلي | نسخة الحزمة |
|---|---|---|
| الحجم / sha256 | 3,099 B · `008bf870…` | 5,671 B · `d1b31967…` |
| يستورد | `rollback_item_inventory_grant` | `REFUSAL_NO_PROVENANCE`, `REFUSAL_CONSUMED` |
| يعمل على | الكود **قبل** الترقيع فقط | الكود **بعد** الترقيع فقط |
| آلية الحكم | يفشل إن حُذفت وحدات أحدث | يفشل إن وُجدت الدالة القديمة (`HELPER_REMOVED`) |

**فـ`ImportError` في R1-B وR1-D ليس فشلاً بل دليل:** الأول يثبت أن Design C **حذف** الدالة المعرّضة (`def rollback_item_inventory_grant`: 1 → **0**)، والثاني يثبت أنه **أضاف** ثوابت الرفض.

### ٤.٣ سلوك Design C الفعلي عند الرفض

```
legacy:     Stackable rollback refused [legacy_no_provenance]: purchase … has no grant
            record: it was delivered before purchase provenance existed, and
            inventory_items merges units from every source into one quantity, so the
            unit cannot be shown to be this purchase's; resolve by hand
provenance: Stackable rollback refused [consumed_before_rollback]: this purchase granted
            1 unit(s) and 0 are still attributable — the unit was used, traded or dropped
            before the rollback, so taking one now would remove a unit from another source
```
مع `inventory_rolled_back: False`, `still_pending: True`, `safe: True` — **يرفض ولا يخصم**.

---

## ٥. R2 — التزام Booster (لم يُصلَح، كما طُلب)

**التشغيل:** exit **1**
```
AssertionError: BLOCKER: source ended under unknown Booster eligibility
                without a durable retry obligation
```

### ٥.١ السيناريو الذي يُضيع الالتزام

`_booster_level_for` تعيد `(1, False)` عند `premium_since` بلا عدد لكل عضو:
> «a live `discord.Member` with `premium_since` set and no per-member count attribute: returns `(1, False)`»

ثم في `booster_role_entitlements`:
```python
if boost_level >= 2 or (not boost_count_known and b2_id in held_role_ids):
    entitled["reward"].add(b2_id)
```

الشرط الثاني **تخمين من الدور الملبوس**: «هو يلبسه، فلا بد أنه مؤهل». النتيجة:
1. `role_removal_blocked` تعيد سبباً ← **الدور يُحفَظ** (fail-closed صحيح)
2. لكن **لا يُكتب أي التزام دائم لإعادة الفحص**

**الدليل على غياب الالتزام:** `INSERT INTO timed_holdings` وحيد في `:252` ولا علاقة له بذلك. والمُجدوِلات الخمسة — `expired_holdings:1389`, `reconcile_equip_for_expired_holding:1465`, `expire_one:1857`, `reconcile_delivery_rollbacks:5201`, `reconcile_purchases:5349` — **لا تعيد فحص أهلية Booster**.

**فالدور يبقى إلى الأبد على تخمين لا يُعاد التحقق منه.**

### ٥.٢ المسارات المتأثرة — 9 مواضع استدعاء

| الملف | المواضع |
|---|---|
| `cogs/boost.py` | `:161`, `:184`, `:208`, `:429` |
| `cogs/mvp.py` | `:191` |
| `cogs/twitch.py` | `:321` |
| `utils/level_claims.py` | `:760`, `:1105` |
| `utils/prestige.py` | `:635` |

### ٥.٣ متطلبات الإغلاق (تحديد فقط، لا تنفيذ)

1. عند `not boost_count_known` والاعتماد على `held_role_ids`، كتابة صف التزام في `timed_holdings` يحدد `role_id` + السبب + وقت إعادة الفحص.
2. مُجدوِل دوري يعيد تقييم الأهلية ويسحب الدور عند سقوطها.
3. اختبارات: (أ) انتهاء المصدر بعدد مجهول ⇒ التزام دائم موجود؛ (ب) المُجدوِل يسحب الدور بعد سقوط الأهلية؛ (ج) المُجدوِل لا يسحبه مع استمرار الأهلية؛ (د) عدم تكرار الالتزام لنفس `(guild,user,role)`.
4. إثبات أن مُعيد `reproduce_booster_obligation_gap.py` يصبح exit 0.

---

## ٦. القرار A — مُثبت تشغيلياً، مع تحذير مهم

الفرق حرفي بين الشجرتين:
```diff
-    check("a permanent Color still offers Drop",
-          "Drop" in [c.label for c in perm_view.children
+    check("a permanent Color never offers Drop",
+          "Drop" not in [c.label for c in perm_view.children
```

**تحذير:** خُضرة `161 passed` عند `f9e413f` **لا تُثبت أن حذف Drop مُعتمَد** — بل تُثبت أن **الاختبار عُدّل ليطابق السلوك الجديد**. فاللون الأخضر هنا دليل على الاتساق الداخلي، لا على الموافقة على قرار المنتج.

حُذف بواسطة `cb54484` (خارج PR #104). الدالة `drop_item` **باقية** في `utils/inventory.py:177` و`inventory.py:123`.

---

## ٧. القرار E — الشروط الخمسة مُتحقَّقة

شُغّل على قاعدة **يمكن التخلص منها** (`/tmp/phase1_e_*`)، لا على الإنتاج.

| # | الشرط | النتيجة |
|---|---|---|
| 1a | نجاح الهجرة | `entry_key added=True`، الصفوف محفوظة (4)، `integrity=ok`، وإعادة التشغيل **idempotent** |
| 1b | فشل الهجرة يُحبط الإقلاع | `raised RuntimeError` |
| 2 | لا حالة جزئية | `entry_key absent=True`، `rows intact=True (4)`، `integrity=ok` |
| 3 | رسالة واضحة بلا أسرار | `message names migration=True`، النص: `Required migration equipped_roles.entry_key failed; startup aborted to protect ownership identity`، `secret leaked=False` |
| 4 | نسخة احتياطية قابلة للاسترجاع | `restore from backup: True` |

**exit 0 — «OK: all Decision E operational conditions verified»**

### ٧.١ خلل في أداتي كاد يُنتج استنتاجاً خاطئاً — موثَّق للأمانة

**التشغيل الأول فشل**: «error message does not name the failing migration»، والرسالة كانت `database is locked`.

**السبب أداتي لا منتَجِي:** مسار النجاح أنشأ `RUN.db-wal` و`RUN.db-shm`، ثم `shutil.copy2` استبدل `RUN.db` **وترك الملفين الجانبيين**، فقراءت SQLite WAL غير مطابق ومات `init_db()` **قبل بلوغ `ALTER`**. فأصلحتُ بـ`fresh_run()` التي تحذف `""`/`-wal`/`-shm`/`-journal` قبل النسخ، وأعدتُ التشغيل.

**الدرس:** فشل اختبار قد يكون خطأً في أداة الاختبار. التحقق من السبب قبل الاستنتاج منعني من الإبلاغ عن عطل غير موجود.

### ٧.٢ البند ٥ — سلوك العملية عند فشل الإقلاع: **مقيس تجريبياً**

شغّلتُ `main.py` و`dashboard/app.py` فعلياً مع هجرة فاشلة على قاعدة يمكن التخلص منها (`/tmp/verify_exit_code.py` و`/tmp/verify_dashboard_crash.py`).

**العملية ١ — البوت (`main.py`، في الخلفية):**
```
Initializing database...
FATAL ERROR: Required migration equipped_roles.entry_key failed; startup aborted to protect ownership identity
[result] process exit code = None        ← خرج بالرمز 0
```
`main()` يلتقط الاستثناء في `except Exception as e:` (السطر 355) فيطبع الخطأ و**لا يستدعي `sys.exit(1)`** بخلاف معالج `LoginFailure` (السطر 354) الذي يفعل. فالبوت لا يقلع — لكن **رمز خروجه 0**، أي أن الفشل مُقنَّع كـ«نجاح» أمام المُشرِف.

**العملية ٢ — حلقة الإشراف (`start.sh`):**
```bash
supervise_bot() {
    while true; do
        python -u main.py 2>&1
        exit_code=$?                                  # يُقرأ ثم يُطبع فقط
        echo "[supervisor] Bot exited with code $exit_code — restarting in 5s..."
        sleep 5
    done
}
```
الحلقة **تتجاهل رمز الخروج** وتُعيد التشغيل كل ٥ ثوانٍ للأبد. فالرمز 0 لا يعني موتاً صامتاً — يعني **حلقة انهيار للبوت**.

**العملية ٣ — اللوحة (`dashboard/app.py`، في المقدمة):**
```
dashboard/app.py:99:  run_async(init_db())
→ uncaught RuntimeError: Required migration equipped_roles.entry_key failed; startup aborted...
```
`init_db()` يُستدعى **على مستوى الوحدة** (السطر 99)، فتموت اللوحة باستثناء غير ملتقط. وهي العملية **الأمامية** في `start.sh` ⇒ `start.sh` ينتهي ⇒ **الحاوية تخرج** ⇒ سياسة إعادة التشغيل في Railway **ستُفعَّل**.

**الصورة الكاملة المقيسة:**

| الطبقة | السلوك | الأثر |
|---|---|---|
| `main.py` | يبتلع الخطأ، يخرج **0** | البوت لا يقلع، والرمز يُخفي الفشل |
| `supervise_bot` | يُعيد كل ٥ ثوانٍ، يتجاهل الرمز | حلقة انهيار مستمرة للبوت |
| `dashboard/app.py` | استثناء غير ملتقط عند `:99` | العملية الأمامية تموت ⇒ **الحاوية تخرج** |
| **الصافي** | **حلقة انهيار على مستوى الحاوية** | Railway يُعيد التشغيل، لكن **لا شفاء ذاتي** |

**الاستنتاج التشغيلي:** الفشل **يُعيد تشغيل الخدمة لكنه لا يُصلحها** — كل إعادة تحاول الهجرة نفسها ويفشل. الخدمة تبقى متوقفة حتى تدخل يدوي.

**تحفّظان مهمان:**
1. **هذا نمط قائم، لا انحدار من الدمج:** `except Exception as e:` بلا `sys.exit(1)` موجود على `main` أيضاً، وحلقة `supervise_bot` كذلك. ما يُدخله الدمج هو **إمكانية الوصول** — هجرة `entry_key` جديدة تجعل هذا المسار قابلًا للتفعيل.
2. **سياسة إعادة تشغيل Railway نفسها غير مُتحقَّقة:** نطاق Railway ليس متاحاً لي. قِستُ **ما يصل إلى السياسة** (الحاوية تخرج برمز غير صفري)، لا **استجابتها**. المطلوب من Akuroi: تأكيد أن سياستها تُعيد تشغيل حاوية خارجة برمز غير صفري، وخطة التراجع إلى إصدار سابق.

### ٧.٣ خلل أداتي ثانٍ كاد يُنتج استنتاجاً خاطئاً

قياسي الأول للوحة أعاد `SystemExit(1)` — بدا كأنها تموت برمز صحيح. **لكن السبب كان حارساً قائماً أصلاً:** `SECRET_KEY` مفقود ⇒ `SystemExit(1)` عند السطر 51 **قبل** بلوغ `init_db` في السطر 99. فقِستُ حارساً آخر لا الهجرة. صحّحتُ اسم المتغير (`SECRET_KEY` لا `FLASK_SECRET_KEY`) وأعدتُ.

**الدرس نفسه من §٧.١ يتكرر:** عندما يخرج القياس مبكراً، تحقّق من **أي** سطر توقف عنده قبل أن تستنتج عن السلوك المطلوب.

---

## ٨. حدود تزامن الأدوار — بلا إعادة هيكلة

### ٨.١ تصحيح قياس

قياسي الأول («60 موضع `remove_roles`») كان **مضخَّماً** — المستودع يحمل نسخاً مكررة في الجذر وفي `cogs/`/`utils/`. القياس الصحيح على **المسارات القانونية فقط**:

| القياس | القيمة |
|---|---|
| `remove_roles` في `cogs/` + `utils/` | **39** |
| `role_removal_blocked` فيها | **19** |

### ٨.٢ نسخ الجذر كود ميت

`main.py` يحمّل `cogs.*` فقط (`await bot.load_extension(cog)` على قائمة `"cogs.shop"`, `"cogs.twitch"`, …). ولا يستورد أي ملف قانوني نسخ الجذر:

| النسخة | مقابل | الحكم |
|---|---|---|
| `reactionroles.py` | `cogs/reactionroles.py` | ✓ مطابق |
| `customcommands.py` | `cogs/customcommands.py` | ✓ مطابق |
| `boost.py` | `cogs/boost.py` | ✗ **يختلف** |
| `shop.py` | `cogs/shop.py` | ✗ **يختلف** |
| `equip_engine.py` | `utils/equip_engine.py` | ✗ **يختلف** |
| `xp_calculator.py` | `utils/xp_calculator.py` | ✗ **يختلف** |

**تحذير:** أربع نسخ جذر **تختلف** عن نظيراتها القانونية وهي غير مُحمَّلة. أي قراءة لها تُنتج قياساً خاطئاً — وهو ما وقعتُ فيه.

### ٨.٣ المحمي فعلياً بـ`slot_lock` — 7 مواضع

```
cogs/shop.py:1272              utils/equip_engine.py:94
utils/level_claims.py:721      utils/timed_ownership.py:1851
utils/timed_ownership.py:2343  utils/timed_ownership.py:4124
utils/timed_ownership.py:4468
```
التعريف في `utils/equip_engine.py:80`. و`utils/level_claims.py:718` يحذّر: *«This public entry point must not be called while holding slot_lock already»* — **القفل غير قابل لإعادة الدخول**، لذا أي إعادة هيكلة واسعة خطيرة.

**غير مُثبَت:** `cogs/reactionroles.py` (7 مواضع `remove_roles`) و`cogs/customcommands.py` (2) — **صفر** `role_removal_blocked`.

---

## ٩. تصحيحات على ادعاءاتي السابقة

| # | ما قلته | الواقع المُثبت |
|---|---|---|
| 1 | «PR #104 أصغر نطاق آمن» | **خاطئ** — يُدخل 3 توافيق فاشلة؛ السلسلة الكاملة صفر انحدار |
| 2 | «PR #104 متسق ذاتياً» | **خاطئ** — شدّد `shop_validation.py` دون تحديث اختباره؛ يفشل عند طرفه |
| 3 | «R1 patched exit 0» (للمُعيد الأصلي) | **خاطئ** — exit 1 بـ`ImportError`؛ exit 0 لنسخة الحزمة فقط |
| 4 | «60 موضع `remove_roles`» | **مضخَّم** — 39 على المسارات القانونية؛ الفرق نسخ جذر ميتة |
| 5 | «`economy_safe.py`/`xp_calculator.py` قابلتان للاستبعاد» | **خاطئ للسلسلة الكاملة** — الاقتران عبر التوقيع (`safe_decrement_stock(…, db=)`, `grant_xp_boost_details`) |
| 6 | «فشل القرار E: الرسالة لا تسمّي الهجرة» | **خطأ أداتي عندي**، لا عطل منتَجِي — صُحّح وأعيد |

---

## ١٠. الحكم

### أي المسارين أكثر أماناً؟

> **السلسلة الكاملة (`f9e413f`) + Design C — وليست PR #104 وحده.**

**السبب القاطع:** مجموعة فشل السلسلة **مطابقة حرفياً** لمجموعة فشل `main` (21 ملفاً، كلها قائم أصلاً). بينما PR #104 يُضيف ملفين فاشلين (`test_vi_shop_api.py`, `test_free_shop_items.py`) يُصلحهما أول commit في الـ46.

**دمج PR #104 وحده يعني شحن انحدار معروف إلى الإنتاج.**

### ما اجتاز

| البند | الحالة |
|---|---|
| بوابات Design C (29) | ✅ |
| `test_timed_ownership` (241) على السلسلة **ومع** Design C | ✅ |
| `test_delivery_safety` (15) | ✅ |
| `test_inventory_ui` (161) على الشجرتين | ✅ |
| `npm test` (18 harness) | ✅ |
| R1 محجوب (R1-C exit 0) | ✅ |
| الكمية صفر، إعادة المحاولة بنفس `op_id`، تزامن الكتابة، `database is locked` (8 اختبارات مُسمّاة) | ✅ |
| القرار E — 4 من 5 شروط | ✅ |
| القرار E — الشرط ٥ (سلوك العملية) | ✅ **مقيس** — البوت يخرج 0، الحاوية تخرج (§٧.٢) |
| انحدار السلسلة مقابل `main` | ✅ **صفر** |
| Design C لا يُفسد `test_timed_ownership` | ✅ (241 قبل وبعد) |

### ما لم يجتز / ما بقي مفتوحاً

| البند | الحالة |
|---|---|
| **R2 — التزام Booster** | ❌ exit 1 · **عائق مفتوح** · لم يُصلَح كما طُلب |
| **PR #104 وحده** | ❌ انحداران حقيقيان |
| **القرار A** | ⏸ الاختبار عُدّل ليطابق الحذف؛ **قرار المنتج ما زال بانتظارك** |
| **سلوك Railway عند فشل الإقلاع** | ❓ **غير مُتحقَّق جزئياً** — قِستُ ما يصل إلى السياسة (الحاوية تخرج غير صفرية)، لا استجابتها؛ النطاق غير متاح لي |
| **تزامن الأدوار العام** | ⏸ 19/39 محمية؛ `reactionroles` و`customcommands` بلا حماية |

### الشروط قبل أي دمج

1. **إصلاح R2** أو استبعاد مساراته بقرار صريح — لا صمتاً.
2. **قرارك في القرار A** — الاختبار الأخضر لا يغني عن موافقتك.
3. **تأكيد Akuroi لسلوك Railway** عند فشل الإقلاع + خطة التراجع.
4. **CI فعلي** على SHA الدمج — لم يُشغَّل CI على أي طرف مرشَّح.

---

## ١١. سجل التجارب — SHA والأمر والنتيجة

**مقدمة بيئية مشتركة لكل تجارب Python:** `/tmp/p1venv` (Python 3.11.2)، والمتغيرات `OWNER_ID=1 PYTHONPATH=. PYTHONFAULTHANDLER=1`.

### ١١.١ التصنيف الثلاثي للمصادر

| التصنيف | العدد | المعنى |
|---|---|---|
| **[شُغّل الآن]** | 20 تجربة | نفّذتُها بنفسي في هذه الجلسة، والنتيجة أدناه حرفية |
| **[سجل سابق]** | **0** | لا شيء في هذا التقرير معتمد على نتائج تاريخية |
| **[غير مُتحقَّق بالكامل]** | **بندان** | سياسة إعادة تشغيل Railway (§٧.٢)، وخطة التراجع (§١١.٨) |

**هذا فارق جوهري عن تقرير المرحلة ٠**، الذي احتوى 5 بنود `[تاريخي]` فقدت أدلتها الخام. هنا **كل رقم قابل لإعادة الإنتاج بالأمر المذكور**.

### ١١.٢ البوابات المُسمّاة

| # | SHA | الأمر (من داخل مجلد نسخة العمل) | النتيجة | exit |
|---|---|---|---|---|
| 1 | `f9e413f` `/tmp/p1-chain` | `python -m pytest test_timed_ownership.py -q --no-header -p no:cacheprovider` | 241 passed + 5 subtests · 130.09s | 0 |
| 2 | `f9e413f` `/tmp/p1-dc` | نفسه | 241 passed + 5 subtests · 135.95s | 0 |
| 3 | `baa6883` `/tmp/p1-pr104` | نفسه | 34 passed · 11.95s | 0 |
| 4 | `f9e413f` `/tmp/p1-chain` | `python -m pytest test_delivery_safety.py -q …` | 15 passed + 14 subtests · 5.16s | 0 |
| 5 | `f9e413f` `/tmp/p1-chain` | `python test_inventory_ui.py` (**وضع سكربت**) | 161 passed, 0 failed | 0 |
| 6 | `baa6883` `/tmp/p1-pr104` | `python test_inventory_ui.py` | 161 passed, 0 failed | 0 |
| 7 | `f9e413f`+DC `/tmp/p1-dc` | `python -m pytest test_purchase_grant_ledger.py -q …` | 29 passed · 12.61s | 0 |
| 8 | `f9e413f` `/tmp/p1-chain` | `npm test` | 18 harness(es) passed | 0 |

### ١١.٣ الحالات الثماني المُسمّاة (كمية صفر / `op_id` / تزامن / قفل)

SHA `f9e413f` + Design C، `/tmp/p1-dc`، أمر واحد بثمانية معرفات:

```
python -m pytest \
  "test_timed_ownership.py::PurchaseSagaTests::test_compensation_is_exactly_once_under_retry" \
  "test_timed_ownership.py::ExpiryCleanupTests::test_cleanup_is_safe_to_run_twice" \
  "test_timed_ownership.py::AuditFixTests::test_p2_level_reversal_entitlement_check_fails_safely_on_locked_db" \
  "test_timed_ownership.py::AuditFixTests::test_p4_stackable_item_concurrent_and_stale_spec_replay_never_double_deducts" \
  "test_timed_ownership.py::AuditFixTests::test_finding_1_refund_vs_delivery_race_and_stale_snapshot_guard" \
  "test_purchase_grant_ledger.py::DeliveryTransactionTests::test_concurrent_purchases_each_get_their_own_grant" \
  "test_purchase_grant_ledger.py::CompensationTests::test_concurrent_consumption_and_compensation_move_exactly_one_unit" \
  "test_purchase_grant_ledger.py::ReproducerTests::test_reproduce_inventory_ambiguity_exits_zero" \
  -v --no-header -p no:cacheprovider
```
**النتيجة: 8 passed · 5.47s · exit 0.** كل سطر `PASSED` مُثبت individually.

**ملاحظة على منهجيتي:** رفضتُ كتابة سكربتات تُعيد تنفيذ المنطق، لأن ذلك يُشغّل بديلاً لا الكود المشحون. بدل ذلك استخرجتُ المعرفات الحقيقية بـ`--collect-only` وشغّلتُ اختبارات المشروع نفسها. (أخطأتُ مرتين في تخمين أسماء الأصناف — `TimedOwnershipTests` غير موجود — فأصلحتُ بالجمع لا بالتخمين.)

### ١١.٤ R1 وR2

| # | SHA | الأمر | النتيجة | exit |
|---|---|---|---|---|
| 9 | `f9e413f` غير مُرقّع | `python docs/execution-20261030/R1_orig.py` | `BLOCKER: old rollback removed newer units` (4 حالات) | 1 |
| 10 | `f9e413f`+DC | نفسه | `ImportError: rollback_item_inventory_grant` | 1 |
| 11 | `f9e413f`+DC | `python docs/execution-20261030/R1_bundle.py` | `OK: no unprovable deduction on any path` | **0** |
| 12 | `f9e413f` غير مُرقّع | نفسه | `ImportError: REFUSAL_NO_PROVENANCE` | 1 |
| 13 | `f9e413f` | `python R2_repro.py` | `BLOCKER: … without a durable retry obligation` | 1 |

نُسختا R1 نُسختا من `git show` وحُفظتا متميزتين: الأصلي 3,099 B `008bf870…`، والحزمة 5,671 B `d1b31967…`.

### ١١.٥ المصفوفة الكاملة

```
bash /tmp/run_matrix.sh /tmp/p1-main  main  /tmp/mx-main.tsv
bash /tmp/run_matrix.sh /tmp/p1-pr104 pr104 /tmp/mx-pr104.tsv
bash /tmp/run_matrix.sh /tmp/p1-chain chain /tmp/mx-chain.tsv
```
المُشغّل يمرّ على كل `test_*.py` في الجذر ثم في `scripts/`، **ملف بملف** (يجمع بـ`--collect-only` أولاً؛ فإن لم يجد شيئاً وكان فيه `__main__` شغّله كسكربت). المدة ~810 ثانية.

| الحالة | PASS | FAIL | SKIP | فاشلة فريدة |
|---|---|---|---|---|
| `main` | 83 | 24 | 3 | 21 |
| `pr104` | 83 | 27 | 3 | 23 |
| `chain` | 88 | 23 | 3 | 21 |

**مقارنة المجموعات:** `comm -13 F-main F-chain` → **فارغ** (صفر انحدار). `comm -13 F-main F-pr104` → `test_free_shop_items.py`, `test_vi_shop_api.py`.

**لماذا ملف بملف؟** الجمع في عملية pytest واحدة يُنتج 3 أخطاء تجميع زائفة على `main` (`DB path mismatch`, `KeyError: 'Bronze'`) — تحققتُ أنها **تنجح منفردة** (exit 0)، فهي أثر عزل لا عطل.

### ١١.٦ القرار E

```
/tmp/p1venv/bin/python /tmp/verify_decision_e.py     (من /tmp/p1-chain)
```
قاعدة يمكن التخلص منها في `/tmp/phase1_e_*`، تُبنى بجدول `equipped_roles` قديم (4 صفوف، بلا `entry_key`). **التشغيل الأول exit 1 بسبب خلل أداتي عندي** (§٧.١)، وبعد `fresh_run()` → **exit 0**.

### ١١.٧ إعادة إنتاج انحدار PR #104 معزولاً

| # | SHA | الأمر | النتيجة | exit |
|---|---|---|---|---|
| 14 | `baa6883` | `python -m pytest test_vi_shop_api.py -q …` | 4 failed, 5 passed | 1 |
| 15 | `main` | نفسه | 6 passed, 8 subtests | 0 |
| 16 | `f9e413f` | نفسه | 6 passed, 8 subtests | 0 |
| 17 | `baa6883` | `cd scripts && python test_free_shop_items.py` | `AssertionError: Lists differ: [] != [('temp_role',)]` | 1 |
| 18 | `baa6883` | `cd scripts && python test_vi_shop_api.py` | `FAILED (failures=4)` | 1 |

### ١١.٧ب الشرط ٥ — سلوك العملية عند فشل الهجرة

SHA `f9e413f`، من `/tmp/p1-chain`، على قاعدة يمكن التخلص منها:

| # | الأمر | النتيجة | exit |
|---|---|---|---|
| 19 | `python /tmp/verify_exit_code.py` | `FATAL ERROR: … startup aborted…` ثم **`exit code = None`** ⇒ خروج بالرمز **0** | 0 |
| 20 | `python /tmp/verify_dashboard_crash.py` | `dashboard/app.py:99 run_async(init_db())` ⇒ **uncaught RuntimeError** | 0 |

**قياسان إضافيان لإثبات الأثر:**
- `grep -n "sys.exit" main.py` → `51`, `354` فقط (معالج `LoginFailure`)؛ **صفر** بعد `except Exception as e:` في `355`.
- `start.sh` → حلقة `supervise_bot` تُعيد كل ٥ ثوانٍ وتتجاهل الرمز (`exit_code=$?` يُطبع فقط).
- `grep -n "init_db" dashboard/app.py` → `15` (استيراد)، **`99` (على مستوى الوحدة)**.

**القياس ٢٠ هُزم أولاً بحارس قائم** (`SECRET_KEY` مفقود ⇒ `SystemExit(1)` عند السطر 51 قبل بلوغ `:99`) — موثَّق في §٧.٣.

### ١١.٨ البندان غير المُتحقَّقين بالكامل

| البند | ما قِسه | ما لم أستطع قياسه |
|---|---|---|
| **سلوك الإقلاع عند فشل الهجرة** (§٧.٢) | ✅ البوت يخرج **0**، حلقة `supervise_bot` تُعيد كل ٥ ثوانٍ، واللوحة تموت باستثناء غير ملتقط عند `:99` ⇒ الحاوية تخرج | ❓ **سياسة إعادة تشغيل Railway نفسها** — نطاق Railway غير متاح لي |
| **خطة التراجع إلى إصدار سابق** | — | ❓ **غير مُتحقَّق إطلاقاً** — يتطلب إعداد Railway الفعلي |

**لا بديل تخميني مُقدَّم لأيٍّ منهما.** المطلوب من Akuroi: تأكيد أن سياسته تُعيد تشغيل حاوية خارجة برمز غير صفري، وخطة التراجع اليدوي.

---

## ١٢. ما لم أفعله (التزاماً بحدود الموافقة)

- ❌ لم أعدّل المستودع الأصلي — 0 تعديل على المصدر، HEAD ما زال `a6b812b`
- ❌ لم أنشئ commit أو PR
- ❌ لم أدفع أي تغييرات إلى GitHub — 0 refs بعيدة للفرع
- ❌ لم أدمج ولم أنشر إلى Railway
- ✅ PR #104 ما زال **OPEN**
- ❌ لم أبدأ B0 أو B1
- ❌ لم أُصلح R2 ولم أبدأ تغييرات خارج نطاق التحقق
- ❌ لم ألمس قاعدة الإنتاج أو بيانات Railway — `/app/data` غير موجود
- ❌ لم أنفّذ عمليات Discord حقيقية

**`git apply` استُخدم على `/tmp/p1-dc` فقط** (نسخة مؤقتة)، والـSHA الأساسي `f9e413f72a026ac09e33104f809ea2345753eee7`، والـpatch `sha256 6d94d9a11d2b3c679068fd39473e22a3c363f07fffc9a7d0f1ffe98ec4d0d4fd` (118,342 بايت، 11 ملفاً)، وانطبق نظيفاً (`git apply --check` exit 0).
