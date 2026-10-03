"""Focused Publication contract tests; Discord and SQLite are isolated."""
import asyncio
import json
import sqlite3

import pytest

from utils import shop_publication as pub


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / 'shop.db'
    conn = sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE shop_designs(id INTEGER PRIMARY KEY, guild_id INTEGER, name TEXT, design_json TEXT);
      CREATE TABLE shop_items(id INTEGER PRIMARY KEY, guild_id INTEGER, name TEXT, type TEXT,
        price REAL, price_diamonds INTEGER, enabled INTEGER, current_stock INTEGER, max_stock INTEGER,
        prestige_tier INTEGER, featured INTEGER, description TEXT, duration_hours INTEGER,
        required_level INTEGER, rarity TEXT, icon_url TEXT, option_of_id INTEGER,
        role_id INTEGER, required_role_id INTEGER, xp_boost_multiplier REAL);
      CREATE TABLE shop_publications(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER,
        design_id INTEGER, channel_id INTEGER, message_id INTEGER, status TEXT,
        last_error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP, last_published_at TEXT);
      INSERT INTO shop_designs VALUES (1, 10, 'Saved', '{"products":[],"action":{"kind":"buttons","entries":[]}}');
    ''')
    conn.commit(); conn.close()
    monkeypatch.setattr(pub, 'DB_PATH', str(path))
    monkeypatch.setattr(pub, 'run_async', lambda coro: asyncio.run(coro))
    return path


def _replace_design(path, data):
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE shop_designs SET design_json=? WHERE guild_id=10 AND id=1',
                     (json.dumps(data),))


def _valid_design():
    return {'presentation': {'mode': 'per_product', 'content': 'hello', 'embeds': []},
            'products': [1], 'action': {'kind': 'buttons', 'entries': [{'product_id': 1}]}}


def test_guild_scope_and_saved_design_source(db):
    assert pub.design_exists(10, 1)
    assert not pub.design_exists(11, 1)
    # Load verifies saved source and safely returns empty rows for an empty roster.
    _replace_design(db, {'presentation': {'mode': 'frame', 'content': '', 'embeds': []},
                         'products': [], 'action': {'kind': 'buttons', 'entries': []}})
    assert pub._load_design_and_rows(10, 1)[0]['id'] == 1
    with pytest.raises(pub.PublicationError):
        pub._load_design_and_rows(11, 1)


def test_payload_reuses_action_shape_without_publication_identity():
    action = {'kind': 'buttons', 'entries': [
        {'label': 'Buy', 'custom_id': 'shop_buy_42', 'emoji': '<:coin:123456>'}
    ]}
    components = pub.action_components(action)
    assert components[0]['components'][0]['custom_id'] == 'shop_buy_42'
    assert 'publication_id' not in str(components)
    assert pub._emoji_component('<malformed>') is None


def test_channel_types_and_effective_permission_validation():
    class Fake(pub.DiscordREST):
        def __init__(self, channel, bits): self.channel, self.bits = channel, bits
        def request(self, method, path, **kwargs):
            if path.endswith('/channels'): return [self.channel]
            return {'ok': True}
        def _effective_bot_permissions(self, guild_id, channel): return self.bits
    channel = {'id': '20', 'type': 0, 'permission_overwrites': []}
    needed = pub.VIEW_CHANNEL | pub.SEND_MESSAGES | pub.EMBED_LINKS
    assert Fake(channel, needed).validate_channel_and_permissions(10, 20, 'publish')
    with pytest.raises(pub.PublicationError, match='Only guild'):
        Fake(dict(channel, type=2), needed).validate_channel_and_permissions(10, 20, 'publish')
    with pytest.raises(pub.PublicationError, match='lacks'):
        Fake(channel, pub.VIEW_CHANNEL).validate_channel_and_permissions(10, 20, 'publish')
    with pytest.raises(pub.PublicationError, match='not available'):
        Fake(channel, needed).validate_channel_and_permissions(10, 21, 'publish')


def test_effective_permissions_fail_closed_on_missing_or_malformed_overwrites():
    class PermissionREST(pub.DiscordREST):
        def __init__(self): pass
        def request(self, method, path, **kwargs):
            if path == '/users/@me': return {'id': '99'}
            if path.endswith('/roles'): return [{'id': '10', 'permissions': '0'}]
            return {'roles': []}
    rest = PermissionREST()
    for channel in ({}, {'permission_overwrites': None}, {'permission_overwrites': [{}]}):
        with pytest.raises(pub.PublicationError, match='permission'):
            rest._effective_bot_permissions(10, channel)


def test_effective_permissions_apply_every_overwrite_layer():
    class PermissionREST(pub.DiscordREST):
        def __init__(self): pass
        def request(self, method, path, **kwargs):
            if path == '/users/@me': return {'id': '99'}
            if path.endswith('/roles'):
                return [{'id': '10', 'permissions': str(pub.VIEW_CHANNEL | pub.SEND_MESSAGES | pub.EMBED_LINKS)},
                        {'id': '20', 'permissions': str(pub.READ_MESSAGE_HISTORY)}]
            return {'roles': ['20']}
    rest = PermissionREST()
    channel = {'permission_overwrites': [
        {'id': '10', 'type': 0, 'deny': str(pub.SEND_MESSAGES), 'allow': '0'},
        {'id': '20', 'type': 0, 'deny': str(pub.VIEW_CHANNEL), 'allow': str(pub.SEND_MESSAGES)},
        {'id': '99', 'type': 1, 'deny': str(pub.EMBED_LINKS), 'allow': str(pub.VIEW_CHANNEL)},
    ]}
    bits = rest._effective_bot_permissions(10, channel)
    assert bits & pub.SEND_MESSAGES
    assert bits & pub.VIEW_CHANNEL
    assert not bits & pub.EMBED_LINKS


def test_publish_pending_then_confirmed_and_uncertain(db, monkeypatch):
    prepared = {'blocking_errors': [], 'warnings': [], 'warning_token': pub._warning_token([]),
                'payload': {'content': 'live'}, 'action': {}}
    monkeypatch.setattr(pub, 'prepare_design', lambda *args: prepared)
    class Fake:
        def validate_channel_and_permissions(self, *args): pass
        def create_message(self, channel, payload): return 987
    result = pub.post_publish(10, 1, 20, rest=Fake())
    assert result['publication']['status'] == 'published'
    assert result['publication']['message_id'] == 987
    class Uncertain(Fake):
        def create_message(self, *args): raise pub.DiscordFailure('timeout', uncertain=True)
    result = pub.post_publish(10, 1, 21, rest=Uncertain())
    assert result['outcome'] == 'uncertain'
    assert result['publication']['status'] == 'pending'
    assert result['publication']['message_id'] is None


def test_ack_and_blocking_validation(db, monkeypatch):
    p = {'blocking_errors': [], 'warnings': [{'code': 'notice', 'message': 'warning'}],
         'warning_token': 'fresh', 'payload': {'content': 'x'}}
    monkeypatch.setattr(pub, 'prepare_design', lambda *args: p)
    class Fake:
        def validate_channel_and_permissions(self, *args): pass
    assert pub.post_publish(10, 1, 20, rest=Fake())['outcome'] == 'ack_required'
    p['blocking_errors'] = [{'message': 'bad'}]
    assert pub.post_publish(10, 1, 20, rest=Fake())['outcome'] == 'blocked'


def test_invalid_presentation_blocks_publish_and_update_before_discord(db):
    class NeverDiscord:
        def validate_channel_and_permissions(self, *args): raise AssertionError('must not call Discord')
    publish = pub.post_publish(10, 1, 20, rest=NeverDiscord())
    assert publish['outcome'] == 'blocked'
    assert publish['prepared']['blocking_errors'][0]['code'] == 'invalid_presentation'
    record = pub.create_pending(10, 1, 20)
    update = pub.update_publication(10, record, rest=NeverDiscord())
    assert update['outcome'] == 'blocked'
    assert update['prepared']['blocking_errors'][0]['code'] == 'invalid_presentation'


def test_presentation_contract_modes_and_structure(db, monkeypatch):
    monkeypatch.setattr(pub, '_load_design_and_rows', lambda *a: (
        _valid_design(), {1: {'id': 1}}, 'Saved'))
    monkeypatch.setattr(pub.SP, 'validate_design', lambda *a: [])
    async def currency(_guild): return {}
    monkeypatch.setattr(pub, 'get_currency_config', currency)
    monkeypatch.setattr(pub.SP, 'preview_design', lambda *a: {
        'content': 'resolved', 'embeds': [], 'action': {'kind': 'buttons', 'entries': []}, 'warnings': []})
    prepare_real = pub.prepare_design
    valid = prepare_real(10, 1)
    assert valid['blocking_errors'] == [] and valid['payload']['content'] == 'resolved'
    monkeypatch.setattr(pub, 'prepare_design', lambda *a: valid)
    class Successful:
        def validate_channel_and_permissions(self, *a): pass
        def create_message(self, *a): return 77
    sent = pub.post_publish(10, 1, 20, rest=Successful())
    assert sent['outcome'] == 'published' and sent['publication']['message_id'] == 77
    monkeypatch.setattr(pub, 'prepare_design', prepare_real)
    for presentation in (None, {'mode': 'legacy', 'content': '', 'embeds': []},
                         {'mode': 'frame', 'content': [], 'embeds': []},
                         {'mode': 'frame', 'content': '', 'embeds': {}}):
        monkeypatch.setattr(pub, '_load_design_and_rows', lambda *a, p=presentation: (
            dict(_valid_design(), presentation=p), {1: {'id': 1}}, 'Saved'))
        assert pub.prepare_design(10, 1)['blocking_errors']


def _patch_valid_prepare(monkeypatch):
    monkeypatch.setattr(pub, 'prepare_design', lambda *args: {
        'blocking_errors': [], 'warnings': [], 'warning_token': pub._warning_token([]),
        'payload': {'content': 'fresh'}})


def _confirmed_record(db):
    row = pub.create_pending(10, 1, 20)
    return pub.set_publication(10, row['id'], status='published', message_id=30, update_message=True)


def test_authorize_replacement_only_transitions_uncertain_state(db):
    row = _confirmed_record(db)
    assert pub.claim_update(10, row['id'], row['message_id'])
    assert pub.claim_replacement(10, row['id'], row['message_id'])
    assert pub.authorize_replacement_retry(10, row['id']) is None
    assert pub.get_publication(10, row['id'])['status'] == 'replacement_sending'
    uncertain, row = pub._conditional_transition(
        10, row['id'], ('replacement_sending',), 'replacement_send_uncertain',
        expected_message_id=row['message_id'], match_message=True, last_error='timeout')
    assert uncertain
    authorized = pub.authorize_replacement_retry(10, row['id'])
    assert authorized['status'] == 'replacement_retry_authorized'
    assert pub.authorize_replacement_retry(10, row['id']) is None


def test_two_competing_replacement_claims_have_one_winner(db):
    row = _confirmed_record(db)
    assert pub.claim_update(10, row['id'], row['message_id'])
    claims = [pub.claim_replacement(10, row['id'], row['message_id']) for _ in range(2)]
    assert claims == [True, False]
    # This exercises SQLite's actual conditional UPDATE sequentially; the
    # environment has no async test harness for a true simultaneous race.


def test_stale_replacement_completion_cannot_overwrite_new_state(db):
    row = _confirmed_record(db)
    assert pub.claim_update(10, row['id'], row['message_id'])
    assert pub.claim_replacement(10, row['id'], row['message_id'])
    changed, _ = pub._conditional_transition(
        10, row['id'], ('replacement_sending',), 'attention',
        expected_message_id=row['message_id'], match_message=True, last_error='newer state')
    assert changed
    changed, current = pub._conditional_transition(
        10, row['id'], ('replacement_sending',), 'published',
        expected_message_id=row['message_id'], match_message=True,
        message_id=999, update_message=True, successful=True)
    assert not changed
    assert current['status'] == 'attention' and current['message_id'] == row['message_id']


def test_stale_unpublish_cannot_remove_after_replacement_claim(db):
    stale = _confirmed_record(db)
    assert pub.claim_update(10, stale['id'], stale['message_id'])
    assert pub.claim_replacement(10, stale['id'], stale['message_id'])
    class NeverDiscord:
        def validate_channel_and_permissions(self, *a): raise AssertionError('must not access Discord')
    result = pub.unpublish(10, stale, rest=NeverDiscord())
    assert result['outcome'] == 'replacement_uncertain'
    assert pub.get_publication(10, stale['id'])['status'] == 'replacement_sending'


def test_ambiguous_replacement_cannot_be_retried_by_normal_update(db, monkeypatch):
    _patch_valid_prepare(monkeypatch)
    row = _confirmed_record(db)
    class AmbiguousReplacement:
        sends = 0
        fail = True
        def validate_channel_and_permissions(self, *a): pass
        def fetch_message(self, *a): raise pub.DiscordFailure('missing', status=404, discord_code=10008)
        def create_message(self, *a):
            self.sends += 1
            if self.fail: raise pub.DiscordFailure('lost response', uncertain=True)
            return 55
    fake = AmbiguousReplacement()
    first = pub.update_publication(10, row, rest=fake)
    assert first['outcome'] == 'replacement_uncertain'
    assert first['publication']['status'] == 'replacement_send_uncertain'
    assert first['publication']['message_id'] == 30
    second = pub.update_publication(10, first['publication'], rest=fake)
    assert second['outcome'] == 'replacement_uncertain'
    assert fake.sends == 1
    recovered = pub.authorize_replacement_retry(10, row['id'])
    assert recovered['status'] == 'replacement_retry_authorized'
    fake.fail = False
    third = pub.update_publication(10, recovered, rest=fake)
    assert third['outcome'] == 'replaced' and fake.sends == 2


def test_confirmed_replacement_updates_message_id_once(db, monkeypatch):
    _patch_valid_prepare(monkeypatch)
    row = _confirmed_record(db)
    class Confirmed:
        sends = 0
        def validate_channel_and_permissions(self, *a): pass
        def fetch_message(self, *a): raise pub.DiscordFailure('missing', status=404, discord_code=10008)
        def create_message(self, *a): self.sends += 1; return 31
    fake = Confirmed()
    result = pub.update_publication(10, row, rest=fake)
    assert result['outcome'] == 'replaced'
    assert result['publication']['message_id'] == 31
    assert result['publication']['id'] == row['id']
    assert fake.sends == 1


def test_multiple_publications_are_allowed_and_isolated_by_guild(db):
    first = pub.create_pending(10, 1, 20)
    second = pub.create_pending(10, 1, 20)
    assert first['id'] != second['id']
    assert len(pub.list_publications(10, 1)) == 2
    assert pub.list_publications(11, 1) == []


def test_definitive_publish_rejection_is_failed(db, monkeypatch):
    monkeypatch.setattr(pub, 'prepare_design', lambda *a: {
        'blocking_errors': [], 'warnings': [], 'warning_token': pub._warning_token([]),
        'payload': {'content': 'x'}})
    class Rejected:
        def validate_channel_and_permissions(self, *a): pass
        def create_message(self, *a): raise pub.DiscordFailure('rejected', status=400)
    result = pub.post_publish(10, 1, 20, rest=Rejected())
    assert result['outcome'] == 'failed' and result['publication']['status'] == 'failed'


def test_unpublish_success_and_already_missing(db):
    row = _confirmed_record(db)
    class Deleted:
        def validate_channel_and_permissions(self, *a): pass
        def fetch_message(self, *a): return {}
        def delete_message(self, *a): return None
    assert pub.unpublish(10, row, rest=Deleted())['outcome'] == 'removed'
    row = _confirmed_record(db)
    class Missing(Deleted):
        def fetch_message(self, *a): raise pub.DiscordFailure('missing', status=404, discord_code=10008)
    result = pub.unpublish(10, row, rest=Missing())
    assert result['already_missing'] is True


def test_publish_rechecks_warning_fingerprint_before_send(monkeypatch):
    warning = {'code': 'notice', 'message': 'same'}
    first = {'blocking_errors': [], 'warnings': [warning], 'warning_token': 'old', 'payload': {'content': 'one'}}
    fresh = {'blocking_errors': [], 'warnings': [warning], 'warning_token': 'new', 'payload': {'content': 'two'}}
    prepared = iter([first, fresh])
    monkeypatch.setattr(pub, 'prepare_design', lambda *a: next(prepared))
    class Rest:
        sends = 0
        def validate_channel_and_permissions(self, *a): pass
        def create_message(self, *a): self.sends += 1; return 1
    rest = Rest()
    result = pub.post_publish(10, 1, 20, warning_token='old', rest=rest)
    assert result['outcome'] == 'ack_required'
    assert rest.sends == 0


def test_warning_ack_binds_design_live_rows_and_resolved_payload(monkeypatch):
    source = {'presentation': {'mode': 'frame', 'content': 'same', 'embeds': []}, 'products': [1],
              'action': {'kind': 'buttons', 'entries': [{'product_id': 1}]}, 'id': 1}
    live = {'1': {'id': 1, 'price': 10}}
    monkeypatch.setattr(pub, '_load_design_and_rows', lambda *a: (dict(source), dict(live), 'Saved'))
    monkeypatch.setattr(pub.SP, 'validate_design', lambda *a: [])
    async def currency(_guild): return {}
    monkeypatch.setattr(pub, 'get_currency_config', currency)
    monkeypatch.setattr(pub.SP, 'preview_design', lambda d, rows, _c: {
        'content': str(rows['1']['price']), 'embeds': [],
        'action': {'kind': 'buttons', 'entries': []},
        'warnings': [{'code': 'notice', 'message': 'same warning'}]})
    token1 = pub.prepare_design(10, 1)['warning_token']
    source['presentation']['content'] = 'changed Design'
    changed_design = pub.prepare_design(10, 1)
    token_design = changed_design['warning_token']
    assert token_design != token1
    assert not pub.require_warning_ack(changed_design, token1)
    source['presentation']['content'] = 'same'
    live['1']['price'] = 11
    changed_live = pub.prepare_design(10, 1)
    token_live = changed_live['warning_token']
    assert token_live != token1
    assert not pub.require_warning_ack(changed_live, token1)
    live['1']['price'] = 10
    monkeypatch.setattr(pub.SP, 'preview_design', lambda d, rows, _c: {
        'content': 'different resolved output', 'embeds': [],
        'action': {'kind': 'buttons', 'entries': []},
        'warnings': [{'code': 'notice', 'message': 'same warning'}]})
    changed_resolved = pub.prepare_design(10, 1)
    assert changed_resolved['warning_token'] != token1
    assert not pub.require_warning_ack(changed_resolved, token1)
    assert pub._warning_token([{'code': 'different'}], 'fingerprint') != pub._warning_token(
        [{'code': 'notice', 'message': 'same warning'}], 'fingerprint')


@pytest.mark.parametrize(('field', 'changed'), [
    ('role_id', 202),
    ('required_role_id', 303),
    ('xp_boost_multiplier', 2.0),
])
def test_warning_ack_invalidated_by_purchase_semantics_fields(monkeypatch, field, changed):
    source = {'presentation': {'mode': 'frame', 'content': '{{unknown}}', 'embeds': []},
              'products': [1], 'action': {'kind': 'buttons',
                                          'entries': [{'product_id': 1}]}, 'id': 1}
    live = {1: {'id': 1, 'name': 'Item', 'enabled': 1, 'price': 10,
                'role_id': 101, 'required_role_id': 102,
                'xp_boost_multiplier': 1.5}}
    monkeypatch.setattr(pub, '_load_design_and_rows', lambda *a: (source, live, 'Saved'))
    monkeypatch.setattr(pub.SP, 'validate_design', lambda *a: [])
    async def currency(_guild): return {}
    monkeypatch.setattr(pub, 'get_currency_config', currency)
    monkeypatch.setattr(pub.SP, 'preview_design', lambda *a: {
        'content': 'same payload', 'embeds': [],
        'action': {'kind': 'buttons', 'entries': []},
        'warnings': [{'code': 'unknown_token', 'message': 'same warning'}]})

    old_token = pub.prepare_design(10, 1)['warning_token']
    previous = live[1][field]
    live[1][field] = changed
    fresh = pub.prepare_design(10, 1)
    assert fresh['warning_token'] != old_token
    assert not pub.require_warning_ack(fresh, old_token)
    live[1][field] = previous
