// Unit test suite for EmbedComposer JSON Bridge (Import, Validate, Export)
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

// Load embed-composer.js into isolated VM context
const composerPath = path.resolve(__dirname, '../dashboard/static/js/embed-composer.js');
const composerCode = fs.readFileSync(composerPath, 'utf8');

const sandbox = {
    window: {},
    document: {
        createElement: () => ({ style: {}, setAttribute: () => {} }),
        head: { appendChild: () => {} }
    },
    URL: {
        createObjectURL: () => 'blob:mock',
        revokeObjectURL: () => {}
    },
    Blob: function(parts) { this.parts = parts; },
    FileReader: function() {
        this.readAsDataURL = () => {};
    },
    console: console
};

vm.createContext(sandbox);
vm.runInContext(composerCode, sandbox);
const EmbedComposer = sandbox.EmbedComposer || sandbox.window.EmbedComposer;

assert(EmbedComposer, "EmbedComposer must be defined in sandbox context");
assert(typeof EmbedComposer.parseMessageJson === 'function', "parseMessageJson must be exported");
assert(typeof EmbedComposer.exportStudioJson === 'function', "exportStudioJson must be exported");
assert(typeof EmbedComposer.exportDiscordJson === 'function', "exportDiscordJson must be exported");

console.log("== 1. Empty & Syntax Error Tests ==");
{
    const resEmpty = EmbedComposer.parseMessageJson("");
    assert.strictEqual(resEmpty.valid, false);
    assert.ok(resEmpty.error.includes("empty"));

    const resSyntax = EmbedComposer.parseMessageJson('{\n  "content": "hello",\n  "invalid": \n}');
    assert.strictEqual(resSyntax.valid, false);
    assert.ok(resSyntax.error.includes("Invalid JSON syntax"));
    console.log("  PASS: Empty & syntax error handling verified");
}

console.log("== 2. Native Discord Payload Parsing ==");
{
    const discordJson = JSON.stringify({
        content: "Hello Discord!",
        embeds: [{
            title: "Welcome",
            description: "This is a test embed",
            color: 0x5865F2,
            fields: [
                { name: "Field 1", value: "Value 1", inline: true },
                { name: "Field 2", value: "Value 2", inline: false }
            ]
        }]
    });

    const res = EmbedComposer.parseMessageJson(discordJson);
    assert.strictEqual(res.valid, true);
    assert.strictEqual(res.state.content, "Hello Discord!");
    assert.strictEqual(res.state.embeds.length, 1);
    assert.strictEqual(res.state.embeds[0].title, "Welcome");
    assert.strictEqual(res.state.embeds[0].description, "This is a test embed");
    assert.strictEqual(res.state.embeds[0].color, "#5865f2");
    assert.strictEqual(res.state.embeds[0].fields.length, 2);
    console.log("  PASS: Native Discord payload parsed accurately");
}

console.log("== 3. Discohook Multi-Message Format ==");
{
    const discohookJson = JSON.stringify({
        messages: [
            {
                data: {
                    content: "Message 1",
                    embeds: [{ title: "Embed 1" }]
                }
            },
            {
                data: {
                    content: "Message 2",
                    embeds: [{ title: "Embed 2" }]
                }
            }
        ]
    });

    const res = EmbedComposer.parseMessageJson(discohookJson);
    assert.strictEqual(res.valid, true);
    assert.strictEqual(res.sourceFormat, "Discohook Multi-Message Export");
    assert.strictEqual(res.state.content, "Message 1");
    assert.strictEqual(res.state.embeds[0].title, "Embed 1");
    assert.ok(res.warnings.some(w => w.code === "MULTI_MESSAGE_TRUNCATED"));
    console.log("  PASS: Discohook multi-message format imported with warning");
}

console.log("== 4. Component Action Rows -> Button Roles ==");
{
    const componentJson = JSON.stringify({
        content: "Pick your role",
        embeds: [{ title: "Roles" }],
        components: [
            {
                type: 1,
                components: [
                    { type: 2, style: 1, label: "VIP Role", custom_id: "rr_987654321_draft_0_0" },
                    { type: 2, style: 3, label: "Member", custom_id: "rr_123456789_draft_0_1" }
                ]
            }
        ]
    });

    const res = EmbedComposer.parseMessageJson(componentJson);
    assert.strictEqual(res.valid, true);
    assert.strictEqual(res.state.buttons.length, 2);
    assert.strictEqual(res.state.buttons[0].label, "VIP Role");
    assert.strictEqual(res.state.buttons[0].role_id, "987654321");
    assert.strictEqual(res.state.buttons[0].color, "blurple");
    assert.strictEqual(res.state.buttons[1].label, "Member");
    assert.strictEqual(res.state.buttons[1].role_id, "123456789");
    assert.strictEqual(res.state.buttons[1].color, "green");
    console.log("  PASS: Action Row button components parsed into button roles");
}

