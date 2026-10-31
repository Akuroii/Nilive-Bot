# Phase 0 — Final Integration Preflight

**التاريخ:** 2026-10-31
**النطاق:** فحص للقراءة فقط. لم يُعدَّل أي ملف مصدر. لم يُستخدم `git apply` / `cherry-pick` / `merge` / `rebase`. لم يُنشأ commit أو فرع أو PR. لم يُدفع شيء. PR #104 ما زال مفتوحاً. لم يبدأ B0 ولا B1.
**الحكم:** في البند ٧.

---

## ١. حالة المستودع والـPR — مُتحقَّق منها

### ١.١ الأطراف

| المرجع | SHA | ملاحظة |
|---|---|---|
| `main` | `a6b812be26d35f3048db39d0425374d23cd804ea` | 614 ملفاً مُتتبَّعاً، tree `d0edba20…`، **0 تعديل على المصدر** |
| `arena/c014328f-nilive-bot` | `f9e413f72a026ac09e33104f809ea2345753eee7` | 2026-10-30 12:35 |
| `arena/884b26a8-nilive-bot` (PR #104) | `baa6883a286551dfbd5b1837c8bcc038cf7897e2` | 2026-10-24 10:09 |
| `review/g-purchase-grant-ledger` (Design C) | `c872aa299396ed58fb19ab3c0fb5c6997b86a6ee` | 2026-10-30 18:34 |

مصدر القراءة: استنساخ كامل **791 commit**، `git fsck --full --strict` = **0 مشكلة**.

### ١.٢ التباعد الدقيق

| القياس | القيمة |
|---|---|
| `main..c014328f` | **53 commit** |
| `c014328f..main` (behind) | **0** |
| `merge-base(main, c014328f)` | `a6b812b…` = **طرف `main` الحالي بالضبط** |
| merge commits في السلسلة | **0** — خطية تماماً |
| ملفات متغيّرة `main…c014328f` | **113** |

### ١.٣ PR #104

`gh pr view 104` نجح عندي — **مشكلة الـ403 التي واجهها Akuroi لا تظهر من هذه البيئة** (المصادقة كـ`Akuroii` عبر `GH_TOKEN`).

| الحقل | القيمة |
|---|---|
| الحالة | **OPEN** |
| mergeable | **MERGEABLE** |
| base | `main` |
| head | `arena/884b26a8-nilive-bot` @ `baa6883a286551dfbd5b1837c8bcc038cf7897e2` |
| commits | **7** |
| ملفات | **20** (+5,125 / −342) |
| **checks** | **«no checks reported on the 'arena/884b26a8-nilive-bot' branch»** — لا CI إطلاقاً |

### ١.٤ أي commits يمثّلها PR #104

`baa6883` **سلف من** `c014328f` (تحقّق `git merge-base --is-ancestor`)، و`c014328f..884b26a8` = **0**. أي:

- **الـ7 commits كلها داخل السلسلة** — لا يوجد عمل في PR #104 خارج `c014328f`.
- **46 commit في `c014328f` غير ممثَّلة في PR #104.**

الـ7 (الأقدم→الأحدث):
```
dbf772f  feat(inventory): dedicated Items/Potions/Titles panel
16f30cc  fix(inventory): resolve prepared emoji for catalog-less Shop items
1f1c62d  feat(shop): timed ownership model (Phase 1)
ae4a9d2  feat(shop): five product types, duration decides permanence (Phase 2)
7c6b714  fix(shop): purchases become a compensated saga (Phase 3)
016186f  feat(inventory): entry-key actions, Colors tab, safe expiry paths (Phase 3)
baa6883  test(shop): timed ownership, Colors and purchase-saga coverage
```

أطراف الـ46: `7283809` (الأقدم) ← … ← `cb54484` ← `f9e413f` (الأحدث).

### ١.٥ CI على الـSHA المرشَّح الفعلي

16 تشغيلاً، **كلها على `arena/c014328f-nilive-bot`**. آخر نجاح: `3307231ae` (2026-10-29 16:09).

| SHA | عدد التشغيلات |
|---|---|
| `f9e413f…` (طرف السلسلة) | **0** |
| `8eb8e3d…` (phase22-final) | **0** |
| `baa6883…` (طرف PR #104) | **0** |
| `cb54484…` | **0** |
| `6c04021…` | **0** |

**لم يُشغَّل CI على أي طرف مرشَّح للدمج.** آخر نجاح يسبق طرف السلسلة بـ4 commits.

---

## ٢. سلوك Railway — مُتحقَّق سلوكياً، لا من ملفات الإعداد

**قيد صريح:** نطاق Railway ليس ضمن النطاقات المتاحة لي (`github.com`, `api.github.com`, `codeload`, `registry.npmjs.org`, `pypi.org` فقط). **لا أستطيع قراءة إعداد Railway الفعلي**، ولذلك لم أعتمد على ملفات الإعداد.

ما فحصته بدلاً من ذلك — **سجل النشر الفعلي** عبر GitHub Deployments API:

| الفحص | النتيجة |
|---|---|
| منشئ عمليات النشر | **`railway-app[bot]`** |
| عدد البيئات | 4، كلها بنمط `<service> / production` |
| refs منشورة فريدة | **39** |
| **منها أسلاف من `main`** | **39 / 39** |
| ليست أسلافاً من `main` | **0** |
| آخر نشر | `ref = a6b812be26d35f3048db39d0425374d23cd804ea` بتاريخ 2026-10-23 19:15 |
| طرف `main` الحالي | `a6b812be26d35f3048db39d0425374d23cd804ea` — **مطابق حرفياً** |

**الاستنتاج المدعوم بالأدلة:** Railway ينشر من `main`. كل ref نُشر تاريخياً هو سلف من `main`، وآخر نشر هو طرف `main` الحالي بالضبط.

**ما لا أستطيع إثباته:** هل النشر **تلقائي** عند كل push إلى `main` أم يدوي. هذا الإعداد داخل Railway نفسه. لكن الاستنتاج العملي لا يتغيّر: **أي دمج في `main` يجب أن يُعامل على أنه نشر إنتاجي وشيك.**

معطيات مساندة: `Dockerfile` موجود على `main` (`python:3.11-slim` → `CMD ["bash","start.sh"]`)، ولا يوجد `railway.json` / `railway.toml` / `Procfile` / `nixpacks.toml`. `app.py` و`backup.py` يذكران Railway صراحة (متغيرات البيئة، ومسار قاعدة البيانات على volume).

`branch protection` على `main`: **HTTP 403 — «Resource not accessible by integration»**. لا أستطيع تأكيد وجود حماية أم لا.

---

## ٣. حالة العوائق

### ٣.١ R1 / Design C — **إعادة تأطير جوهرية**

**المسار المعرّض حرفياً:** `rollback_item_inventory_grant`، معرَّفة في `utils/reward_engine.py:673` على `c014328f`، وتُستدعى من `utils/timed_ownership.py:5075` داخل مسار الاسترجاع:

```python
inv_ok = await rollback_item_inventory_grant(
    guild_id, user_id, item_name,
    quantity=1, prior_inv=prior_inv, delivered_inv=delivered_inv, db=rb_db,
)
```

**أين يوجد هذا المسار فعلاً:**

| المراجعة | `rollback_item_inventory_grant` |
|---|---|
| `main` (الإنتاج اليوم) | **0 — غير موجود إطلاقاً** |
| `baa6883` (طرف PR #104) | **0 — غير موجود** |
| `f9e413f` (طرف السلسلة) | **4 ملفات** |

أدخله commit **`5100a5f`** (2026-10-25) «fix(shop,rewards): harden purchase compensation for permanent Colors…» — وهو **داخل الـ46، لا داخل PR #104**.

**النتيجة:** خطر R1 **لا يصل مع PR #104**. يصل فقط إذا أُخذت الـ46 commit اللاحقة. ومُعيد R1 نفسه (`from utils.reward_engine import rollback_item_inventory_grant`) **لا يمكن أن يعمل على طرف PR #104** لأن الدالة غير موجودة هناك.

**تصحيح منهجي على هذا الاستنتاج:** بنيته أولاً على غياب *اسم* الدالة، وهو دليل ضعيف (غياب الاسم لا يثبت غياب الثغرة — فقد تكون مُعاد تسميتها). أعدتُ الفحص بنيوياً، والنتيجة نجت بدليل أقوى:

| الفحص البنيوي | `baa6883` (PR #104) | `f9e413f` (السلسلة) |
|---|---|---|
| دوال استرجاع في `utils/reward_engine.py` | **0** | **2** — `rollback_role_inventory_grant:530`, `rollback_item_inventory_grant:673` |
| دوال استرجاع في `utils/timed_ownership.py` | **0** | موجودة |
| `_compensate_failed_purchase` في `cogs/shop.py` | موجود (`:639`) | موجود |

`_compensate_failed_purchase` عند `baa6883` **لا يلمس استرجاع المخزون إطلاقاً** — يقتصر على `claim_purchase_transition` ثم `refund_purchase(op_id)`. ومسح الأسطر 639–860 لـ`inventory|give_item|drop_item|quantity|reward_grants` أعاد **تعليقات فقط** عن تنظيف temp_role، لا استدعاءات فعلية.

**وكلا الاسترجاعين أُدخلا في commit واحد:** `5100a5f` (2026-10-25) — داخل الـ46، لا داخل PR #104.

**الحكم المُصحَّح:** آلية الاسترجاع المعرّضة **غير موجودة بنيوياً** عند طرف PR #104، لا merely مُعاد تسميتها. الاستنتاج قائم، والدليل الآن بنيوي لا اسمي.

#### شبهة أثرتُها ثم نقضتُها بالدليل — موثَّقة لأمانة السجل

بينما كنت أُثبت ما سبق، طرحتُ شبهة أخطر: إن كان مسار التعويض عند `baa6883` (`refund_purchase`) يعالج **المال والمخزون التجاري فقط** ولا يسترجع منح العنصر، فهل يمكن أن **يحتفظ المشتري بالعنصر ويسترد ماله**؟

تتبعتُ السلسلة كاملة عند `baa6883`:

| الحلقة | الموضع | ما ثبت |
|---|---|---|
| `_deliver_purchase` → فرع `item`/`custom`/`potion`/`title` الدائم | `cogs/shop.py:570-640` | يستدعي `give_reward(..., "item", amount=1)` |
| `give_reward` → فرع `item` | `utils/reward_engine.py:421` | يستدعي `give_item(...)` **بدون معامل `db`**، ويعيد `success:False` عند أي استثناء |
| `give_item` | `utils/inventory.py:46` | **ذرية** |
| `refund_purchase` | `utils/timed_ownership.py:914` | `safe_credit` + `restore_stock` فقط — لا استرجاع لمنح العنصر |

**`give_item` ذرية حرفياً:**

```python
async with aiosqlite.connect(DB_PATH) as connection:
    await connection.execute("BEGIN IMMEDIATE")
    try:
        result = await apply(connection)
        await connection.commit()
        return result
    except Exception:
        await connection.execute("ROLLBACK")
        raise
```

ولأن `give_reward` لا يمرّر `db`، فهي تسلك **المسار الذري المستقل** لا مسار المعاملة المملوكة للمستدعي. فـ`success:False` بعد استثناء تعني أن الصف **لم يُودَع أصلاً**.

**نقض الشبهة:** لا يوجد منح جزئي ليسترجعه `refund_purchase`. عدم استرجاعها لمنح العنصر **سلوك صحيح** عند `baa6883`، لا ثغرة.

**ما يوضحه هذا عن طبيعة R1 نفسها:** `rollback_item_inventory_grant` ليست معالجة لخلل قديم، بل **آلية جديدة** أدخلها `5100a5f` مع مسار تعويض Colors الدائم. وثغرة R1 كامنة **داخل منطق الاسترجاع الجديد نفسه** (يسترجع وحدات أحدث لا يملك إثباتها) — لا في كود سابق لها. لذلك R1 **خطر يُستحدَث بالدمج**، ويحتاج Design C في نفس الوحدة، ولا وجود له في PR #104.

**قيد صريح على هذا الاستنتاج:** استنتاجي مبنيّ على **قراءة الكود**، لا على تشغيل. لم أُشغّل مُعيداً على `baa6883`. الذرّية ثابتة نصياً، لكن السلوك تحت `database is locked` أو انقطاع العملية بين `commit` وعودة القيمة **لم يُختبر** — وهذا بند #11 في مصفوفة المرحلة ١.

**هل Design C مبنيّ على `c014328f` المقصود؟ — نعم، مُثبَت بالبصمات.**

طابقتُ بصمات ما-قبل-الصورة في الـpatch مع blobs الفرع الفعلية (`git rev-parse <ref>:<path>`):

| الملف | patch | `c014328f` | الحكم |
|---|---|---|---|
| `.github/workflows/tests.yml` | `e8eb1ba` | `e8eb1ba` | ✓ |
| `cogs/shop.py` | `8bf82fd` | `8bf82fd` | ✓ |
| `reproduce_inventory_ambiguity.py` | `3f5e15b` | `3f5e15b` | ✓ |
| `test_timed_ownership.py` | `27f227c` | `27f227c` | ✓ |
| `utils/level_ops.py` | `40f2861` | `40f2861` | ✓ |
| `utils/level_test_reset.py` | `1c69241` | `1c69241` | ✓ |
| `utils/reward_engine.py` | `f053130` | `f053130` | ✓ |
| `utils/timed_ownership.py` | `110d524` | `110d524` | ✓ |
| `G-purchase-grant-ledger.md` | جديد | غائب | ✓ |
| `test_purchase_grant_ledger.py` | جديد | غائب | ✓ |
| `docs/execution-20261030/README.md` | `0ade36a` | `476f4a7` | **اختلاف — توثيقي فقط** |

**8/8 ملفات كود مطابقة حرفياً.** الاختلاف الوحيد ملف توثيقي، ولا يؤثر على قابلية التطبيق على الكود.

**ترتيب التراكب الإلزامي:** `c014328f` (أو أي نطاق يحتوي `5100a5f`) **أولاً**، ثم Design C فوقه. Design C **لا يُطبَّق على `main`** — بصمات ما-قبل-الصورة لا توجد هناك.

**القاعدة الحاكمة:** لا يجوز أن يصل `5100a5f` وما بعده إلى الإنتاج دون Design C في نفس الوحدة.

### ٣.٢ R2 / التزام Booster

**ما يفحصه المُعيد حرفياً:** يستدعي `Twitch._go_offline` مع عضو حالته `premium_since` معلومة لكن **عدد الـboosts غير متاح** (`assert not hasattr(person,"premium_subscription_count")`)، ثم:

```python
assert obligations, "BLOCKER: source ended under unknown Booster eligibility
                     without a durable retry obligation"
```

**المسارات الإنتاجية المتأثرة:**

| المعطى | `main` | `baa6883` | `f9e413f` |
|---|---|---|---|
| `timed_holdings` (ملفات) | **0** | 8 | 12 |
| `utils/timed_ownership.py` | **غائب** | موجود | موجود |
| `role_removal_blocked` (ملفات) | **0** | **0** | 8 |
| `cogs.twitch` مُحمَّل في `main.py` | — | نعم (`:196`) | نعم |

**النتيجة:** R2 **ليس خللاً موجوداً في الإنتاج اليوم**. `timed_holdings` و`utils/timed_ownership.py` غير موجودة على `main` إطلاقاً. الخطر **يصل مع الدمج**.

**هل يمنع الدمج؟** هذا قرار لك، لكن المعطيات تقول:
- المسار المعني (`Twitch._go_offline` + حماية الأدوار المشتركة) **لا يكتمل إلا في الـ46 commit** — `role_removal_blocked` = 0 عند طرف PR #104.
- لذلك **PR #104 وحده لا يُدخل مسار R2 بصورته المُبلغ عنها**، لأنه لا يحوي `role_removal_blocked` أصلاً.
- أخذ الـ46 كاملة يُدخل المسار **بثغرة R2 مفتوحة**.

**لا أصنّفها «مهمة مستقبلية» صامتة.** الخياران الآمنان: (أ) استبعاد نطاق الدور المشترك (boost/twitch/mvp/prestige) مؤقتاً، أو (ب) إصلاح R2 قبل أخذ هذا النطاق.

**تحفّظ صريح:** لم أُشغّل مُعيد R2 على `baa6883`. استنتاجي أن المسار غير مكتمل هناك مبنيّ على غياب `role_removal_blocked`، لا على تشغيل. يُحسم في المرحلة ١.

### ٣.٣ تزامن الأدوار

`slot_lock` معرَّف في `utils/equip_engine.py:80`. **15 موضعاً غير اختباري:**

| محمي فعلياً بـ`async with slot_lock(...)` | الموضع |
|---|---|
| `cogs/shop.py` | `:1271-1272` (temp_role_cleanup) |
| `utils/equip_engine.py` | `:94` (داخل `equip_role`/`unequip_role`) |
| `utils/level_claims.py` | `:721` |
| `utils/timed_ownership.py` | `:1850`, `:2342`, `:4123`, `:4467` |

**يعتمد على المستدعي / غير مُثبَت:** ما توثّقه `ROLE_SYNCHRONIZATION.md` — **78 موضع كتابة أدوار إنتاجي**، منها `boost.py`, `cogs/customcommands.py`, `cogs/moderation.py`, `cogs/reactionroles.py`, `cogs/tickets.py`, `cogs/twitch.py`, `cogs/mvp.py` — موثّقة صراحة بـ**«NO shared slot lock verified»**.

الوثيقة نفسها تنفي التعميم حرفياً:
> «This is a call-site inventory, **not a claim that every reachable path is synchronized**.»
> «**Do not wrap the whole call graph blindly: `slot_lock` is not reentrant.**»

**ما يُؤخذ الآن بأمان:** أقفال equip/slot في المتجر والملكية المؤقتة ومستوى progression — وهي مُختبَرة بسباق مُعاد إنتاجه.
**ما يبقى غير محسوم:** serialization عام لكل كتّاب الأدوار. **لا إعادة هيكلة أقفال واسعة** — التزاماً بطلبك ولتحذير `slot_lock` غير القابل لإعادة الدخول.

### ٣.٤ القرار A — زر Drop

**مُتحقَّق:**

| المراجعة | `DropButton` في `cogs/wallet.py` | في `wallet.py` (الجذر) |
|---|---|---|
| `main` | 2 | 2 |
| `baa6883` (PR #104) | **2 — ما زال موجوداً** | **2 — ما زال موجوداً** |
| `f9e413f` (طرف السلسلة) | **0 — محذوف** | **0 — محذوف** |

حذفه commit **`cb54484`** (2026-10-30) «Implement bounded delivery safety fixes…» — **خارج PR #104، داخل الـ46**.

**العواقب:** المحذوف هو السطح فقط (`DropButton`, `DropConfirmView`, `build_drop_confirm_embed`, `open_drop_confirm`, والاستيرادات). الدالة `drop_item` **باقية** في `utils/inventory.py:177` و`inventory.py:123`، وما زالت مستخدمة في `scripts/test_level_ops_provenance.py` و`scripts/test_wallet_phase2.py`.

**الحكم:** **PR #104 لا يمسّ زر Drop** — السلوك الحالي محفوظ تلقائياً إذا اقتصر الدمج عليه. قرار المنتج لا يُلزم إلا عند أخذ `cb54484`. **أبقيتُ السلوك الحالي افتراضياً** ولم أوافق على أي تغيير منتج.

### ٣.٥ القرار E — فشل هجرة `entry_key`

**الكود حرفياً** (`database.py:1552-1563` على `c014328f`):

```python
cursor = await db.execute("PRAGMA table_info(equipped_roles)")
if "entry_key" not in {c[1] for c in await cursor.fetchall()}:
    try:
        await db.execute("ALTER TABLE equipped_roles ADD COLUMN entry_key TEXT")
    except Exception as e:
        raise RuntimeError(
            "Required migration equipped_roles.entry_key failed; "
            "startup aborted to protect ownership identity"
        ) from e
```

**الأثر التشغيلي:** فشل `ALTER` يرفع `RuntimeError` من داخل `init_db()` ← **البوت لا يقلع**. على Railway هذا يعني حلقة إعادة تشغيل (crash-loop) و**توقّف كامل للخدمة**، لا تدهور جزئي.

**لماذا هو مقصود:** البديل أسوأ — الإقلاع بلا `entry_key` يعني أن فتحة التجهيز لا تستطيع تمييز «Ocean Blue» الدائم عن المؤقت، فيُلبَس دور خاطئ أو يُسحب دور صحيح. التوقف protects هوية الملكية.

**نطاقه:** `entry_key` = **0 ملف على `main`**، 14 على `c014328f`. أدخله `1f1c62d` — **داخل PR #104**. أي أن **القرار E ينطبق على PR #104 أيضاً**، بخلاف A وR1.

**التخفيف:** الهجرة **إضافية وidempotent** — محمية بفحص `PRAGMA table_info` أولاً، و`NULL` على الصفوف القائمة تُقرأ كـ«ملكية دائمة قديمة، طابِق بالاسم». الفشل العملي الوحيد المتوقع: قرص للقراءة فقط، أو جدول مقفل، أو ملف قاعدة تالف.

**كيف يُتحقَّق بأمان دون لمس بيانات الإنتاج:** الاختبار موجود فعلاً — `test_delivery_safety.py:141-152` (`test_required_alter_failure_aborts_startup`) يحاكي قرصاً للقراءة فقط عبر `patch.object(aiosqlite.Connection,"execute",…)` ويتحقق من: رفع `RuntimeError`، **وعدم** إضافة العمود، **وسلامة** الصف القديم `("Legacy", 55)`. أي أنه يُجرى على قاعدة معزولة entirely. يُستكمل في المرحلة ١ بنسخة من قاعدة الإنتاج (لا الأصل) لقياس زمن `ALTER` على الحجم الحقيقي.

---

## ٤. جرد الدمج

### ٤.A — كود واختبارات إنتاجية **مطلوبة**

**الخيار ١ (الحد الأدنى المتماسك) = PR #104 كما هو، 20 ملفاً:**

| الملف | لماذا مطلوب | اعتماده |
|---|---|---|
| `utils/timed_ownership.py` (A) | **حجر الزاوية** — نموذج الملكية المؤقتة | stdlib + `aiosqlite` + `database.DB_PATH` فقط |
| `database.py` (M) | مخطط `timed_holdings` + هجرة `entry_key` (القرار E) | — |
| `cogs/shop.py` (M) | يستورده على مستوى الوحدة (`:12`) — **صلب** | timed_ownership |
| `cogs/wallet.py` (M) | يستورده على مستوى الوحدة (`:21`) — **صلب** | timed_ownership |
| `utils/equip_engine.py` (M) | `slot_lock` + فتحة التجهيز | timed_ownership (كسول `:171`) |
| `utils/inventory.py` (M) | قرّاء المخزون | timed_ownership (كسول `:282`) |
| `utils/level_claims.py` (M) | `SCHEMA_STATEMENTS` + قفل المستوى | timed_ownership (كسول `:210`) |
| `utils/reward_engine.py` (M) | محرّك المكافآت | timed_ownership (كسول `:382`) |
| `utils/potion_engine.py`, `utils/shop_validation.py`, `utils/title_engine.py`, `utils/rank_card_data.py`, `cogs/trade.py` (M) | تماسك المتجر/العناصر | لا تستورد timed_ownership |
| `dashboard/templates/systems/shop.html` (M) | الواجهة الوحيدة المتغيّرة (+38/−11) | — |
| `test_timed_ownership.py`, `test_inventory_ui.py` (A) + 5 اختبارات مُعدَّلة | التغطية | — |

**لماذا لا يمكن تصغير هذا أكثر:** `cogs/shop.py` و`cogs/wallet.py` يستوردان `timed_ownership` **على مستوى الوحدة** — حذف أحدهما يُفشل الإقلاع. و`utils/timed_ownership.py` لا يعتمد على أي وحدة جديدة أخرى، فهو جذر السلسلة.

**ما يستبعده هذا الخيار تلقائياً:** المسار المعرّض R1 (لا يوجد هنا)، وحذف زر Drop (القرار A)، و`role_removal_blocked` (نطاق R2).

**الخيار ٢ (السلسلة الكاملة) = 113 ملفاً، 53 commit.** يضيف كل التقسية (سباق refund-vs-delivery، entitlements الآمنة، A–F)، لكنه **يُدخل R1 المعرّض ← Design C يصبح إلزامياً في نفس الوحدة**، ويُدخل `cb54484` ← **قرار A يصبح لازماً**.

### ٤.B — اختياري، قابل للاستبعاد دون كسر السلسلة

> **⚠️ تصحيح على هذا الجدول — نسخة سابقة منه كانت خاطئة.**
> صنّفتُ أولاً `utils/economy_safe.py` و`utils/xp_calculator.py` كقابلين للاستبعاد بحجة أنهما «لا يستوردان `timed_ownership`». **الحجة صحيحة والنتيجة خاطئة**: الاقتران هنا ليس عبر الاستيراد بل عبر **توقيع الدوال والرموز الجديدة** التي يستهلكها `cogs/shop.py`. أُعيد الفحص بالاعتماد العكسي، والتصحيح أدناه.

**ما ثبت بالفحص (الاعتماد العكسي، `main` → `f9e413f`):**

| الملف | رموز جديدة | من يستهلكها | الحكم |
|---|---|---|---|
| `utils/economy_safe.py` | توقيع `safe_decrement_stock` **تغيّر**: `(item_id)` ← `(item_id, db=None)` | `cogs/shop.py:358` → `safe_decrement_stock(iid, db=db)` | **مطلوب للخيار ٢** |
| `utils/xp_calculator.py` | `grant_xp_boost_details` (**0** على `main`، **0** على `baa6883`، **3** على `f9e413f`) | `cogs/shop.py:866-868` | **مطلوب للخيار ٢** |
| `utils/prestige.py` | **0** رمز جديد | يستهلك `role_removal_blocked` (`:634`) | اختياري |
| `utils/level_test_reset.py` | **0** رمز جديد | **غير مستورد من أي ملف إنتاجي** | **قابل للاستبعاد فعلاً** |
| `cogs/boost.py`, `cogs/mvp.py`, `cogs/twitch.py`, `wallet.py` | **0** رمز جديد | تستهلك `role_removal_blocked` (استيراد كسول داخل الدوال) | اختياري — **لكن استبعادها يُسقط حماية R2** |

**العاقبة المحددة للاستبعاد الخاطئ:** أخذ `cogs/shop.py` من `f9e413f` مع `utils/economy_safe.py` من `main` يُنتج عند `:358`:
```
TypeError: safe_decrement_stock() got an unexpected keyword argument 'db'
```
أي **تعطّل مسار الشراء ذو المخزون المحدود**. وبالمثل `grant_xp_boost_details` غائبة ← `AttributeError`/`ImportError` على مسار XP boost.

**التمييز الحاسم بين الخيارين:**
- **الخيار ١ (PR #104):** `economy_safe.py` و`xp_calculator.py` **ليستا ضمن العشرين أصلاً**، و`baa6883:cogs/shop.py:350` يستدعي `safe_decrement_stock(iid)` **بدون** `db`، و`:559` يستدعي `grant_xp_boost` (الموجودة على `main`) لا `_details`. ⇒ **PR #104 متسق ذاتياً ولا ينكسر.**
- **الخيار ٢ (السلسلة الكاملة):** الاثنتان **إلزاميتان**.

**القابل للاستبعاد فعلاً دون كسر:**

| المادة | السبب المُثبت |
|---|---|
| `docs/execution-20261030/**` (61) | توثيق وسجلات، لا كود |
| `recovery/*` (2) | أرشيف لقطة، ثبت أنه زائد (28/28 بصمة) |
| `utils/level_test_reset.py` | 0 رمز جديد، 0 مستورد إنتاجي |
| `AGENTS.md`, `CLAUDE.md`, `TEST_BASELINE.md` | توثيق |
| `.gitignore` (+4) | يضيف `preview_p4_*.png` — غير ضار |
| حذف `preview_p4_*.png` (5) + `__pycache__/*.pyc` (3) | تنظيف، لا أثر تشغيلي |
| `.github/workflows/tests.yml` | CI لا كود تشغيل — **يُنصح بأخذه** لأن `main` بلا workflows |

**تحفّظ صريح:** فحصتُ الاعتماد العكسي لـ**الرموز الجديدة** في الملفات المُعدَّلة. لم أفحص كل تعديل سلوكي داخل كل ملف — فقد يكون تعديلٌ بلا رمز جديد لازماً لسبب دلالي. يُحسم بمراجعة الفروق في المرحلة ١.

### ٤.C — يبقى **خارج** مشروع الإنتاج

| المادة | العدد | السبب |
|---|---|---|
| `docs/execution-20261030/precommit-logs/` | **52** | سجلات تنفيذ تاريخية — دليل مراجعة، لا كود |
| `docs/execution-20261030/*` الأخرى | **9** | تقارير ونقاط تنفيذ و`role-writers.csv` |
| `recovery/nilive-snapshot-16706-unreviewed.tar.gz` + `.manifest.json` | **2** | أرشيف لقطة غير مُراجعة — **وثبت أنه زائد عن الحاجة** (28/28 بصمة تطابق نسخاً مُودَعة) |
| `__pycache__/*.pyc` المتتبَّعة على `main` | 3 | لا يجب أن تكون متتبَّعة أصلاً |
| حزمة Design C (`review/g-purchase-grant-ledger`) | 22 | `repo-files/` **نسخ مكررة** من blobs الفرع؛ تُؤخذ من الـpatch لا من النسخ |

**ملاحظة على حزمة Design C:** تُعلن `MANIFEST.txt` **11** ملفاً في `repo-files/` وفيها **10** — `.github/workflows/tests.yml` غائب من شجرة الحزمة، والـblob المذكور في `blob-verification.txt` (`4466b90c…`) يعيد `could not get object info`. لا تعتمد على `tests.yml` من الحزمة.

---

## ٥. تسلسل الدمج المقترح ولماذا هو آمن

**التسلسل — مبنيّ على نتيجة §٣.١:**

```
الخطوة ١  فرع تجميع من main الحالي (a6b812b)
الخطوة ٢  PR #104 كما هو: dbf772f..baa6883  (7 commits، 20 ملفاً)
          ├─ لا يحوي rollback_item_inventory_grant  → لا حاجة لـDesign C
          ├─ لا يحوي DropButton removal              → القرار A غير مُلزم
          ├─ لا يحوي role_removal_blocked            → نطاق R2 غير مُدخل
          └─ يحوي entry_key migration                → القرار E ينطبق ويُختبر
الخطوة ٣  تحقق معزول كامل (البند ٦) + CI فعلي على الـSHA
الخطوة ٤  مراجعة بشرية للفروق → دمج ← نشر
```

**لماذا هذا الترتيب آمن:**
1. **merge-base هو طرف `main` الحالي** (`a6b812b`) و**behind = 0** ← لا إعادة أساس ولا تعارض زمني.
2. **السلسلة خطية** (0 merge commits) ← لا مفاجآت في history.
3. **الوحدة مُختبَرة ذاتياً**: الـ7 commits تنتهي بـ`baa6883` = commit اختباري يغطي الملكية المؤقتة وColors وpurchase-saga.
4. **يفصل المخاطر غير المحسومة** (R1، R2، القرار A) عن أول شريحة — فلا يصل مسار معرّض إلى الإنتاج قبل حمايته.
5. **لا يُدخل Design C قبل أوانه**: Design C يعالج `5100a5f`، وهو غير موجود في هذه الشريحة. إدخاله الآن يعني patch على كود لا يحوي المسار.

**المرحلة التالية (بعد إثبات الخطوة ٤):** الـ46 commit **كوحدة واحدة مع Design C**، لا مجزّأة. السبب: `5100a5f` يُدخل المسار المعرّض، و`cb54484` يحذف زر Drop، وكلاهما يحتاج قراراً. **لا يجوز أخذ `5100a5f` دون Design C في نفس الوحدة.**

**استبعاد صريح:** أعمال تحويل الصور إلى إيموجي وPR #103 **خارج هذا المسار** — #103 مدموج منذ 2026-10-23 وهو طرف `main` الحالي.

---

## ٦. مصفوفة الاختبار — المتوقع مقابل الفعلي

**تحذير منهجي:** في المرحلة ٠ **لم أُشغّل أي اختبار** — الفحص للقراءة فقط. عمود «الفعلي» أدناه يميّز ثلاثة مصادر مختلفة، ولا يخلط بينها:
- **[شُغّل]** = نفّذتُه بنفسي في هذه الجلسة
- **[تاريخي]** = سجلات/نتائجagents آخرين، **لم أتحقق منها بتشغيل**
- **[لم يُشغّل]** = مطلوب في المرحلة ١

| # | الحالة | المتوقع | الفعلي | المصدر |
|---|---|---|---|---|
| 1 | خط الأساس على `main` @ `a6b812b` | 4 مصفوفات = `TEST_BASELINE.md` | 10/39 pytest، 25/39 script، 67/71 `scripts/` Py، 18/18 Node | **[تاريخي]** قياس سابق هذه الجلسة، 513 تشغيلاً. **لم يُعَد الآن** |
| 2 | طرف PR #104 `baa6883` | لا انحدار عن `main` | — | **[لم يُشغّل]** |
| 3 | Design C على أساسه الصحيح | ينطبق على `c014328f` | **10/11 بصمة ما-قبل-صورة مطابقة** (الثامن توثيقي) | **[شُغّل]** تحقق بصمات هذه المرحلة |
| 4 | Design C: 29 اختباراً | 29 passed | 29 passed / 26.15s | **[تاريخي]** تشغيل سابق هذه الجلسة |
| 5 | **R1 — مُعيد `c014328f` الأصلي** (3,099 B) على كود **غير** مُرقّع | exit 1، `BLOCKER: old rollback removed newer units` | exit 1 | **[تاريخي]** A/B سابق |
| 6 | **R1 — نفس المُعيد** على كود **مُرقّع** بـDesign C | exit 0، `OK: no unprovable deduction on any path` | exit 0 + رفض الاسترجاع (`consumed_before_rollback`) | **[تاريخي]** A/B سابق |
| 7 | **R1 — نسخة الحزمة المُعدَّلة** (5,671 B) | ترفض إن وُجد `rollback_item_inventory_grant` | — | **[لم يُشغّل]** — **الفرق الدلالي:** تفترض ledger موجوداً؛ لا تعمل على كود بلا ledger |
| 8 | R1 على **طرف PR #104** | **غير قابل للتشغيل** — الدالة غائبة | `rollback_item_inventory_grant` = **0** عند `baa6883` | **[شُغّل]** فحص هذه المرحلة |
| 9 | كمية المخزون صفر + كل القرّاء | لا طرح غير قابل للإثبات | — | **[لم يُشغّل]** |
| 10 | إعادة محاولة بعد التزام بنفس `op_id` | idempotent — لا منح مزدوج | — | **[لم يُشغّل]** |
| 11 | كتابات متزامنة + `database is locked` | لا فقدان، retry آمن | — | **[لم يُشغّل]** |
| 12 | هجرة `entry_key` + نسخ/استعادة | إضافية، idempotent، الصفوف القديمة `NULL` | اختبار موجود (`test_delivery_safety.py:141-152`) يغطي فشل `ALTER` | **[لم يُشغّل]** — الاختبار موجود، لم أُشغّله |
| 13 | فشل `ALTER` ⇒ إحباط الإقلاع | `RuntimeError` + سلامة البيانات | مغطى نظرياً بالاختبار #12 | **[لم يُشغّل]** |
| 14 | **R2** على `f9e413f` | exit 1 (BLOCKER) | exit 1، `BLOCKER: source ended under unknown Booster eligibility…` | **[تاريخي]** تحققتُ منه سابقاً؛ **Design C لا يلمسه** |
| 15 | **R2** على `baa6883` | غير محسوم | `role_removal_blocked` = **0** عند `baa6883` → المسار غير مكتمل | **[شُغّل]** فحص وجود، **لا تشغيل للمُعيد** |
| 16 | حدود تزامن الأدوار | 78 موضعاً، المُثبَت منها أقفال equip/slot فقط | 15 موضع `slot_lock` غير اختباري؛ الباقي «NO shared slot lock verified» | **[شُغّل]** جرد هذه المرحلة |
| 17 | CI على الـSHA المدموج فعلياً | أخضر | **0 تشغيل** على `baa6883` / `f9e413f` / `8eb8e3d` | **[شُغّل]** فحص API |

**ملاحظة على البنود [تاريخي]:** نتائج #1 و#4 و#5 و#6 و#14 قِستها بنفسي **في دورة سابقة من هذه الجلسة**، لكن البيئة صُفّرت وفُقدت السجلات الخام (`/tmp/bl-*`, `/tmp/r1_*.log`, `/tmp/r2_*.log`). الأرقام محفوظة، **الأدلة الخام لم تعد متاحة**. لهذا لا أعاملها كأدلة قائمة بذاتها في حكم المرحلة ٠ — **تُعاد في المرحلة ١**.

---

## ٧. العوائق المتبقية والقرارات التي تحتاج موافقتك

| # | البند | الحالة | المطلوب منك |
|---|---|---|---|
| 1 | **CI لم يُشغَّل على أي طرف مرشَّح** | عائق | الموافقة على تشغيل CI فعلياً على فرع التجميع قبل الدمج |
| 2 | **حماية `main` غير قابلة للقراءة** (HTTP 403) | عائق | تأكيد هل `main` محمي، وهل الدمج يتطلب مراجعة |
| 3 | **إعداد Railway الفعلي غير متاح لي** | عائق | تأكيد هل push إلى `main` ينشر تلقائياً |
| 4 | **القرار E** — فشل الهجرة يوقف الإقلاع | يحتاج قراراً | قبول «التوقف أفضل من الإقلاع بهوية ملكية خاطئة»، أم تفضيل تدهور جزئي |
| 5 | **القرار A** — حذف زر Drop | **غير مُلزم في الخيار ١** | لا إجراء الآن إن اقتصر الدمج على PR #104. **يُطلب قبل أخذ `cb54484`** |
| 6 | **R2** — التزام Booster | غير محلول | اختيار: استبعاد نطاق الدور المشترك مؤقتاً، أم إصلاح R2 قبل أخذه |
| 7 | **تزامن الأدوار العام** | غير مُثبَت | قبول أن serialization العام يبقى مفتوحاً، بلا إعادة هيكلة أقفال |
| 8 | **نطاق الـ46 commit** | معلّق | لا يُؤخذ إلا كوحدة واحدة مع Design C، وبعد ١–٧ |

---

## ٨. الحكم

# NOT READY للدمج — **READY لتحديد النطاق، وNOT READY للتحقق المعزول بعد**

**بدقة أكبر، حكمان منفصلان:**

### ✅ READY — ما ثبت أنه جاهز
- **نطاق PR #104 (7 commits، 20 ملفاً)** مُحدَّد بدقة، ومتماسك، ومُثبَت أنه **لا يحوي** المسار المعرّض R1 ولا حذف زر Drop ولا نطاق R2.
- **Design C مُثبَت أنه مبنيّ على `c014328f` الصحيح** — 8/8 ملفات كود ببصمات مطابقة حرفياً.
- **سلوك Railway مُثبَت سلوكياً** — 39/39 refs أسلاف من `main`، وآخر نشر = طرف `main`.

### ❌ NOT READY — ما يمنع الانتقال الآن
1. **لم يُشغَّل أي اختبار في هذه المرحلة** — الفحص كان للقراءة فقط التزاماً بالحدود. مصفوفة الاختبار فيها **9 بنود [لم يُشغّل]** و**5 بنود [تاريخي] فقدت أدلتها الخام**.
2. **CI لم يُشغَّل على أي طرف مرشَّح** — `baa6883` و`f9e413f` كلاهما **0 تشغيل**.
3. **قرارك في البند ٤ (E) و٦ (R2) لم يُتَّخذ بعد** — وهما يحددان النطاق نفسه، فلا يمكن تثبيت خطة تحقق قبلهما.

**الشرط للانتقال إلى التحقق المعزول:** موافقتك على (أ) نطاق PR #104 كخطوة أولى، (ب) قبول سلوك القرار E، (ج) قرار R2، (د) تشغيل CI فعلياً.

---

## ما لم أفعله (التزاماً بالحدود)

- ❌ لم أبدأ B0/B1 ولا أي دفعة
- ❌ لم أعدّل أي ملف مصدر
- ❌ لم أستخدم `git apply` / `cherry-pick` / `merge` / `rebase`
- ❌ لم أنفّذ هجرة أو استرجاعاً على الإنتاج
- ❌ لم أنشئ commit أو فرعاً أو PR، ولم أدفع شيئاً
- ❌ لم أنشر
- ✅ PR #104 ما زال **OPEN**
- ❌ لم أُعِد بناء عمل تاريخي مفقود بالتخمين

**سلامة الأرشيف:** HEAD `a6b812be26d35f3048db39d0425374d23cd804ea`، tree `d0edba20c252ab780b7b43013e216ce36cf8db56`، 614 ملفاً مُتتبَّعاً، **0 تعديل على المصدر**، commit واحد على الفرع.

**توقفت هنا. لم أبدأ المرحلة ١.**
