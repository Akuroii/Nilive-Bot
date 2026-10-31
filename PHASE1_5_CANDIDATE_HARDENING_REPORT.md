# Phase 1.5 — Candidate Hardening: Final Report

**Branch:** `arena/3792b762-nilive-bot` · **Date:** 2026-10-31
**Candidate:** `f9e413f72a026ac09e33104f809ea2345753eee7` + Design C + Phase 1.5 fixes
**Isolation:** `/tmp/p1-cand` (worktree of `/tmp/fx`). The archive `/home/user/Nilive-Bot` was **not modified** — 0 source changes, verified again at the end of this phase.

---

## ١. الخلاصة التنفيذية

| البند | الحالة |
|---|---|
| المرشح مُقام على `f9e413f` + Design C (لا PR #104) | ✅ |
| مصفوفة ١١٦ بوابة على الشجرة النهائية، ٠ انحسارات مقابل main | ✅ |
| R2 — التزام دائم + مُصالِح؛ المُعيد ينجح | ✅ |
| ٦ اختبارات R2 المطلوبة (+٢ إضافيان) | ✅ ٨/٨ |
| استعادة Drop دون الرجوع عن إصلاحات `cb54484` | ✅ |
| تصحيح تقرير فشل بدء التشغيل (يبقى fail-closed) | ✅ |
| `test_timed_ownership.py` — ٢٤١ | ✅ |
| `npm test` — ١٨ حزمة / ١٤٨ فحصاً | ✅ |

**حكم الجاهزية:** المرشح **جاهز للمراجعة البشرية**. ليس جاهزاً للنشر — انظر §٩.

---

## ٢. ما كان معطوباً وما أُصلح

### ٢.١ R2 — الالتزام الدائم عند أهلية Booster مجهولة

**العطب:** عندما تكون أهلية Booster غير قابلة للإثبات، كان `_booster_level_for` يُرجع `(1, False)` فيخمّن `b2_id` من الدور الملبوس. `role_removal_blocked` كان يُبقي الدور (fail-closed، صحيح) لكن **لا يكتب أي التزام دائم** — فيُحتفظ بالدور إلى الأبد على تخمين لا يمكن التحقق منه.

**الإصلاح:** الالتزام صف محجوز في `timed_holdings`:

| الحقل | القيمة |
|---|---|
| `item_type` | `role_retention` (ثابت `RETENTION_ITEM_TYPE`) |
| `source` | `booster_retention` (ثابت `RETENTION_SOURCE`) |
| `role_id` | الدور المشترك |
| `expires_at` | `9999-12-31T23:59:59+00:00` — بعيد جداً فلا يختاره `expired_holdings` ولا يمسه المكنسة |
| `metadata` | `{retention, role_id, reason, origin, attempts, first_seen, last_checked, last_state}` |

**منع التكرار:** فهرس جزئي فريد + إدراج لا يكرر:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_timed_holdings_retention
  ON timed_holdings(guild_id, user_id, role_id)
  WHERE item_type = 'role_retention'
```
```
INSERT … ON CONFLICT(guild_id, user_id, role_id)
  WHERE item_type = 'role_retention' DO NOTHING
```

**المُصالِح:** `reconcile_role_retention_obligations(bot, *, limit)` — يُعيد تقييم الأهلية ويُزيل الدور **فقط** بعد تأكيد انتهاء الأهلية؛ يُبقي الالتزام إذا فشلت الإزالة أو بقيت الأهلية مجهولة. مُوصَّل بـ `cleanup_expired_holdings` (السطر ٢٠٤٠) التي تعمل على مؤشر الدّور العشر دقائق الموجود في `cogs/shop.py:1306`.

**الزخَّة التي تجعل الالتزام محاسبة لا منحة:** كل قارئ استحقاق يُستثنى منه `item_type='role_retention'` — وإلا لأصبح الالتزام نفسه سبباً في إبقاء الدور أبداً، وهو العطب نفسه الذي وُضع لإصلاحه.

**حاجز التزام مُصَمَّم لتجنُّب فخّ:** التزام المراجعة يملك اتصاله الخاص ويُنفّذ `commit()` صريحاً. قِيست هذه المسألة: `async with aiosqlite.connect(...)` **لا يُثبِّت** عند الخروج؛ إدراج بدون `commit()` ترَك الجدول فارغاً وأسقط الالتزام بصمت.

---

### ٢.٢ استعادة Drop

**العطب:** `cb54484` حذف واجهة Drop و**عدّل الاختبارات لتطابق الحذف** (`"still offers Drop"` → `"never offers Drop"`). الحذف لم يُعتمد؛ والاختبار الأخضر ليس موافقة.

**الإصلاح:** استعادة `wallet.py` و`cogs/wallet.py` من `cb54484^` حصراً — تأكّدت أن الشجرتين لم يتغيّرا بعد `cb54484`. واستعادة توقعات الاختبار في `test_inventory_ui.py` و`test_wallet_phase2.py` ونسختَي `scripts/`.

**ما لم يُرجَع:** إصلاحات `cb54484` الأمنية الحقيقية في `utils/level_ops.py` و`utils/reward_engine.py` و`utils/potion_engine.py` و`utils/level_test_reset.py` و`test_purchase_grant_ledger.py` — كلها باقية دون مساس.

**تفصيل مهم كشفته الاختبارات:** Drop مقيَّد بالصفوف **الدائمة** فقط؛ الملكية المؤقتة تُنهيها صلاحيتها وهي نفسها تُخلع الدور وتُفرغ الخانة. هذا **خاصية سلامة حقيقية مُعلَّلة** في `cogs/wallet.py:1157-1163`، لا جزء من الحذف. الاختبار الآن يؤكّد السلوك الحقيقي: Drop للدائم، ولا Drop للمؤقت بتصميم.

---

### ٢.٣ تقرير فشل بدء التشغيل

**العطب:** `main.py` يمسك `except Exception` ويطبع `FATAL ERROR` ثم **يخرج برمز ٠** — فشل قاتل لا يُميَّز عن إغلاق نظيف. و`start.sh` `supervise_bot` يتجاهل رمز الخروج ويعيد التشغيل كل ٥ ثوانٍ إلى الأبد. المحصّلة: حلقة تحطّم لا تلتئم، والحاوية تبدو سليمة.

**التصحيح الأدنى (يبقى fail-closed):**
- `main.py`: `sys.exit(1)` في المعالج العام. الرمز ١ = عقد الفشل القاتل.
- `start.sh`: المشرف يتوقف عند الرمز ١ بدل الدوران؛ أي رمز آخر لا يزال يُعاد تشغيله.

**لم يُفترض — بل قِيست الدورة الكاملة** (فشل SQLite حقيقي: VIEW مكان TABLE ⇒ `Cannot add a column to a view`):

| الشرط | النتيجة |
|---|---|
| A الهجرة المطلوبة تفشل فعلاً | ✅ exit 1، `raised=True`، `names_migration=True` |
| B `main.py` كعملية فرعية حقيقية | ✅ exit 1 (كان ٠) |
| C `supervise_bot` الحقيقي المستخرج من start.sh | ✅ ينتهي برمز ١، إطلاق واحد فقط |
| D ضبط: تحطم عابر | ✅ ما زال يُعاد تشغيله (لا تراجع عن إصلاح الموثوقية) |

---

## ٣. الملفات المتغيّرة ولماذا

### تعديلاتي وحدها (مقابل `f9e413f` + Design C)

| الملف | لماذا |
|---|---|
| `utils/timed_ownership.py` | R2: الثوابت، التسجيل، المُصالِح، الفُهرس الجزئي، استثناء الالتزام من كل قارئ استحقاق |
| `utils/level_claims.py` | R2: استثناء `role_retention` من فحص الاستحقاق في `_role_granted_elsewhere` |
| `cogs/wallet.py` | استعادة واجهة Drop |
| `wallet.py` | استعادة واجهة Drop |
| `main.py` | خروج ١ عند فشل بدء التشغيل |
| `start.sh` | توقف المشرف عند الفشل القاتل |
| `test_timed_ownership.py` | تحديث ٤ توكيدات لشكل قاموس `cleanup_expired_holdings` (مفتاح `retention` جديد) |
| `test_delivery_safety.py` | استعادة توقعات Drop في `WalletDeletionSurfaceTests` |
| `test_inventory_ui.py` + `scripts/` | استعادة توقع `"still offers Drop"` |
| `test_wallet_phase2.py` + `scripts/` | استعادة توقعات Drop |

### ملفات جديدة

| الملف | لماذا |
|---|---|
| `test_role_retention_obligations.py` | الاختبارات الثمانية لـ R2 |
| `R2_repro.py` | مُعيد العطب الذي قدّمه المستخدم |

### حذف

`package-lock.json` — **غير متتبَّع في Git، وغير موجود على main، وغير مدرج في `.gitignore`**. حذفه لا يغيّر شجرة Git بحرف واحد، و`npm test` ينجح بدونه (١٨/١٨). لم يُحذف شيء متتبَّع.

---

## ٤. قبل / بعد

### R1 (لم يتغيّر — سُجِّل للمقارنة)

| المُعيد | الشجرة | قبل | بعد |
|---|---|---|---|
| الأصلي (`008bf870…`) | بلا Design C | exit 1 `BLOCKER: old rollback removed newer units` | exit 1 `ImportError: rollback_item_inventory_grant` |
| الحزمة (`d1b31967…`) | +Design C | — | **exit 0** `OK: no unprovable deduction on any path` |

`ImportError` **دليل**: Design C أزال `def rollback_item_inventory_grant` وأضاف ثوابت الرفض.

### R2

| | قبل | بعد |
|---|---|---|
| الدور | محفوظ (fail-closed) | محفوظ |
| التزام دائم | **لا شيء** | `[('booster_retention', 557)]` |
| المُعيد | exit 1 | **exit 0** |

```
[TWITCH] Keeping Twitch live role 557 for 8201: still an active Booster reward role
{'role_preserved_under_unknown': True, 'durable_obligations': [('booster_retention', 557)]}
```

---

## ٥. النتائج الكاملة للاختبارات

| المجموعة | الأمر | النتيجة |
|---|---|---|
| مصفوفة ١١٦ بوابة | `/tmp/run_matrix.sh /tmp/p1-cand clean /tmp/mx-clean.tsv` | **90 PASS / 23 FAIL / 3 SKIP** |
| `test_timed_ownership.py` | pytest | **241 passed** |
| `test_delivery_safety.py` | pytest | **17 passed** |
| `test_purchase_grant_ledger.py` | pytest | **29 passed** |
| `test_inventory_ui.py` | `python` (سكربت) | **161 passed** |
| `test_wallet_phase2.py` | `python` (سكربت) | **54 passed** |
| `npm test` | npm | **18 harnesses / 148 checks** |
| `test_role_retention_obligations.py` | pytest | **8 passed** (٦ مطلوبة + ٢) |
| R2 مُعيد | `python R2_repro.py` | **exit 0** |
| R1 حزمة | `/tmp/R1_bundle.py` | **exit 0** |
| R1 أصلي | `/tmp/R1_original.py` | exit 1 `ImportError` (متوقع) |
| Decision E ١-٤ | `/tmp/verify_decision_e.py` | **exit 0** |
| مسار بدء التشغيل A-D | `/tmp/verify_startup_failure.py` | **exit 0** |

### المقارنة مقابل main — لا انحسارات

| | main | المرشح |
|---|---|---|
| صفوف المصفوفة | 110 | 116 |
| PASS / FAIL / SKIP | 83 / 24 / 3 | **90 / 23 / 3** |
| ملفات فاشلة فريدة | **21** | **21** |
| بوابات مشتركة | — | 91 |
| انحسارات | — | **٠** |
| تحسّنات | — | ١ (`test_booster_rewards.py`) |
| بوابات جديدة | — | ٥، **كلها PASS** |

**مجموعتا الإخفاقات مُطابتَان تماماً** — لا ملف يفشل عند المرشح وينجح عند main، ولا العكس. الإخفاقات الـ٢١ كلها **موروثة من main** وليست من تعديلاتي:

`test_afk.py` · `test_afk_dashboard.py` · `test_booster_rewards.py` · `test_check_emoji.py` · `test_currency_icon.py` · `test_heartbeat.py` · `test_leveling_voice_hard_blocks.py` · `test_minigame_{engines,spawn,store}.py` · `test_phase1_runtime.py` · `test_phase2_backend.py` · `test_prestige.py` · `test_shop_{emoji,emoji_tls,options,publications,publisher,publisher_api}.py` · `test_slice3_shop_claims.py` · `test_slice4_boost_claims.py`

---

## ٦. الاختبارات الستة المطلوبة لـ R2

| # | الاختبار | الحالة |
|---|---|---|
| ١ | أهلية مجهولة ⇒ التزام دائم | ✅ |
| ٢ | يبقى بعد إعادة التشغيل/المُصالِح (٣ دورات، `attempts` تتراكم) | ✅ |
| ٣ | الدور يُزال عند تأكيد انتهاء الأهلية | ✅ |
| ٤ | الدور يبقى أثناء النشاط/عدم اليقين (حالتان) | ✅ |
| ٥ | منع التكرار (٦ استدعاءات ⇒ صف واحد) | ✅ |
| ٦ | مُصالِح فاشل لا يُسقط الالتزام (`remove_roles` يرفع) | ✅ |
| + | الالتزام مخفي عن جرد العضو | ✅ |
| + | الالتزام ليس استحقاقاً (`level_claim_role_entitled` ⇒ None) | ✅ |

---

## ٧. ما حُفظ وما استُثني بقصد

**حُفظ:**
- `boost.py` · `mvp.py` · `twitch.py` · `prestige.py` — لم يُستثنَ شيء؛ العطب عولج أصلاً لا بإخفاء.
- حماية الدور المشترك لم تُعطَّل بصمت.
- Decision E (fail-closed) كما هو — صُحِّح التقرير فقط.
- إصلاحات `cb54484` الأمنية في `utils/`.
- PR #104 **مفتوح** — لم يُلمس.

**استُثني بقصد:**
- لا مراجعة واسعة لـ `slot_lock` (غير قابل لإعادة الدخول) — فقط ما تتطلبه R2.
- لا B0 ولا B1.
- لا إعادة هيكلة ولا ميزات جديدة.

---

## ٨. قيود مُتبقية

1. **`main` يحمل ٢١ إخفاقاً أصلاً** — لم أُصلحها (خارج النطاق). كلها موروثة ومُوثَّقة.
2. **لم يُشغَّل CI** — لا `.github/workflows/tests.yml` على `main`، ولا فحوص على أي من SHAs المعنية.
3. **إعدادات Railway غير مقروءة** — مضيفها خارج نطاقي. سياسة إعادة التشغيل تُستنتج من سجل GitHub Deployments لا من الإعدادات.
4. **حماية الفرع غير معروفة** — `branches/main/protection` ⇒ HTTP 403.
5. **كتّاب الأدوار غير الموثَّقين:** `cogs/reactionroles.py` (٧) و`cogs/customcommands.py` (٢) ليسوا محميين بـ `role_removal_blocked` — موثَّقون فقط، غير مُصلَحين.
6. **المُصالِح يعمل على مؤقّت الدّور العشر دقائق** — التأخير حتى ١٠ دقائق مقبول، لم يُقس.
7. **اختبار D لمسار بدء التشغيل يتحقق من عدم الدوران فقط** — لا يحاكي سياسة إعادة تشغيل الحاوية الفعلية.

---

## ٩. حكم الجاهزية

**المرشح جاهز للمراجعة البشرية.** الدليل: ٠ انحسارات على ١١٦ بوابة، مجموعة إخفاقات مُطابقة لـ main، R2 مثبَتة بثمانية اختبارات ومُعيد ناجح، Drop مُستعاد دون المساس بإصلاحات السلامة، ومسار بدء التشغيل مُقاس لا مُفترض.

**غير جاهز للنشر.** ثلاث خطوات يجب أن تُغلق قبل المرحلة التالية:
1. CI على الطرف النهائي.
2. التحقق من إعدادات Railway (سياسة إعادة التشغيل + خطة التراجع).
3. مراجعة الفرق النهائي بقبول بشري.

**لم يُدفع شيء. لم يُنشأ فرع بعيد. لم تُفتح PR. لم يُدمج. لم يُنشر. الأرشيف لم يُعدَّل.**

### التحقق النهائي من العزل (مقيس لا مُدَّعى)

| الفحص | النتيجة |
|---|---|
| `HEAD` في الأرشيف | `a6b812be26d35f3048db39d0425374d23cd804ea` (بلا تغيير) |
| شجرة الأرشيف | `d0edba20c252ab780b7b43013e216ce36cf8db56` (بلا تغيير) |
| تعديلات المصدر المتتبَّعة | **٠** |
| ملفات متتبَّعة | ٦١٤ |
| commits محلية فوق `origin/main` | **٠** |
| فروع على `origin` باسم جلستي | **٠** |
| مراجع بعيدة في الأرشيف | ٢ (`origin/HEAD`, `origin/main`) — أثر الاستنساخ الأصلي فقط، تشيران إلى `a6b812b` نفسه |

**تصحيح لادّعاء سابق:** قلت في مرحلة سابقة إن المراجع البعيدة "صفر". القياس الصحيح أنها ٢، وهما أثر الاستنساخ الأصلي لا شيء دفعته أنا. لم يُدفع شيء إلى أي مضيف.