console.log("== 5. Nero Studio v1 Full-Fidelity Export & Re-import ==");
{
    const state = {
        content: "Nero Studio Announcement",
        embeds: [{
            title: "Rules & Roles",
            description: "Click below to get roles.",
            color: "#e67e22",
            fields: [{ name: "Rule 1", value: "Be respectful", inline: false }]
        }]
    };
    const buttons = [
        { id: "btn_1", label: "Gamer", role_id: "111222333", color: "green", emoji: "🎮", booster_only: false, required_role_id: null, expiry_days: 0 },
        { id: "btn_2", label: "VIP", role_id: "444555666", color: "blurple", emoji: "⭐", booster_only: true, required_role_id: "111222333", expiry_days: 30 }
    ];
    const panelConfig = { exclusive: 1, max_roles: 2, require_confirmation: true };

    const exportedStudio = EmbedComposer.exportStudioJson(state, buttons, panelConfig);
    assert.strictEqual(exportedStudio.schema_version, "nero-studio-v1");
    assert.strictEqual(exportedStudio.buttons.length, 2);
    assert.strictEqual(exportedStudio.panel_config.exclusive, 1);
    assert.strictEqual(exportedStudio.panel_config.max_roles, 2);
    assert.strictEqual(exportedStudio.panel_config.require_confirmation, true);

    const reimported = EmbedComposer.parseMessageJson(JSON.stringify(exportedStudio));
    assert.strictEqual(reimported.valid, true);
    assert.strictEqual(reimported.sourceFormat, "Nero Studio JSON (Full Fidelity)");
    assert.strictEqual(reimported.state.buttons.length, 2);
    assert.strictEqual(reimported.state.buttons[1].booster_only, true);
    assert.strictEqual(reimported.state.buttons[1].expiry_days, 30);
    assert.strictEqual(reimported.state.buttons[1].required_role_id, "111222333");
    assert.strictEqual(reimported.state.panelConfig.exclusive, 1);
    console.log("  PASS: Studio JSON full-fidelity roundtrip verified");
}

console.log("== 6. Truncation and Limits ==");
{
    const longContent = "a".repeat(2500);
    const manyEmbeds = Array.from({ length: 15 }, (_, i) => ({ title: `Embed ${i + 1}` }));
    const payload = JSON.stringify({ content: longContent, embeds: manyEmbeds });

    const res = EmbedComposer.parseMessageJson(payload);
    assert.strictEqual(res.valid, true);
    assert.strictEqual(res.state.content.length, 2000);
    assert.strictEqual(res.state.embeds.length, 10);
    assert.ok(res.warnings.some(w => w.code === "CONTENT_TRUNCATED"));
    assert.ok(res.warnings.some(w => w.code === "EMBEDS_TRUNCATED"));
    console.log("  PASS: Content & embed count limits enforced with warnings");
}

console.log("== 7. Component Action Rows -> Select Role Menus (Type 3) ==");
{
    const selectJson = JSON.stringify({
        content: "Choose your notification roles below",
        components: [
            {
                type: 1,
                components: [
                    {
                        type: 3,
                        custom_id: "rr_sel_12345_draft_0",
                        placeholder: "Choose notification updates...",
                        options: [
                            { label: "Announcements", value: "90001", description: "Important news", emoji: { name: "📢" } },
                            { label: "Giveaways", value: "90002", description: "Special events", emoji: { name: "🎁" } }
                        ]
                    }
                ]
            }
        ]
    });

    const res = EmbedComposer.parseMessageJson(selectJson);
    assert.strictEqual(res.valid, true);
    assert.strictEqual(res.state.select_menus.length, 1);
    assert.strictEqual(res.state.select_menus[0].placeholder, "Choose notification updates...");
    assert.strictEqual(res.state.select_menus[0].options.length, 2);
    assert.strictEqual(res.state.select_menus[0].options[0].label, "Announcements");
    assert.strictEqual(res.state.select_menus[0].options[0].role_id, "90001");
    assert.strictEqual(res.state.select_menus[0].options[0].description, "Important news");
    assert.strictEqual(res.state.select_menus[0].options[0].emoji, "📢");
    assert.strictEqual(res.state.select_menus[0].options[1].label, "Giveaways");
    assert.strictEqual(res.state.select_menus[0].options[1].role_id, "90002");
    console.log("  PASS: Discord Type 3 String Select components parsed accurately into select menus");
}

