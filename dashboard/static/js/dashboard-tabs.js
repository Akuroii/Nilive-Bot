/* Dashboard-wide tab accessibility, URL synchronization, and refresh/history state (WS4).
 * Existing page click handlers remain the source of truth for panel behavior;
 * this layer adds URL ?tab= sync, browser Back/Forward (popstate) support,
 * ARIA semantics, keyboard navigation, and per-route tab memory without
 * replacing any page workflow or selector. */
(function () {
    'use strict';

    const TAB_SELECTOR = '.tab-btn, .ctab-btn, .ttab-btn';
    const STORAGE_PREFIX = 'nero-dashboard-tab-v1:';

    function getTabName(tab) {
        if (!tab) return null;
        if (tab.dataset && tab.dataset.tab) return tab.dataset.tab;
        if (tab.dataset && tab.dataset.category) {
            return tab.dataset.category.toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
        }
        if (tab.id) {
            if (tab.id.startsWith('ctab-')) return tab.id.slice(5);
            if (tab.id.startsWith('ttab-')) return tab.id.slice(5);
            if (tab.id.startsWith('tab-')) return tab.id.slice(4);
            return tab.id;
        }
        const text = tab.textContent.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
        return text || null;
    }

    function panelFor(tab) {
        if (!tab) return null;
        if (tab.dataset && tab.dataset.controls) {
            const el = document.getElementById(tab.dataset.controls);
            if (el) return el;
        }
        if (!tab.id) return null;
        let prefix;
        if (tab.id.startsWith('ctab-')) prefix = 'ctab-content-';
        else if (tab.id.startsWith('ttab-')) prefix = 'ttab-content-';
        else if (tab.id.startsWith('tab-')) prefix = 'tab-content-';
        else return null;
        return document.getElementById(prefix + tab.id.slice(tab.id.indexOf('-') + 1));
    }

    function tabsFor(group) {
        return Array.from(group.querySelectorAll(TAB_SELECTOR)).filter(function (tab) {
            return tab.parentElement === group;
        });
    }

    function panelIsVisible(panel) {
        // aria-hidden is maintained by this script, so it cannot be used to
        // infer visibility after a page handler switches to a hidden panel.
        if (!panel || panel.hidden) return false;
        return window.getComputedStyle(panel).display !== 'none';
    }

    function findTabByName(tabs, name) {
        if (!name) return null;
        const clean = String(name).toLowerCase().trim().replace(/^#tab-|^#/, '');
        return tabs.find(function (t) {
            const tName = getTabName(t);
            if (tName && tName.toLowerCase() === clean) return true;
            if (t.id && t.id.toLowerCase() === clean) return true;
            if (t.id && (t.id === 'tab-' + clean || t.id === 'ttab-' + clean || t.id === 'ctab-' + clean)) return true;
            return false;
        }) || null;
    }

    function syncUrlWithTab(tab, push) {
        const tabName = getTabName(tab);
        if (!tabName) return;

        try {
            const url = new URL(window.location.href);
            const currentParam = url.searchParams.get('tab');
            if (currentParam === tabName) return;

            url.searchParams.set('tab', tabName);
            const state = { tab: tabName, path: url.pathname };

            if (push) {
                window.history.pushState(state, '', url.pathname + url.search + url.hash);
            } else {
                window.history.replaceState(state, '', url.pathname + url.search + url.hash);
            }
        } catch (e) {
            /* Fallback for sandboxed frames */
        }
    }

    function decorateGroup(group, activeTab) {
        const tabs = tabsFor(group);
        if (!tabs.length) return;

        group.setAttribute('role', 'tablist');
        group.setAttribute('aria-orientation', 'horizontal');
        if (!group.hasAttribute('aria-label')) {
            const title = document.querySelector('[data-page-title]')?.dataset.pageTitle
                || document.getElementById('page-title')?.textContent.trim();
            group.setAttribute('aria-label', title ? title + ' sections' : 'Page sections');
        }

        tabs.forEach(function (tab) {
            const selected = tab === activeTab;
            tab.setAttribute('role', 'tab');
            tab.setAttribute('aria-selected', String(selected));
            tab.tabIndex = selected ? 0 : -1;

            const panel = panelFor(tab);
            if (panel) {
                if (!panel.id) panel.id = 'dashboard-tab-panel-' + (tab.id || Math.random().toString(36).slice(2));
                tab.setAttribute('aria-controls', panel.id);
                panel.setAttribute('role', 'tabpanel');
                panel.setAttribute('aria-labelledby', tab.id || '');
                panel.setAttribute('aria-hidden', String(!panelIsVisible(panel)));
            }
        });

        if (!group.dataset.dashboardTabsReady) {
            group.addEventListener('keydown', function (event) {
                const current = event.target.closest(TAB_SELECTOR);
                if (!current || current.parentElement !== group) return;
                const currentTabs = tabsFor(group);
                const index = currentTabs.indexOf(current);
                if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;

                event.preventDefault();
                let nextIndex = index;
                if (event.key === 'ArrowRight') nextIndex = (index + 1) % currentTabs.length;
                else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + currentTabs.length) % currentTabs.length;
                else if (event.key === 'Home') nextIndex = 0;
                else if (event.key === 'End') nextIndex = currentTabs.length - 1;
                const next = currentTabs[nextIndex];
                next.focus();
                next.click();
            });
            group.dataset.dashboardTabsReady = 'true';
        }
    }

    function routeStorageKey() {
        return STORAGE_PREFIX + window.location.pathname;
    }

    function restoreTabs(root, forcedTabName, syncUrl) {
        const scope = root && root.querySelectorAll ? root : document;
        const groups = new Set(Array.from(scope.querySelectorAll(TAB_SELECTOR)).map(function (tab) {
            return tab.parentElement;
        }).filter(Boolean));

        let urlTabName = forcedTabName;
        if (!urlTabName) {
            try {
                const url = new URL(window.location.href);
                urlTabName = url.searchParams.get('tab') || window.location.hash.replace(/^#tab-|^#/, '') || null;
            } catch (e) {
                urlTabName = null;
            }
        }

        let savedId = null;
        try { savedId = sessionStorage.getItem(routeStorageKey()); } catch (e) { /* storage is optional */ }

        groups.forEach(function (group) {
            const tabs = tabsFor(group);
            if (!tabs.length) return;

            let targetTab = null;

            if (urlTabName) {
                targetTab = findTabByName(tabs, urlTabName);
            }
            if (!targetTab && savedId && !urlTabName) {
                targetTab = tabs.find(function (tab) { return tab.id === savedId; });
            }
            if (!targetTab) {
                targetTab = tabs.find(function (tab) { return panelIsVisible(panelFor(tab)); });
            }
            if (!targetTab) {
                targetTab = tabs[0];
            }

            const currentlyVisible = tabs.find(function (tab) { return panelIsVisible(panelFor(tab)); });

            if (targetTab && targetTab !== currentlyVisible) {
                // The page's existing click handler changes the panel
                targetTab.click();
            } else if (targetTab) {
                decorateGroup(group, targetTab);
            }

            if (targetTab && syncUrl) {
                syncUrlWithTab(targetTab, false);
            }
        });
    }

    document.addEventListener('click', function (event) {
        const tab = event.target.closest && event.target.closest(TAB_SELECTOR);
        if (!tab || !tab.parentElement) return;

        decorateGroup(tab.parentElement, tab);

        if (tab.id) {
            try { sessionStorage.setItem(routeStorageKey(), tab.id); } catch (e) { /* optional */ }
        }

        // Push history state with URL ?tab=<name>
        syncUrlWithTab(tab, true);
    });

    function restoreAfterNavigation(event) {
        const reportedTarget = event && event.detail && event.detail.target;
        const root = reportedTarget && reportedTarget.id
            ? (document.getElementById(reportedTarget.id) || reportedTarget)
            : reportedTarget;
        window.setTimeout(function () {
            restoreTabs(root && root.isConnected ? root : document, null, false);
        }, 0);
    }

    // Initial parse is complete because this file is loaded at the end of body.
    restoreTabs(document, null, false);

    document.addEventListener('htmx:afterSwap', restoreAfterNavigation);
    document.addEventListener('htmx:historyRestore', restoreAfterNavigation);

    window.addEventListener('popstate', function () {
        window.setTimeout(function () {
            try {
                const url = new URL(window.location.href);
                const tabParam = url.searchParams.get('tab') || window.location.hash.replace(/^#tab-|^#/, '');
                restoreTabs(document, tabParam, false);
            } catch (e) {
                restoreTabs(document, null, false);
            }
        }, 0);
    });
})();
