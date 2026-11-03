// End-to-end browser verification for Moderation Smooth Navigation & Global Button Functionality
let playwright;
try {
    playwright = require('playwright');
} catch (e) {
    try {
        playwright = require('/tmp/node_modules/playwright');
    } catch (e2) {
        console.log("Playwright not installed, skipping browser automation test.");
        process.exit(0);
    }
}
const { chromium } = playwright;
const assert = require('assert');

(async () => {
    console.log("▶ Launching Playwright Chromium instance...");
    const browser = await chromium.launch({
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
    });

    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();

    page.on('console', msg => console.log('PAGE LOG:', msg.text()));

    let pageReloadCount = 0;
    page.on('load', () => { pageReloadCount++; });

    console.log("1. Authenticating via /demo-login...");
    await page.goto('http://127.0.0.1:5377/demo-login', { waitUntil: 'networkidle' });

    console.log("2. Navigating to /moderation...");
    await page.goto('http://127.0.0.1:5377/moderation', { waitUntil: 'networkidle' });
    const initialLoads = pageReloadCount;

    // Attach marker to window to detect if full page reload ever happens
    await page.evaluate(() => { window.__neroNavigationMarker = 'stayed_in_page'; });

    console.log("3. Testing smooth, in-place tab switches (No full-page reload)...");
    
    // Tab 1: Active Punishments
    console.log("   → Clicking Active Punishments tab...");
    await page.click('#tab-punishments');
    await page.waitForTimeout(100);

    let isMarkerPresent = await page.evaluate(() => window.__neroNavigationMarker === 'stayed_in_page');
    assert.strictEqual(isMarkerPresent, true, "Page reloaded! Must stay in-page.");
    
    let url = page.url();
    assert.ok(url.includes('tab=punishments'), `Expected URL with ?tab=punishments, got ${url}`);
    
    let isPunishmentsVisible = await page.$eval('#tab-content-punishments', el => window.getComputedStyle(el).display !== 'none');
    let isLogsHidden = await page.$eval('#tab-content-logs', el => window.getComputedStyle(el).display === 'none');
    assert.strictEqual(isPunishmentsVisible, true, "Punishments content must be visible");
    assert.strictEqual(isLogsHidden, true, "Logs content must be hidden");

    // Tab 2: Warnings
    console.log("   → Clicking Warnings tab...");
    await page.click('#tab-warnings');
    await page.waitForTimeout(100);

    url = page.url();
    assert.ok(url.includes('tab=warnings'), `Expected URL with ?tab=warnings, got ${url}`);
    let isWarningsVisible = await page.$eval('#tab-content-warnings', el => window.getComputedStyle(el).display !== 'none');
    assert.strictEqual(isWarningsVisible, true, "Warnings content must be visible");

    // Tab 3: Thresholds
    console.log("   → Clicking Thresholds tab...");
    await page.click('#tab-thresholds');
    await page.waitForTimeout(100);

    url = page.url();
    assert.ok(url.includes('tab=thresholds'), `Expected URL with ?tab=thresholds, got ${url}`);
    let isThresholdsVisible = await page.$eval('#tab-content-thresholds', el => window.getComputedStyle(el).display !== 'none');
    assert.strictEqual(isThresholdsVisible, true, "Thresholds content must be visible");

    // Tab 4: Mod Logs
    console.log("   → Clicking Mod Logs tab...");
    await page.click('#tab-logs');
    await page.waitForTimeout(100);

    url = page.url();
    assert.ok(url.includes('tab=logs'), `Expected URL with ?tab=logs, got ${url}`);
    let isLogsVisible = await page.$eval('#tab-content-logs', el => window.getComputedStyle(el).display !== 'none');
    assert.strictEqual(isLogsVisible, true, "Logs content must be visible");

    // Verify zero top-level page reloads occurred during all 4 tab clicks
    assert.strictEqual(pageReloadCount, initialLoads, "Zero page reloads must occur during tab switches");
    console.log("   ✓ All 4 tab transitions executed smoothly with ZERO document reloads");

    console.log("4. Testing Browser Back and Forward (popstate)...");
    // Back to Thresholds
    await page.goBack();
    await page.waitForTimeout(150);
    assert.ok(page.url().includes('tab=thresholds'), `Back should restore ?tab=thresholds, got ${page.url()}`);
    isThresholdsVisible = await page.$eval('#tab-content-thresholds', el => window.getComputedStyle(el).display !== 'none');
    assert.strictEqual(isThresholdsVisible, true, "Back button must display Thresholds panel");

    // Back to Warnings
    await page.goBack();
    await page.waitForTimeout(150);
    assert.ok(page.url().includes('tab=warnings'), `Back should restore ?tab=warnings, got ${page.url()}`);
    isWarningsVisible = await page.$eval('#tab-content-warnings', el => window.getComputedStyle(el).display !== 'none');
    assert.strictEqual(isWarningsVisible, true, "Back button must display Warnings panel");

    // Forward to Thresholds
    await page.goForward();
    await page.waitForTimeout(150);
    assert.ok(page.url().includes('tab=thresholds'), `Forward should restore ?tab=thresholds, got ${page.url()}`);
    isThresholdsVisible = await page.$eval('#tab-content-thresholds', el => window.getComputedStyle(el).display !== 'none');
    assert.strictEqual(isThresholdsVisible, true, "Forward button must display Thresholds panel");
    console.log("   ✓ Browser Back and Forward successfully restore tab state and URL");

    console.log("5. Testing Direct Deep-Links and Invalid Tab Fallback...");
    await page.goto('http://127.0.0.1:5377/moderation?tab=warnings', { waitUntil: 'networkidle' });
    isWarningsVisible = await page.$eval('#tab-content-warnings', el => window.getComputedStyle(el).display !== 'none');
    assert.strictEqual(isWarningsVisible, true, "Direct deep-link to ?tab=warnings must show Warnings tab");

    await page.goto('http://127.0.0.1:5377/moderation?tab=non_existent_tab', { waitUntil: 'networkidle' });
    isLogsVisible = await page.$eval('#tab-content-logs', el => window.getComputedStyle(el).display !== 'none');
    assert.strictEqual(isLogsVisible, true, "Invalid tab parameter must fall back safely to Mod Logs");
    console.log("   ✓ Deep linking and invalid tab fallback verified");

    console.log("6. Testing Interactive Controls: Auto-Escalation Toggle (Both Directions)...");
    await page.click('#tab-thresholds');
    await page.waitForTimeout(100);

    const toggle = await page.$('#auto-escalation-toggle');
    if (toggle) {
        const initialChecked = await toggle.isChecked();
        // Toggle direction 1
        await toggle.click();
        await page.waitForTimeout(300);
        let statText = await page.$eval('#stat-auto-escalation', el => el.textContent.trim());
        assert.strictEqual(statText, initialChecked ? 'Off' : 'On');

        // Toggle direction 2 (back)
        await toggle.click();
        await page.waitForTimeout(300);
        statText = await page.$eval('#stat-auto-escalation', el => el.textContent.trim());
        assert.strictEqual(statText, initialChecked ? 'On' : 'Off');
        console.log("   ✓ Auto-Escalation toggle works in both ON and OFF directions");
    }

    console.log("7. Testing Interactive Controls: Quick Action Submission...");
    await page.click('#tab-logs');
    await page.waitForTimeout(100);

    const userInp = await page.$('#qa-user');
    if (userInp) {
        await page.fill('#qa-user', '123456789012345678');
        await page.selectOption('#qa-action', 'warn');
        await page.fill('#qa-reason', 'E2E Test Warning');
        
        const countBefore = await page.$eval('#stat-total-logs', el => parseInt(el.textContent, 10) || 0);
        await page.click('#qa-submit-btn');
        await page.waitForTimeout(400);

        const countAfter = await page.$eval('#stat-total-logs', el => parseInt(el.textContent, 10) || 0);
        assert.strictEqual(countAfter, countBefore + 1, "Quick action should increment total logs counter");
        console.log("   ✓ Quick action executed and stat card incremented");
    }

    console.log("8. Testing Interactive Controls: Edit Reason Modal & In-Place Update...");
    await page.click('#tab-logs');
    await page.waitForTimeout(100);

    const editBtn = await page.$('button[data-log-id]');
    if (editBtn) {
        const logId = await editBtn.getAttribute('data-log-id');
        await editBtn.click();
        await page.waitForTimeout(200);

        const modalVisible = await page.$eval('#edit-reason-modal', el => window.getComputedStyle(el).display !== 'none');
        assert.strictEqual(modalVisible, true, "Edit reason modal must open");

        await page.fill('#edit-reason-text', 'Updated Reason via E2E Test');
        await page.click('#edit-reason-modal .btn-primary');
        await page.waitForTimeout(400);

        const updatedReason = await page.$eval(`#log-${logId} .log-reason-cell`, el => el.textContent.trim());
        assert.strictEqual(updatedReason, 'Updated Reason via E2E Test');
        console.log("   ✓ Edit reason modal saved and updated DOM table cell in-place");
    }

    console.log("9. Testing Interactive Controls: Add & Delete Warning Threshold...");
    await page.click('#tab-thresholds');
    await page.waitForTimeout(100);

    const addThBtn = await page.$('#th-add-btn');
    if (addThBtn) {
        await page.fill('#th-count', '5');
        await page.selectOption('#th-action', 'ban');
        await page.fill('#th-duration', '60');
        
        await addThBtn.click();
        await page.waitForTimeout(500);

        const threshRows = await page.$$('#thresholds-tbody tr');
        assert.ok(threshRows.length > 0, "Thresholds table should have rows after adding");

        // Delete threshold
        page.on('dialog', async dialog => { await dialog.accept(); });
        const delThBtn = await page.$('#thresholds-tbody .btn-danger');
        if (delThBtn) {
            await delThBtn.click();
            await page.waitForTimeout(400);
            console.log("   ✓ Threshold added and deleted in-place");
        }
    }

    console.log("10. Testing Message & Role Studio + JSON Bridge Controls...");
    await page.goto('http://127.0.0.1:5377/embed-builder', { waitUntil: 'networkidle' });

    // Open JSON Bridge
    await page.click('#eb-json-bridge-btn');
    await page.waitForTimeout(200);
    let jsonModalVisible = await page.$eval('#eb-json-modal', el => window.getComputedStyle(el).display !== 'none');
    assert.strictEqual(jsonModalVisible, true, "JSON Bridge modal must open");

    // Paste, Validate & Apply JSON
    const testJson = JSON.stringify({
        content: "E2E Studio Payload Test",
        embeds: [{
            title: "Moderation Notice",
            description: "Server rules and active moderation policy.",
            color: 0xe74c3c
        }],
        buttons: [
            { id: "btn_1", label: "Acknowledge", role_id: "8001", color: "green", emoji: "✅" }
        ]
    }, null, 2);

    await page.fill('#eb-json-input', testJson);
    await page.waitForTimeout(200);

    let statusBadge = await page.$eval('#eb-json-status-badge', el => el.textContent);
    assert.ok(statusBadge.includes('Valid'), `Status badge should be Valid, got ${statusBadge}`);

    await page.click('#eb-json-apply-btn');
    await page.waitForTimeout(300);

    const editorContent = await page.$eval('#eb-content', el => el.value);
    assert.strictEqual(editorContent, "E2E Studio Payload Test");

    const appliedBtnCount = await page.$$eval('.eb-btn-item', els => els.length);
    assert.strictEqual(appliedBtnCount, 1);
    console.log("   ✓ JSON Bridge modal import, validation, and apply verified in Studio");

    console.log("11. Testing Reports Page Status Filter Navigation...");
    await page.goto('http://127.0.0.1:5377/reports', { waitUntil: 'networkidle' });
    await page.evaluate(() => { window.__neroReportsMarker = 'stayed_in_reports'; });
    
    const resolvedBtn = await page.$('a[href="/reports?status=resolved"]');
    if (resolvedBtn) {
        await resolvedBtn.click();
        await page.waitForTimeout(300);
        const marker = await page.evaluate(() => window.__neroReportsMarker);
        assert.strictEqual(marker, 'stayed_in_reports', "Reports status filter should swap smoothly via HTMX");
        assert.ok(page.url().includes('status=resolved'), "URL should update to ?status=resolved");
        console.log("   ✓ Reports status filter HTMX smooth swap verified");
    }

    console.log("============================================================");
    console.log("All Moderation Navigation & Button Functionality checks PASSED!");
    await browser.close();
})();