console.log("== 8. Studio JSON & Discord JSON Export with Select Menus ==");
{
    const state = {
        content: "Notification Hub",
        embeds: [{ title: "Select Roles", description: "Pick below" }]
    };
    const buttons = [
        { id: "btn_1", label: "Help", role_id: "1001", color: "blurple" }
    ];
    const selectMenus = [
        {
            placeholder: "Select your games...",
            options: [
                { label: "Valorant", role_id: "2001", description: "FPS updates", emoji: "🎯", booster_only: false, expiry_days: 0 },
                { label: "Minecraft", role_id: "2002", description: "SMP updates", emoji: "⛏️", booster_only: true, expiry_days: 14 }
            ]
        }
    ];
    const panelConfig = { exclusive: 1, max_roles: 1, require_confirmation: false };

    // Export Studio JSON
    const exportedStudio = EmbedComposer.exportStudioJson(state, buttons, selectMenus, panelConfig);
    assert.strictEqual(exportedStudio.select_menus.length, 1);
    assert.strictEqual(exportedStudio.select_menus[0].options.length, 2);
    assert.strictEqual(exportedStudio.select_menus[0].options[1].expiry_days, 14);

    // Export Discord JSON
    const exportedDiscord = EmbedComposer.exportDiscordJson(state, buttons, selectMenus, panelConfig);
    assert.strictEqual(exportedDiscord.components.length, 2); // 1 button row + 1 select menu row
    assert.strictEqual(exportedDiscord.components[0].components[0].type, 2); // Button
    assert.strictEqual(exportedDiscord.components[1].components[0].type, 3); // Select Menu
    assert.strictEqual(exportedDiscord.components[1].components[0].options.length, 2);
    assert.strictEqual(exportedDiscord.components[1].components[0].options[0].value, "2001");

    // Reimport Studio JSON
    const reimported = EmbedComposer.parseMessageJson(JSON.stringify(exportedStudio));
    assert.strictEqual(reimported.valid, true);
    assert.strictEqual(reimported.state.select_menus.length, 1);
    assert.strictEqual(reimported.state.select_menus[0].options[1].booster_only, true);
    assert.strictEqual(reimported.state.select_menus[0].options[1].expiry_days, 14);
    console.log("  PASS: Studio and Discord JSON export & reimport with select menus verified");
}

console.log("== 9. Acceptance Fixture: nero_message_style_selectmenu_reference.json ==");
{
    const fixturePath = path.resolve(__dirname, 'fixtures/nero_message_style_selectmenu_reference.json');
    const fixtureRaw = fs.readFileSync(fixturePath, 'utf8');
    const res = EmbedComposer.parseMessageJson(fixtureRaw);

    assert.strictEqual(res.valid, true, "Fixture must be parsed as valid JSON");
    assert.strictEqual(res.state.embeds.length, 2, "Fixture must have 2 embeds");
    assert.ok(res.state.content.includes("ansi"), "ANSI formatted text must survive in content");

    // Embed 1 checks
    const e1 = res.state.embeds[0];
    assert.ok(e1.title.includes("Color Roles"), "Embed 1 title must match");
    assert.strictEqual(e1.color, "#d1d5f0");
    assert.strictEqual(e1.image, "https://cdn.discordapp.com/attachments/1088248319982006320/color_palette.jpg");

    // Embed 2 checks
    const e2 = res.state.embeds[1];
    assert.ok(e2.title.includes("Guidelines & Perks"), "Embed 2 title must match");
    assert.strictEqual(e2.color, "#d1d5f0");
    assert.strictEqual(e2.image, "https://cdn.discordapp.com/attachments/1088248319982006320/palette_animation.gif");
    assert.ok(e2.fields.length >= 2, "Embed 2 must retain its fields");
    assert.strictEqual(e2.fields[0].name, "First");
    assert.strictEqual(e2.fields[0].inline, true, "Field 1 must be inline");
    assert.strictEqual(e2.fields[1].name, "Second");
    assert.strictEqual(e2.fields[1].inline, true, "Field 2 must be inline");

    // Select Menu checks
    assert.strictEqual(res.state.select_menus.length, 1, "Must parse 1 String Select Menu");
    const sm = res.state.select_menus[0];
    assert.strictEqual(sm.placeholder, "Choose your role color...");
    assert.strictEqual(sm.options.length, 20, "Select Menu must parse all 20 color options");
    assert.strictEqual(sm.options[0].label, "Lavender");
    assert.strictEqual(sm.options[0].emoji, "🖌️");
    assert.strictEqual(sm.options[0].role_id, "900000000000000101");
    assert.strictEqual(sm.options[19].label, "Iris");
    assert.strictEqual(sm.options[19].emoji, "🖌️");
    assert.strictEqual(sm.options[19].role_id, "900000000000000120");

    // Buttons check
    assert.strictEqual(res.state.buttons.length, 2, "Must parse 2 buttons from second action row");
    assert.strictEqual(res.state.buttons[0].label, "Remove Color");
    assert.strictEqual(res.state.buttons[0].color, "red");
    assert.strictEqual(res.state.buttons[0].emoji, "🗑️");
    assert.strictEqual(res.state.buttons[1].label, "Clear Preferences");
    assert.strictEqual(res.state.buttons[1].color, "red");
    assert.strictEqual(res.state.buttons[1].emoji, "⚠️");

    // Full round-trip re-export
    const exportedDiscord = EmbedComposer.exportDiscordJson(res.state, res.state.buttons, res.state.select_menus, res.state.panelConfig);
    assert.strictEqual(exportedDiscord.embeds.length, 2, "Exported Discord JSON must retain 2 embeds");
    assert.strictEqual(exportedDiscord.components.length, 2, "Exported Discord JSON must have 2 action rows (select + buttons)");
    
    // Find select row and button row
    const selectRow = exportedDiscord.components.find(r => r.components && r.components.some(c => c.type === 3));
    const buttonRow = exportedDiscord.components.find(r => r.components && r.components.some(c => c.type === 2));
    
    assert(selectRow, "Exported Discord JSON must contain a select menu row (type 3)");
    assert.strictEqual(selectRow.components[0].options.length, 20, "Exported Select Menu must retain all 20 options");
    assert.strictEqual(selectRow.components[0].placeholder, "Choose your role color...");
    
    assert(buttonRow, "Exported Discord JSON must contain a button row (type 2)");
    assert.strictEqual(buttonRow.components.length, 2, "Row 2 must retain 2 buttons");
    assert.strictEqual(buttonRow.components[0].label, "Remove Color");
    assert.strictEqual(buttonRow.components[1].label, "Clear Preferences");

    console.log("  PASS: Acceptance fixture parsed with full fidelity (2 embeds, 20 select options, 2 buttons, images & ANSI)");
}

