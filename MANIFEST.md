# Nero Bot Delivery Manifest

Authoritative inventory of files delivered in `nero-bot-delivery.zip`.

## Verification Instructions
To verify archive integrity against this manifest:
```bash
awk -F'|' '/^\| `/ {gsub(/[` ]/, "", $2); gsub(/[` ]/, "", $4); if ($2 != "" && $4 != "") print $4 "  " $2}' MANIFEST.md | sha256sum -c -
```

## Inventory

| File Path | Size (Bytes) | SHA-256 |
| :--- | :--- | :--- |
| `auth.py` | 8,901 | `47bf8b95466fbcdfb6fbba028f9843046a113080a6694221a515e05caa9d1d03` |
| `cogs/activity_engine.py` | 7,337 | `7c0f9638324957c6f8d318cda90326a02cd1485eaf36c990b12c4aba5524b0ca` |
| `cogs/leveling.py` | 161,988 | `41822e6e32d53320b46d43fd95fe74792f139cb16769b9d43f5dcae7fbb0eff3` |
| `cogs/reactionroles.py` | 41,224 | `59d2df54b325029ca0b5fb3e2abdae576837993a9e4d02e9c9ed0571b6b19eff` |
| `cogs/welcome.py` | 10,470 | `70066d02fa4d457daa7d6a57d20ff58139c24c108beffafd244ea0fcf79bea4a` |
| `dashboard/api/core.py` | 16,768 | `ca689801de387a22fc5bd978f52d14e64b20bd6319636d0083a2b366fb288ae2` |
| `dashboard/api/creator.py` | 28,563 | `574a2402f2fd450e86b6c7e8deb0f9d1e08ea5114132376ede1226f31427bad8` |
| `dashboard/api/embedbuilder.py` | 38,518 | `b563db2af0248157e8e2365ecb5d1569f5c70383bb64d14d5e01a6dab56d8005` |
| `dashboard/api/leveling.py` | 41,748 | `2e822ec47a8418dd69524b7c35da2ac439aa0a3aff0ffa5b2ce1e380d2fb6e99` |
| `dashboard/api/moderation.py` | 19,660 | `7fdfa509b13692f65e78c48e5bb8e9573781d5ff4a445f9cc1b6a6a034e02292` |
| `dashboard/api/mvp.py` | 4,927 | `3fb83a2d1a785212ec8af58fefa1a715ff3264055007b6eebb6e567ddbe3da3e` |
| `dashboard/api/tagmissions.py` | 10,409 | `5d26bef6df7027c733eecf62d10624cdfb0e120a876b2ac69591c2571f8a928b` |
| `dashboard/app.py` | 152,052 | `6353818f855118513008d12d9c547d0fa1cf95d86b1c8a030df9bfe658b7153e` |
| `dashboard/auth.py` | 13,779 | `b8fce28abbcd1f3b9af90ac2d16c59292e40ac71167fecc8ce46a60f4bea3c46` |
| `dashboard/permissions.py` | 13,020 | `df9a1cf34db3928e97694627c9c6166b8f3def8789b9347b346aa1748b554cdf` |
| `dashboard/static/css/command-manager.css` | 24,136 | `fcc02c13043b4ee18fce6cdb371bc0dc19619454c9cb340f996faf56d79d4f36` |
| `dashboard/static/css/embed-composer.css` | 31,710 | `645b95ccf42f5394962ea0b9bc5ced0423ebc53b2f4236230e2e3dc955dded02` |
| `dashboard/static/css/main.css` | 48,730 | `f5ec00df844ae434b8773648d3d2cd6b8945139b3fd6471da88dbffdfab91bc5` |
| `dashboard/static/css/overview.css` | 16,664 | `e8cba364e1ab51bb5efda2beb45418178107f398f2331f78351a92bd10da46ad` |
| `dashboard/static/css/shop-designer.css` | 13,203 | `0e5c61097e5f66ada1f99f027be7ea17ca1239959e008dcf745b0d7ea194eb21` |
| `dashboard/static/js/dashboard-tabs.js` | 9,482 | `8df568235d6388727b798971a9dfbabf26640c6320060961d68ae7f5575eb2e9` |
| `dashboard/static/js/dashboard.js` | 54,034 | `01c6142ef6c6d6e0fb492559aecbbb64eedba63dd545555ab7903e6207b807fb` |
| `dashboard/static/js/embed-composer.js` | 72,844 | `d173fd2a3860cc2c3417bc3bbaef7ad229ee6494584191cd2acbf4dba8e00b8e` |
| `dashboard/static/js/nero-select.js` | 13,583 | `24a57aa5328235f0d46ecec8852bc08c29561b684050fc7ec9da914dc68caf09` |
| `dashboard/templates/base.html` | 45,519 | `1b4c29a543f66176a255ca01c1b8ae956e4cc5de9652442ee5114fdfcc850fed` |
| `dashboard/templates/config/access.html` | 5,994 | `435561cbb0b35ebfd36b6e370093d4ef9460eeb32b5aee30bc8be1ae608b51e1` |
| `dashboard/templates/config/announcements.html` | 2,864 | `972458ee6d08cf66143bc098118e7db386e587516898fcf5ba25d0ff926b3bef` |
| `dashboard/templates/config/boost.html` | 6,065 | `5522d1f9f421531a058ebcfd3ca865fbe7f9fe340cebe935a43219fcaa87e0ed` |
| `dashboard/templates/config/botprofile.html` | 17,889 | `c5f13b84aada25a90b90fb92d234d634fc38dc14dbae5cd7f31d75c754aa993e` |
| `dashboard/templates/config/creator.html` | 37,652 | `a2c27d14625663d290e2b2586052ffd336c0afda95e7ca08735ef4564ff89716` |
| `dashboard/templates/config/general.html` | 8,836 | `5c54779f9f2914bdbd31bb4bfb43b29be05cc7478bd1b711e511edf80e9baa24` |
| `dashboard/templates/config/welcome.html` | 8,071 | `0718f2220ccf0fa71c53e68142ec2e1eff676d751a5e8cf807732af8d50b1b21` |
| `dashboard/templates/context_switch.html` | 1,946 | `80e67054a7bfc8f798f8b3fb0bd4cd7e4d9289d899b55302f22e3e368c40bcce` |
| `dashboard/templates/general/auditlog.html` | 1,463 | `1830ae5629f4124b71fa4aeaf871b3600fdab1ee600c1723789b81c7efbd1cef` |
| `dashboard/templates/general/backups.html` | 3,834 | `683209f04f16b24371937383fe7ac68efc25731882a125f1d06632075ba8f477` |
| `dashboard/templates/general/health.html` | 5,218 | `d5828b962845840eeedafd9ab03729779acc3d877e21abddf5cfa315421a07e7` |
| `dashboard/templates/general/member_profile.html` | 9,023 | `59195a88dd85d53631c7c5743fc4adf6ef90f703c02936721178493d986eb75f` |
| `dashboard/templates/general/members.html` | 4,651 | `8853c7571a6361267a4507fa527dce446aed0cc658c9bf26b2a81ae68a087cb1` |
| `dashboard/templates/general/overview.html` | 14,603 | `2545316ea5eeb7e54bea6af6dc0025d5368bfc9eaa02d3da4e38c6f8a8728aac` |
| `dashboard/templates/general/reports.html` | 3,810 | `650abd03f3029fcf70f3b1903b152efa01868a44e61e21c3249dbf27a93d7992` |
| `dashboard/templates/login.html` | 4,096 | `7ff7f25faff4500cffa2500d06986448b9b42b41446f77c610b270ad9f09cd3a` |
| `dashboard/templates/manage/commands.html` | 60,476 | `13a8bcead1a0bbcaa692aff2a16864b851d2009a72d953d8079431b15224eb85` |
| `dashboard/templates/manage/customcommands.html` | 11,307 | `58bcd78b27ba650ec5bb866e9d4d0a73013d9dfc5a009918fb48cdc4b677cf67` |
| `dashboard/templates/manage/embedbuilder.html` | 103,547 | `8da41d266cf1fe25962dfd230829f97b29ac3bd1d6b903a8c48de243b0261a3a` |
| `dashboard/templates/manage/moderation.html` | 30,982 | `1bee955bed51c4098a2e35530142128652e5cddcbee347a8d78eec8cedd1bab6` |
| `dashboard/templates/manage/reactionroles.html` | 16,727 | `e654564265516d19c4201c1c43398c136fba3117ae9f6928737c027201868c03` |
| `dashboard/templates/manage/shopdesigner.html` | 15,316 | `40adaa95ffc871acfb4e15dd70daad2a149d2419de1706c8a3e533dcde4964bc` |
| `dashboard/templates/manage/shoppublisher.html` | 10,063 | `3194153a21b7dbace24e6f02f748328b82aae307aed12ad545a559ba2db81015` |
| `dashboard/templates/manage/tickets.html` | 22,445 | `f5f172001226ab31052ff133ab420289bc8402481ba313e30ab4010e6e007c25` |
| `dashboard/templates/manage/triggers.html` | 8,053 | `48c1f0c9f7d1cf387856f420088ee516068939fb36052a7e2438dcaed8e9b4ea` |
| `dashboard/templates/server_select.html` | 16,262 | `d508130e2f71069eef18a161cd2d6c002efaaf49f7f2c368ff03b68fc103cfdf` |
| `dashboard/templates/systems/economy.html` | 18,072 | `e9a58a48c2fd3757408bf4bd5761f4c93ffbb8372e96691a4d749e2e91e10cf7` |
| `dashboard/templates/systems/events.html` | 1,553 | `144e587b8a25ce6172e0acb0ed1a6b15b0fd55b72f8643e9e9f2509fed7a2a17` |
| `dashboard/templates/systems/inventory.html` | 4,212 | `8789201db8d2295838d967e462b0c251dbf2eaa510e4f0359caf0826be1f1ffd` |
| `dashboard/templates/systems/ledger.html` | 7,984 | `c54c99d48ad0520b6bddef0e3df685acc1bcb419ada5b34166a0c56ad8540234` |
| `dashboard/templates/systems/leveling.html` | 77,863 | `52bd3f58deaab8f7813fe87da32d20c8171def3503d47b3ce298bb2f1df1d77e` |
| `dashboard/templates/systems/minigame_builder.html` | 39,170 | `f1125bcddfd0a479f9d4064977bdc8a3ce1f39fb97158cd11b35ab4ec4b67f54` |
| `dashboard/templates/systems/minigames.html` | 40,845 | `f5bd5fd9a0840381e50c68aaad650c9bbf9b9cf45511b5975dd14010e1f9664e` |
| `dashboard/templates/systems/missions.html` | 17,813 | `89eabcd49b8586d67196e0d53d3ca5d0daa4cc98f7d9e3fd2bc6a82b7279c820` |
| `dashboard/templates/systems/mvp.html` | 9,914 | `cd713ff71b6dd8fa8a5869518dcff4bbc917ee5256b91f45813aa098f6621b7a` |
| `dashboard/templates/systems/shop.html` | 23,301 | `21bc46eda28edb833f41a009914791a0a6be5d59faff38cda432f74a1d6b67bd` |
| `dashboard/templates/systems/tagmissions.html` | 10,725 | `ed6f50a14c5b6f6522c25204391520033e33027ff59582ebc282d83bca228176` |
| `dashboard/templates/systems/tagpartners.html` | 5,794 | `09f24bca768a659fd2d70cb08bb92b518c594c7a7e810b470e14dab5912c4a58` |
| `dashboard/templates/systems/trade.html` | 5,609 | `51293c0104d92b9c84f44f9c7046d4d7f6a58c1a58ee0bb811ec1402c306cff1` |
| `dashboard/utils/role_validation.py` | 9,322 | `5b4602678a837b9fe219c41756571ac9203229ec81b5cc27ac56c66737631da8` |
| `database.py` | 110,838 | `1be7b03451bd5d56da9609eb81aab24ee4070fd62792d510c25b6e9a5a7a920a` |
| `main.css` | 48,644 | `ac11a04618e4ae059bc417c8151dde675ba67b540b68144f6f7185dd1c0f5f23` |
| `nero-select.js` | 13,583 | `24a57aa5328235f0d46ecec8852bc08c29561b684050fc7ec9da914dc68caf09` |
| `permissions.py` | 8,408 | `2e7122a28701c13e08551e36063fe8328968cb22f8b34128ef4cceaa4aa5ed8a` |
| `reactionroles.py` | 41,224 | `59d2df54b325029ca0b5fb3e2abdae576837993a9e4d02e9c9ed0571b6b19eff` |
| `scripts/fixtures/nero_message_style_selectmenu_reference.json` | 6,882 | `66278a476d44bdb3f62eb234a2b4bdbdf5d9cfd6a892539f78ba04d89e83ac5c` |
| `scripts/preview_minigames_dashboard.py` | 12,545 | `e3bc2f27c2c8fa0e98d7d7983370da22372cdeeaf08693ee24997ac13fee6d23` |
| `scripts/recover_shop_purchases.py` | 1,931 | `63ec1d2eaf8bb679366d768d5f9eddc0df710ad392a208982067620d618fdc13` |
| `scripts/test_booster_rewards.py` | 20,926 | `2b7b8cf3d95822ec3688910da98b9ee0929c66cd09cc0c5e7334a2ee99148331` |
| `scripts/test_channel_grouping_acceptance.py` | 6,071 | `b2408d7d978b0124b9fc08f65c33115e860407c41b6cc654ba64509d9d0a8a9e` |
| `scripts/test_dashboard_page_scripts.py` | 16,413 | `44ff10ace82aa1b3966ca2e98027c4524852150a2220eca00215762e99c8e221` |
| `scripts/test_embed_json_bridge.js` | 18,146 | `3a5598d7d62238dc302fd39e28d1328d359a665de727b5099f9453045a26b62e` |
| `scripts/test_leveling_reward_e2e.py` | 21,050 | `9bd0d364eaf9a65a56556d3f5d9ed7e3f39eea3806ca38ed37df110ff7a86844` |
| `scripts/test_leveling_runtime_contract.py` | 10,215 | `1f799b9dbec516ef3ebd26e4dbd8ad90307f13b5d2e649e77ead757f2a16191b` |
| `scripts/test_member_blacklist_d3.py` | 31,414 | `df3870bc45431c4c1582c2fecb4dc20d6fd0c870bdc634d0c57cde6ef3d54eed` |
| `scripts/test_moderation_smooth_acceptance.py` | 11,062 | `8c8d6008f9faeeba9ff155ab87144963baa545a64238ee0edd73e98d9a473734` |
| `scripts/test_moderation_smooth_navigation.js` | 12,843 | `fc3736364e6487d4715c7738308140c8c8cc04a10e280b2114280b40c11c1151` |
| `scripts/test_nero_select_grouping.js` | 9,872 | `7c9e6c10465afa41a17291955e745a3164575ac140178636e367095d9e0b4873` |
| `scripts/test_phase2_backend.py` | 18,999 | `090dc09e392cdf46a6605debfceccc095d285e5831b616434404c3220642ef24` |
| `scripts/test_rank_integration.py` | 16,767 | `68fc9006b560fdfe06ba4cbdcd588e93469440ceb7b382c5e95cf45caefd925a` |
| `scripts/test_select_roles_acceptance.py` | 46,778 | `ae08bbcd154fd0c86e020327c06e327215670307d50f7e67bf78d0b7b516d60a` |
| `scripts/test_studio_acceptance.py` | 70,024 | `754c74a7cb118663323581f19a7f8cb3b07dfcf4bc52d1c9b2c210c2548819fc` |
| `scripts/test_studio_select_ui_browser.js` | 9,413 | `673010a9aa76565f2e9808c3614704ffbc2b85a1934a8299c77ad54330609077` |
| `scripts/test_workstream1_acceptance.py` | 13,910 | `26cb440edea4f6999529124983af8158e7c8ab89da4c89ceeafd164b64b15ffc` |
| `scripts/test_workstream2_acceptance.py` | 8,688 | `c1466e9f893a26d9b177a48d5a2f69384fd242b87e8f4438625814ebccc21436` |
| `scripts/test_workstream2_roles_acceptance.py` | 23,003 | `78504a7520ecb3efc2e63476c684d7d0566d0e30738844cfb5df4f69d27fe2f1` |
| `scripts/test_workstream3_tables_acceptance.py` | 5,755 | `96050435184bcea6df6f8a59689412ea99f12d779c305fd8414f6f4f77c9c336` |
| `scripts/test_workstream4_tabs_acceptance.py` | 4,759 | `50c77c772f9eb5a0d987d26fb5401cfb141d87490f9615a0fa15c88dbbd8d477` |
| `tools/alias_probes/run_all.sh` | 2,202 | `578dae9e6ac0af40a59cfb385d10fe3c72de47f6a76e9342a4e595150c51a955` |
| `utils/level_claims.py` | 140,252 | `4b91ef3e19f374b0edda8e5b47ecb1dfa2559b624271c6b5032ad65d07a6d40e` |
| `utils/level_ops.py` | 83,680 | `256d77efc89174271cef92aa2d160102c8b12a219cc7e51d95825abf9e8e933b` |
| `utils/permissions.py` | 10,219 | `7a1d4568198ae29a182a5d7d8b365a2aba121d5b31e899428ac562455fd46a07` |
| `utils/reward_engine.py` | 40,034 | `2f2d8619d18c1d1d091b374271de4ad47c16f14813ad4d24d3af677f305b51f9` |
| `utils/xp_calculator.py` | 31,019 | `b82f247926edf1d85a9e7beb14377f5bfc67730ea8cffe81b18ffe283156ed95` |
| `welcome.py` | 10,470 | `70066d02fa4d457daa7d6a57d20ff58139c24c108beffafd244ea0fcf79bea4a` |
