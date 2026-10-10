/* Minimal saved-Design Publication controls; the API owns all publication data. */
(function () {
    'use strict';
    function init(root) {
        var select = root.querySelector('#sd-design-select');
        var channel = root.querySelector('#sd-publication-channel');
        var button = root.querySelector('#sd-publication-publish');
        var list = root.querySelector('#sd-publication-list');
        var status = root.querySelector('#sd-publication-status');
        if (!select || !button || button.dataset.publicationBound) return;
        button.dataset.publicationBound = '1';
        var csrf = window.__CSRF_TOKEN__ || '';
        var listGeneration = 0;
        var publishInFlight = false;
        // Keep uncertainty attached to the exact Design/channel request, not
        // as a page-wide flag that could warn for another Design.
        var uncertainPublishContexts = Object.create(null);
        var pendingChannels = Object.create(null);
        var publicationListDesign = null;
        var publicationListState = 'idle';
        button.disabled = true;
        function syncPublishButton() {
            button.disabled = publishInFlight || !select.value || publicationListState !== 'loaded' ||
                publicationListDesign !== select.value;
        }
        function publishContextKey(designId, channelId) {
            return String(designId) + ':' + String(channelId);
        }
        function isCurrentContext(designId, generation) {
            return select.value === designId && publicationListDesign === designId &&
                generation === listGeneration;
        }
        function request(url, method, body) {
            return fetch(url, {method: method, credentials: 'same-origin', headers: {
                'Content-Type': 'application/json', 'X-CSRF-Token': csrf
            }, body: body ? JSON.stringify(body) : undefined}).then(function (r) {
                return r.json().then(function (data) {
                    if (!r.ok && r.status !== 202) {
                        var error = new Error(data.error || 'Request failed'); error.status = r.status; throw error;
                    }
                    return data;
                });
            });
        }
        function render() {
            var designId = select.value;
            var generation = ++listGeneration;
            list.textContent = '';
            pendingChannels = Object.create(null);
            publicationListDesign = designId || null;
            publicationListState = designId ? 'loading' : 'idle';
            syncPublishButton();
            if (!designId) return;
            request('/api/shop-publisher/designs/' + encodeURIComponent(designId) + '/publications', 'GET').then(function (data) {
                if (generation !== listGeneration || select.value !== designId) return;
                if (!data || data.success !== true || !Array.isArray(data.publications)) {
                    throw new Error((data && data.error) || 'Publication list could not be verified.');
                }
                publicationListState = 'loaded';
                syncPublishButton();
                list.textContent = '';
                (data.publications || []).forEach(function (p) {
                    if (p.status === 'pending') pendingChannels[String(p.channel_id)] = true;
                    var li = document.createElement('li');
                    li.textContent = 'Channel ' + p.channel_id + ' · ' + p.status + (p.message_id ? ' · message ' + p.message_id : '');
                    var hasMessageId = p.message_id !== null && p.message_id !== undefined && String(p.message_id).trim() !== '';
                    // A definitively failed send never created a Discord message, so only
                    // this exact state may be cleared; `pending` (uncertain) stays locked.
                    var failedOrphan = p.status === 'failed' && !hasMessageId;
                    function showLocked(message) {
                        var locked = document.createElement('span');
                        locked.textContent = ' Not actionable: ' + message;
                        li.appendChild(locked);
                    }
                    if (p.status === 'replacement_sending') {
                        showLocked('replacement send is in progress; this state remains locked until its outcome is known.');
                    } else if (p.status === 'replacement_send_uncertain') {
                        if (!hasMessageId) {
                            showLocked('there is no confirmed message ID for explicit replacement authorization.');
                        } else {
                            var recover = document.createElement('button'); recover.type = 'button';
                            recover.textContent = 'Authorize replacement retry';
                            recover.addEventListener('click', function () {
                                if (select.value !== designId || generation !== listGeneration) return;
                                if (!window.confirm('The previous replacement may already exist. Authorize one deliberate replacement attempt?')) return;
                                recover.disabled = true;
                                request('/api/shop-publisher/publications/' + p.id + '/authorize-replacement', 'POST', {}).then(function (result) {
                                    if (!isCurrentContext(designId, generation)) return;
                                    status.textContent = result.message || 'Replacement retry authorized; choose Update deliberately.'; render();
                                }).catch(function (e) {
                                    if (!isCurrentContext(designId, generation)) return;
                                    status.textContent = e.message;
                                    recover.disabled = false;
                                });
                            }); li.appendChild(recover);
                        }
                    } else if (p.status === 'pending' || p.status === 'updating' || p.status === 'unpublishing') {
                        showLocked(p.status === 'pending'
                            ? 'publish is pending or uncertain; no retry, Update, or Unpublish is available.'
                            : 'a lifecycle operation is in progress or locked; Update and Unpublish are unavailable.');
                    } else if (!hasMessageId && !failedOrphan) {
                        showLocked('no confirmed message ID is recorded; Update and Unpublish are unavailable.');
                    } else if (p.status !== 'published' && p.status !== 'attention' &&
                            p.status !== 'replacement_retry_authorized' && !failedOrphan) {
                        showLocked('this lifecycle state does not permit Update or Unpublish.');
                    } else {
                        var rowActionInFlight = false;
                        (p.status === 'replacement_retry_authorized' ? ['Update'] : failedOrphan ? ['Unpublish'] : ['Update', 'Unpublish']).forEach(function (label) {
                            var action = document.createElement('button'); action.type = 'button'; action.textContent = label;
                            action.addEventListener('click', function () {
                                if (!isCurrentContext(designId, generation) || rowActionInFlight) return;
                                if (label === 'Unpublish' && !window.confirm(failedOrphan
                                    ? 'The send failed and no Discord message exists. Remove this failed Publication record?'
                                    : 'Delete this Discord message and remove its Publication?')) return;
                                rowActionInFlight = true;
                                var rowButtons = li.querySelectorAll('button');
                                for (var buttonIndex = 0; buttonIndex < rowButtons.length; buttonIndex += 1) {
                                    rowButtons[buttonIndex].disabled = true;
                                }
                                var url = '/api/shop-publisher/publications/' + p.id + (label === 'Update' ? '/update' : '');
                                function releaseRowActions() {
                                    rowActionInFlight = false;
                                    for (var i = 0; i < rowButtons.length; i += 1) rowButtons[i].disabled = false;
                                }
                                function run(token) {
                                    if (!isCurrentContext(designId, generation)) return Promise.resolve();
                                    return request(url, label === 'Update' ? 'POST' : 'DELETE', token ? {warning_token: token} : {}).then(function (result) {
                                        if (!isCurrentContext(designId, generation)) return;
                                        if (result.requires_warning_ack) {
                                            var warnings = (result.warnings || []).map(function (w) { return w.message || w.code; }).join('\n');
                                            if (window.confirm('Review these current warnings and continue?\n\n' + warnings)) return run(result.warning_token);
                                            status.textContent = 'Update cancelled; no change was sent.';
                                            releaseRowActions();
                                            return;
                                        }
                                        status.textContent = result.error || label + ' completed.'; render();
                                    });
                                }
                                run(null).catch(function (e) {
                                    if (!isCurrentContext(designId, generation)) return;
                                    status.textContent = e.message;
                                    render();
                                });
                            }); li.appendChild(action);
                        });
                    }
                    list.appendChild(li);
                });
            }).catch(function (e) {
                if (generation === listGeneration && select.value === designId) {
                    publicationListState = 'failed';
                    syncPublishButton();
                    status.textContent = e.message;
                }
            });
        }
        select.addEventListener('change', function () {
            // Status belongs to the Design that initiated it. Clear it before
            // loading another Design; in-flight callbacks use their captured
            // list generation and cannot write into this new context.
            status.textContent = '';
            render();
        });
        button.addEventListener('click', function () {
            if (publishInFlight) return;
            // Snowflake safety: Discord channel IDs exceed JS's safe-integer
            // range, so the exact digit string is validated and sent as-is.
            var designId = select.value, channelRaw = (channel.value || '').trim();
            if (publicationListState !== 'loaded' || publicationListDesign !== designId) {
                syncPublishButton();
                status.textContent = 'Wait for this Design’s Publication list to load successfully before publishing.';
                return;
            }
            if (!designId || !/^\d{17,20}$/.test(channelRaw)) { status.textContent = 'Select a saved Design and enter a valid channel ID.'; return; }
            var requestGeneration = listGeneration;
            var contextKey = publishContextKey(designId, channelRaw);
            // The list API serializes channel_id as a JSON number, which JS
            // rounds past 2^53, so match the exact string and that rounded
            // numeric form: the pending-duplication guard must still fire.
            var pendingKey = pendingChannels[channelRaw] ? channelRaw : String(Number(channelRaw));
            if ((uncertainPublishContexts[contextKey] || pendingChannels[pendingKey]) &&
                    !window.confirm('A Publication for this Design and channel is pending or has an uncertain outcome. Sending again can create another message. Deliberately create a separate Publication anyway?')) return;
            publishInFlight = true;
            syncPublishButton();
            status.textContent = 'Preparing publication…';
            function publish(token) {
                if (!isCurrentContext(designId, requestGeneration)) return Promise.resolve();
                return request('/api/shop-publisher/publications/publish', 'POST', {
                    design_id: Number(designId), channel_id: channelRaw, warning_token: token
                }).then(function (result) {
                    if (result.uncertain) uncertainPublishContexts[contextKey] = true;
                    if (result.publication && result.publication.status === 'published') {
                        delete uncertainPublishContexts[contextKey];
                    }
                    if (!isCurrentContext(designId, requestGeneration)) return;
                    if (result.requires_warning_ack) {
                        var warnings = (result.warnings || []).map(function (w) { return w.message || w.code; }).join('\n');
                        if (window.confirm('Review these current warnings and publish anyway?\n\n' + warnings)) return publish(result.warning_token);
                        status.textContent = 'Publish cancelled; no message was sent.'; return;
                    }
                    if (result.uncertain) {
                        status.textContent = 'UNCERTAIN — Publication remains pending. Do not retry unless you deliberately accept possible duplication.';
                    } else if (result.publication && result.publication.status === 'published') {
                        status.textContent = 'Publication confirmed.';
                    } else {
                        status.textContent = result.error || 'Publish did not confirm success.';
                    }
                    render();
                });
            }
            publish(null).catch(function (e) {
                // A browser-level failure may occur after the server sent; keep
                // that uncertainty with the initiating Design/channel only.
                if (!e.status) uncertainPublishContexts[contextKey] = true;
                if (!isCurrentContext(designId, requestGeneration)) return;
                if (!e.status) {
                    status.textContent = 'UNCERTAIN — the request outcome is unknown. Do not retry unless you deliberately accept possible duplication.';
                } else status.textContent = e.message;
            }).then(function () {
                publishInFlight = false;
                syncPublishButton();
            });
        });
        render();
    }
    function boot() { document.querySelectorAll('[data-page-module="shop-designer"]').forEach(init); }
    document.addEventListener('DOMContentLoaded', boot);
    document.addEventListener('htmx:afterSwap', boot);
    boot();
}());