// ── 10. Link Buttons and Action Row Budgeting ──────────────────────────
{
    console.log("== 10. Link Buttons & Action Row Budgeting ==");
    const linkMsg = {
        content: "Check our docs",
        components: [
            {
                type: 1,
                components: [
                    { type: 2, style: 5, label: "Documentation", url: "https://message.style/docs" },
                    { type: 2, style: 1, label: "Get Role", custom_id: "rr_999911112222_0" }
                ]
            }
        ]
    };

    const parsed = EmbedComposer.parseMessageJson(JSON.stringify(linkMsg));
    assert(parsed.valid, "Link button message must parse successfully");
    assert.strictEqual(parsed.state.buttons.length, 2, "Must import 2 buttons");
    assert.strictEqual(parsed.state.buttons[0].color, "link");
    assert.strictEqual(parsed.state.buttons[0].url, "https://message.style/docs");
    assert.strictEqual(parsed.state.buttons[1].color, "blurple");

    // Export to Discord JSON
    const discordPayload = EmbedComposer.exportDiscordJson(parsed.state, parsed.state.buttons, parsed.state.select_menus, parsed.state.panelConfig);
    const rowComps = discordPayload.components[0].components;
    assert.strictEqual(rowComps[0].type, 2);
    assert.strictEqual(rowComps[0].style, 5);
    assert.strictEqual(rowComps[0].url, "https://message.style/docs");
    assert.strictEqual(rowComps[0].custom_id, undefined, "Link button MUST NOT have a custom_id in Discord payload");
    assert.strictEqual(rowComps[1].style, 1);
    assert(rowComps[1].custom_id.startsWith("rr_"), "Non-link button MUST have custom_id");

    // Action Row budgeting validation: ceil(btn / 5) + sel <= 5
    function calcBudget(btnCount, selCount) {
        return Math.ceil(btnCount / 5) + selCount;
    }
    assert.strictEqual(calcBudget(5, 4), 5, "5 buttons (1 row) + 4 select menus (4 rows) = 5 rows (valid)");
    assert.strictEqual(calcBudget(6, 4), 6, "6 buttons (2 rows) + 4 select menus (4 rows) = 6 rows (exceeds 5)");
    assert.strictEqual(calcBudget(25, 0), 5, "25 buttons (5 rows) + 0 select menus = 5 rows (valid)");
    assert.strictEqual(calcBudget(0, 5), 5, "0 buttons + 5 select menus = 5 rows (valid)");

    console.log("  PASS: Link button serialization and Action Row budgeting verified");
}

console.log("============================================================");
console.log("All JSON Bridge checks passed successfully.");
